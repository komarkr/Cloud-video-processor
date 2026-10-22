import json
import logging
import os
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Task, TaskStatus, User
from app.routers.deps import get_current_user
from app.schemas import TaskOut
from app.services.probe import VideoValidationError, probe_video

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.post(
    "",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload video and create a queued processing task",
)
async def create_task(
    file: UploadFile = File(...),
    operation_type: str = Form("convert"),
    params: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    1. Stream upload file in chunks into /data/uploads/<uuid>.<ext>.
    2. Interrupt and delete if total size > MAX_FILE_MB (100 MB).
    3. Run ffprobe validation (video stream, duration <= 120s, resolution <= 1920x1080, allowed formats).
    4. Check user active tasks limit (<= 5).
    5. Check global queue capacity (<= MAX_QUEUE).
    6. Save task record with status 'queued'.
    7. Clean up temporary file on any error (422/429/503).
    """
    os.makedirs(settings.upload_dir, exist_ok=True)

    task_id = uuid.uuid4()
    original_filename = file.filename or "upload.mp4"
    _, ext = os.path.splitext(original_filename)
    if not ext:
        ext = ".mp4"

    file_path = os.path.join(settings.upload_dir, f"{task_id}{ext}")
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
    try:
        probe_video(file_path)
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
        )

    # Parse optional JSON parameters
    parsed_params = None
    if params:
        try:
            parsed_params = json.loads(params)
        except json.JSONDecodeError:
            parsed_params = {"raw": params}

    # Create task record in DB
    new_task = Task(
        id=task_id,
        user_id=current_user.id,
        status=TaskStatus.queued,
        operation_type=operation_type,
        params=parsed_params,
        input_file=file_path,
        output_file=None,
        error_message=None,
    )
    db.add(new_task)
    db.commit()
    db.refresh(new_task)

    return new_task


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

    return task
