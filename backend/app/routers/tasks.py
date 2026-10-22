import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image
import redis
import redis.asyncio as aioredis
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Task, TaskStatus, User
from app.routers.deps import get_current_user
from app.schemas import TaskOut
from app.services.probe import VideoValidationError, probe_video
from app.services.estimation import enrich_task_out, estimate_wait_sec, get_queue_position

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def ensure_task_owner(task_id: uuid.UUID, user: User, db: Session) -> Task:
    """Ensure task exists and belongs to user (or user is admin), else raise 404."""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    if task.user_id != user.id and not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    return task


@router.post(
    "",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload video and create a queued processing task",
)
async def create_task(
    file: UploadFile = File(...),
    logo: Optional[UploadFile] = File(None),
    operation_type: str = Form("convert"),
    params: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    1. Stream upload file in chunks into /data/uploads/<uuid>.<ext>.
    2. Interrupt and delete if total size > MAX_FILE_MB (100 MB).
    3. Run ffprobe validation (video stream, duration <= 120s, resolution <= 1920x1080, allowed formats).
    4. Check active tasks limit (<= 5).
    5. Check global queue capacity (<= MAX_QUEUE).
    6. Prune user history if >= MAX_HISTORY (delete oldest completed tasks along with files).
    7. Validate logo for watermark (PNG <= 2 MB via Pillow).
    8. Save task record with status 'queued'.
    9. Dispatch Celery task: process_task.delay(task_id).
    """
    os.makedirs(settings.upload_dir, exist_ok=True)

    task_id = uuid.uuid4()
    original_filename = file.filename or "upload.mp4"
    _, ext = os.path.splitext(original_filename)
    if not ext:
        ext = ".mp4"

    file_path = os.path.join(settings.upload_dir, f"{task_id}{ext}")
    logo_file_path: Optional[str] = None
    max_bytes = settings.max_file_mb * 1024 * 1024
    total_bytes = 0
    chunk_size = 1024 * 1024  # 1 MB

    try:
        with open(file_path, "wb") as out_file:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    out_file.close()
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"File size exceeds maximum allowed limit of {settings.max_file_mb} MB.",
                    )
                out_file.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        if os.path.exists(file_path):
            os.remove(file_path)
        logger.error("Failed to save uploaded file: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save uploaded file: {str(e)}",
        )

    # Validate file with ffprobe
    probe_res = None
    try:
        probe_res = probe_video(file_path)
    except VideoValidationError as e:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(
            status_code=e.status_code,
            detail=e.message,
        )
    except Exception as e:
        if os.path.exists(file_path):
            os.remove(file_path)
        logger.error("Unexpected error during video probing: %s", e)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Video validation failed: {str(e)}",
        )

    # Check active user tasks limit (active <= 5)
    active_count = (
        db.query(Task)
        .filter(
            Task.user_id == current_user.id,
            Task.status.in_([TaskStatus.queued, TaskStatus.running]),
        )
        .count()
    )
    if active_count >= settings.max_active_tasks:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Active tasks limit exceeded (maximum {settings.max_active_tasks} active tasks allowed).",
        )

    # Check global queue capacity (queued <= MAX_QUEUE)
    global_queued_count = (
        db.query(Task)
        .filter(Task.status == TaskStatus.queued)
        .count()
    )
    if global_queued_count >= settings.max_queue:
        if os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server queue is at maximum capacity. Please try again later.",
            headers={"Retry-After": "30"},
        )

    # Parse optional JSON parameters
    parsed_params = {}
    if params:
        try:
            parsed = json.loads(params)
            if isinstance(parsed, dict):
                parsed_params = parsed
            else:
                parsed_params = {"raw": params}
        except json.JSONDecodeError:
            parsed_params = {"raw": params}

    # Validate logo for watermark operation
    op = (operation_type or "convert").lower()
    if op == "watermark":
        if not logo or not logo.filename:
            if os.path.exists(file_path):
                os.remove(file_path)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Watermark operation requires a 'logo' image file.",
            )

        logo_file_path = os.path.join(settings.upload_dir, f"{task_id}_logo.png")
        max_logo_bytes = 2 * 1024 * 1024  # 2 MB limit
        logo_bytes = 0

        try:
            with open(logo_file_path, "wb") as logo_out:
                while True:
                    l_chunk = await logo.read(256 * 1024)
                    if not l_chunk:
                        break
                    logo_bytes += len(l_chunk)
                    if logo_bytes > max_logo_bytes:
                        logo_out.close()
                        if os.path.exists(logo_file_path):
                            os.remove(logo_file_path)
                        if os.path.exists(file_path):
                            os.remove(file_path)
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Watermark logo exceeds maximum allowed size of 2 MB.",
                        )
                    logo_out.write(l_chunk)
        except HTTPException:
            raise
        except Exception as e:
            if os.path.exists(logo_file_path):
                os.remove(logo_file_path)
            if os.path.exists(file_path):
                os.remove(file_path)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to save logo file: {str(e)}",
            )

        # Validate with Pillow verify()
        try:
            with Image.open(logo_file_path) as img:
                img.verify()
                if img.format != "PNG":
                    raise ValueError("Logo format is not PNG")
        except Exception:
            if os.path.exists(logo_file_path):
                os.remove(logo_file_path)
            if os.path.exists(file_path):
                os.remove(file_path)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Watermark logo must be a valid PNG image.",
            )

        parsed_params["logo_path"] = logo_file_path

    # History limit: if user has >= MAX_HISTORY (20), delete oldest completed tasks + files
    user_total_count = db.query(Task).filter(Task.user_id == current_user.id).count()
    if user_total_count >= settings.max_history:
        to_delete_count = (user_total_count - settings.max_history) + 1
        oldest_completed = (
            db.query(Task)
            .filter(
                Task.user_id == current_user.id,
                Task.status.in_([TaskStatus.done, TaskStatus.failed, TaskStatus.cancelled]),
            )
            .order_by(Task.created_at.asc())
            .limit(to_delete_count)
            .all()
        )
        for old in oldest_completed:
            if old.input_file and os.path.exists(old.input_file):
                try:
                    os.remove(old.input_file)
                except OSError:
                    pass
            if old.output_file and os.path.exists(old.output_file):
                try:
                    os.remove(old.output_file)
                except OSError:
                    pass
            if old.params and isinstance(old.params, dict):
                old_logo = old.params.get("logo_path")
                if old_logo and os.path.exists(old_logo):
                    try:
                        os.remove(old_logo)
                    except OSError:
                        pass
            old_log = os.path.join(settings.log_dir, f"{old.id}.log")
            if os.path.exists(old_log):
                try:
                    os.remove(old_log)
                except OSError:
                    pass
            db.delete(old)
        db.commit()

    # Create task record in DB
    new_task = Task(
        id=task_id,
        user_id=current_user.id,
        status=TaskStatus.queued,
        operation_type=operation_type,
        params=parsed_params if parsed_params else None,
        input_file=file_path,
        output_file=None,
        duration_sec=float(probe_res.duration) if probe_res and probe_res.duration else 0.0,
        error_message=None,
    )
    db.add(new_task)
    db.commit()
    db.refresh(new_task)

    # Dispatch Celery worker task asynchronously
    try:
        from worker.tasks import process_task
        process_task.delay(str(new_task.id))
    except Exception as e:
        logger.warning("Could not dispatch Celery task %s to queue: %s", new_task.id, e)

    try:
        r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception:
        r = None
    enrich_task_out(new_task, db, r)

    return new_task


@router.get(
    "/{task_id}/events",
    summary="Stream task progress and status updates via Server-Sent Events (SSE)",
)
async def events(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    SSE stream reading state from Redis once per second:
    Yields data: {"status": <status>, "progress": <int>}\n\n.
    Terminates when status is done, failed, or cancelled.
    """
    ensure_task_owner(task_id, current_user, db)

    async def gen():
        r = None
        try:
            r = aioredis.from_url(settings.redis_url, decode_responses=True)
        except Exception:
            r = None

        sync_r = None
        try:
            sync_r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        except Exception:
            pass

        while True:
            h = {}
            if r:
                try:
                    h = await r.hgetall(f"task:{task_id}")
                except Exception as e:
                    logger.warning("Could not read Redis for task events: %s", e)
                    h = {}

            if not h:
                # Fallback to database check
                t = db.query(Task).filter(Task.id == task_id).first()
                if t:
                    status_val = t.status.value
                    progress = 100 if status_val == "done" else 0
                else:
                    status_val = "queued"
                    progress = 0
            else:
                status_val = h.get("status", "queued")
                progress_raw = h.get("progress", 0)
                try:
                    progress = int(progress_raw)
                except (ValueError, TypeError):
                    progress = 100 if status_val == "done" else 0

            payload = {"status": status_val, "progress": progress}
            if status_val == "queued":
                try:
                    db.expire_all()
                    t = db.query(Task).filter(Task.id == task_id).first()
                    if t:
                        payload["queue_position"] = get_queue_position(db, t)
                        payload["eta_sec"] = estimate_wait_sec(db, sync_r, t)
                except Exception as ex:
                    logger.warning("Could not compute queue ETA for task %s: %s", task_id, ex)

            yield f"data: {json.dumps(payload)}\n\n"
            if status_val in ("done", "failed", "cancelled"):
                break
            await asyncio.sleep(1)

        if r:
            try:
                await r.aclose()
            except Exception:
                pass

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/{task_id}/download",
    summary="Download processed output file for a completed task",
)
def download_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Download processed output file:
    - Only allowed for owner (or admin).
    - Only for status == 'done'.
    - Only before expires_at.
    """
    task = ensure_task_owner(task_id, current_user, db)

    if task.status != TaskStatus.done:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Task is not completed yet (current status: '{task.status.value}')",
        )

    if task.expires_at:
        now = datetime.now(timezone.utc)
        exp = task.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if now > exp:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="Task output has expired and is no longer available for download.",
            )

    if not task.output_file or not os.path.exists(task.output_file):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Processed output file not found on disk.",
        )

    filename = os.path.basename(task.output_file)
    ext = os.path.splitext(filename)[1].lower()
    media_type = "audio/mpeg" if ext == ".mp3" else "video/mp4"

    return FileResponse(
        path=task.output_file,
        filename=filename,
        media_type=media_type,
    )


@router.delete(
    "/{task_id}",
    summary="Delete a task record and its associated media files",
)
def delete_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Delete task:
    - If active (queued or running), cancel first via Redis.
    - Delete input, output, logo, and log files.
    - Remove Redis keys and database record.
    """
    task = ensure_task_owner(task_id, current_user, db)

    try:
        r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception:
        r = None

    if task.status in [TaskStatus.queued, TaskStatus.running]:
        if r:
            try:
                r.set(f"cancel:{task_id}", 1, ex=3600)
            except Exception:
                pass
        task.status = TaskStatus.cancelled
        task.finished_at = datetime.now(timezone.utc)
        db.commit()

    # Clean up files from disk
    if task.input_file and os.path.exists(task.input_file):
        try:
            os.remove(task.input_file)
        except OSError:
            pass

    if task.output_file and os.path.exists(task.output_file):
        try:
            os.remove(task.output_file)
        except OSError:
            pass

    if task.params and isinstance(task.params, dict):
        logo_path = task.params.get("logo_path")
        if logo_path and os.path.exists(logo_path):
            try:
                os.remove(logo_path)
            except OSError:
                pass

    log_path = os.path.join(settings.log_dir, f"{task_id}.log")
    if os.path.exists(log_path):
        try:
            os.remove(log_path)
        except OSError:
            pass

    # Clean up Redis keys
    if r:
        try:
            r.delete(f"task:{task_id}", f"cancel:{task_id}")
        except Exception:
            pass

    db.delete(task)
    db.commit()

    return {"status": "ok", "message": "Task and associated files deleted", "task_id": str(task_id)}


@router.post(
    "/{task_id}/cancel",
    summary="Cancel a queued or running task",
)
def cancel_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Cancel processing task:
    - If status == 'queued': immediately transition to 'cancelled' in DB and Redis.
    - If status == 'running': set cancel:{task_id} in Redis to trigger cooperative cancellation.
    - If already completed/cancelled/failed: return 400.
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )

    if task.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: you do not have permission to cancel this task",
        )

    if task.status in [TaskStatus.done, TaskStatus.failed, TaskStatus.cancelled]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel task with status '{task.status.value}'",
        )

    # Redis cancellation flag
    try:
        r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception:
        r = None

    if task.status == TaskStatus.queued:
        now = datetime.now(timezone.utc)
        task.status = TaskStatus.cancelled
        task.error_message = "Task cancelled by user before processing"
        task.finished_at = now
        db.commit()

        if r:
            try:
                r.set(f"cancel:{task_id}", 1, ex=3600)
                r.hset(f"task:{task_id}", mapping={"status": "cancelled", "finished_at": str(now)})
            except Exception as e:
                logger.warning("Redis operation failed: %s", e)

        return {"status": "ok", "message": "Queued task was immediately cancelled", "task_id": str(task_id)}

    elif task.status == TaskStatus.running:
        if r:
            try:
                r.set(f"cancel:{task_id}", 1, ex=3600)
                r.hset(f"task:{task_id}", mapping={"cancellation_requested": "true"})
            except Exception as e:
                logger.warning("Redis operation failed: %s", e)

        return {"status": "ok", "message": "Cancellation request submitted for running task", "task_id": str(task_id)}


@router.get(
    "",
    response_model=List[TaskOut],
    summary="List all tasks created by the authenticated user",
)
def get_my_tasks(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retrieve only tasks belonging to the currently logged in user."""
    tasks = (
        db.query(Task)
        .filter(Task.user_id == current_user.id)
        .order_by(Task.created_at.desc())
        .all()
    )
    try:
        r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception:
        r = None

    for t in tasks:
        enrich_task_out(t, db, r)

    return tasks


@router.get(
    "/{task_id}",
    response_model=TaskOut,
    summary="Get details of a specific task with ownership verification",
)
def get_task_by_id(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retrieve a task by ID. Return 404 if not found, 403 if user is not the owner."""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )

    if task.user_id != current_user.id and not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: you do not have permission to view this task",
        )

    try:
        r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception:
        r = None

    enrich_task_out(task, db, r)
    return task
