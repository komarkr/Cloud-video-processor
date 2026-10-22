from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
import uuid

import redis
from app.config import settings
from app.db import SessionLocal
from app.models import Task, TaskStatus
from app.services.probe import get_ffprobe_executable, probe_video
from worker.app import celery_app
from worker.heartbeat import set_current_task

logger = logging.getLogger(__name__)


WATERMARK_POSITIONS = {
    "top_left": "10:10",
    "top_right": "main_w-overlay_w-10:10",
    "bottom_left": "10:main_h-overlay_h-10",
    "bottom_right": "main_w-overlay_w-10:main_h-overlay_h-10",
    "center": "(main_w-overlay_w)/2:(main_h-overlay_h)/2",
}


def get_output_extension(task: Task) -> str:
    """Determine output file extension based on operation type and parameters."""
    op = (task.operation_type or "").lower()
    params = task.params or {}
    if op in ["extract_audio", "extract"]:
        return ".mp3"
    if op == "audio" and params.get("action") not in ["mute", "remove"]:
        return ".mp3"
    return ".mp4"


class Cancelled(Exception):
    """Raised when task execution is cancelled via Redis flag."""
    pass


def get_ffmpeg_executable() -> str:
    """Find ffmpeg executable path."""
    custom_path = os.environ.get("FFMPEG_PATH")
    if custom_path and os.path.exists(custom_path):
        return custom_path

    which_path = shutil.which("ffmpeg")
    if which_path:
        return which_path

    # Check Windows WinGet or Chocolatey installation
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        winget_dir = os.path.join(local_app_data, "Microsoft", "WinGet", "Packages")
        if os.path.exists(winget_dir):
            for root, _, files in os.walk(winget_dir):
                if "ffmpeg.exe" in files:
                    return os.path.join(root, "ffmpeg.exe")

    # Fallback to imageio-ffmpeg if installed
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass

    return "ffmpeg"


def run_ffmpeg(task_id: str, args: list[str], total_sec: float, r: redis.Redis) -> None:
    """Execute FFmpeg with live progress parsing and cooperative cancellation checking."""
    os.makedirs(settings.log_dir, exist_ok=True)
    log_file_path = os.path.join(settings.log_dir, f"{task_id}.log")
    cmd = [
        get_ffmpeg_executable(),
        "-y",
        "-progress",
        "pipe:1",
        "-nostats",
        "-loglevel",
        "error",
        *args,
    ]

    with open(log_file_path, "w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True)
        last_check = 0.0

        if proc.stdout:
            for line in proc.stdout:
                if line.startswith("out_time_us="):
                    v = line.split("=")[1].strip()
                    if v.isdigit() and total_sec > 0:
                        pct = min(99, int(int(v) / 1e6 / total_sec * 100))
                        try:
                            r.hset(f"task:{task_id}", "progress", pct)
                        except Exception:
                            pass

                now = time.time()
                if now - last_check > 0.5:
                    last_check = now
                    try:
                        worker_id = socket.gethostname()
                        r.set(f"worker:heartbeat:{worker_id}", 1, ex=60)
                        if r.exists(f"cancel:{task_id}"):
                            proc.terminate()
                            try:
                                proc.wait(5)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                            raise Cancelled()
                    except Cancelled:
                        raise
                    except Exception:
                        pass

        ret = proc.wait()
        if ret != 0:
            error_content = ""
            if os.path.exists(log_file_path):
                with open(log_file_path, "r", encoding="utf-8", errors="replace") as f:
                    error_content = f.read()[-500:]
            raise RuntimeError(error_content or f"FFmpeg process failed with exit code {ret}.")


def build_ffmpeg_args(task: Task, output_path: str) -> list[str]:
    """Build FFmpeg command line arguments based on operation_type and params with strict protocol whitelist."""
    op = (task.operation_type or "convert").lower()
    params = task.params or {}

    if op == "convert":
        return [
            "-protocol_whitelist",
            "file",
            "-i",
            task.input_file,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            output_path,
        ]

    elif op == "scale":
        SCALE_CRF_TABLE = {1080: 22, 720: 23, 480: 25, 360: 28}
        raw_res = params.get("resolution") or params.get("height") or 720
        cleaned = re.sub(r"[^\d]", "", str(raw_res))
        height = int(cleaned) if cleaned else 720
        if height not in SCALE_CRF_TABLE:
            height = 720
        crf = SCALE_CRF_TABLE.get(height, 23)

        return [
            "-protocol_whitelist",
            "file",
            "-i",
            task.input_file,
            "-vf",
            f"scale=-2:{height}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-c:a",
            "aac",
            output_path,
        ]

    elif op == "watermark":
        logo_path = params.get("logo_path")
        pos_key = params.get("position", "bottom_right")
        pos_expr = WATERMARK_POSITIONS.get(pos_key, WATERMARK_POSITIONS["bottom_right"])
        try:
            opacity = float(params.get("opacity", 1.0))
        except (ValueError, TypeError):
            opacity = 1.0

        if 0.0 < opacity < 1.0:
            filter_complex = f"[1:v]format=rgba,colorchannelmixer=aa={opacity}[wm];[0:v][wm]overlay={pos_expr}"
        else:
            filter_complex = f"[0:v][1:v]overlay={pos_expr}"

        return [
            "-protocol_whitelist",
            "file",
            "-i",
            task.input_file,
            "-protocol_whitelist",
            "file",
            "-i",
            logo_path,
            "-filter_complex",
            filter_complex,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            output_path,
        ]

    elif op in ["audio", "extract_audio", "mute", "mute_audio", "remove_audio"]:
        action = params.get("action", "").lower()
        if action in ["mute", "remove"] or op in ["mute", "mute_audio", "remove_audio"]:
            # Mute audio, copy video stream
            return [
                "-protocol_whitelist",
                "file",
                "-i",
                task.input_file,
                "-an",
                "-c:v",
                "copy",
                output_path,
            ]
        else:
            # Extract audio to MP3 using libmp3lame
            return [
                "-protocol_whitelist",
                "file",
                "-i",
                task.input_file,
                "-vn",
                "-c:a",
                "libmp3lame",
                "-q:a",
                "2",
                output_path,
            ]

    else:
        # Default convert operation
        return [
            "-protocol_whitelist",
            "file",
            "-i",
            task.input_file,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            output_path,
        ]


@celery_app.task(name="worker.process_task")
def process_task(task_id: str):
    """
    Celery task that executes video processing:
    1. If cancelled in DB/Redis, exit immediately.
    2. Set state 'running', started_at, worker_id (hostname) in DB and Redis task:{id}.
    3. Check disk space availability.
    4. Build operation arguments and run FFmpeg with live progress tracking.
    5. On success: set status 'done', output_file, finished_at, expires_at.
    6. On Cancelled: delete partial file and set status 'cancelled'.
    7. On error: set status 'failed', error_message.
    """
    task_uuid = uuid.UUID(task_id)
    set_current_task(task_id)
    r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    db = SessionLocal()
    worker_id = socket.gethostname()
    os.makedirs(settings.output_dir, exist_ok=True)

    try:
        task = db.query(Task).filter(Task.id == task_uuid).first()
        if not task:
            logger.error("[worker_id=%s task_id=%s] Task not found in database.", worker_id, task_id)
            return

        ext = get_output_extension(task)
        output_path = os.path.join(settings.output_dir, f"{task_id}{ext}")

        # Check pre-cancellation
        if task.status == TaskStatus.cancelled or r.exists(f"cancel:{task_id}"):
            task.status = TaskStatus.cancelled
            task.finished_at = datetime.now(timezone.utc)
            db.commit()
            r.hset(f"task:{task_id}", mapping={"status": "cancelled"})
            logger.info("[worker_id=%s task_id=%s] Task was cancelled before processing.", worker_id, task_id)
            return

        # Check available disk storage
        disk_info = shutil.disk_usage(settings.output_dir)
        if disk_info.free < 50 * 1024 * 1024:  # At least 50 MB required
            raise RuntimeError("Insufficient server disk space for processing.")

        # Transition to running
        started_at = datetime.now(timezone.utc)
        task.status = TaskStatus.running
        task.started_at = started_at
        task.worker_id = worker_id
        db.commit()

        r.hset(
            f"task:{task_id}",
            mapping={
                "status": "running",
                "progress": 0,
                "worker_id": worker_id,
                "started_at": str(started_at),
            },
        )
        try:
            r.set(f"worker:heartbeat:{worker_id}", 1, ex=60)
        except Exception:
            pass

        logger.info(
            "[worker_id=%s task_id=%s] Starting processing (operation: %s)",
            worker_id,
            task_id,
            task.operation_type,
        )

        # Get video duration for progress calculation
        total_sec = 1.0
        try:
            probe_res = probe_video(task.input_file)
            total_sec = max(0.1, probe_res.duration)
            if not task.duration_sec or task.duration_sec <= 0:
                task.duration_sec = total_sec
                db.commit()
        except Exception as e:
            logger.warning("[worker_id=%s task_id=%s] Could not probe video duration: %s", worker_id, task_id, e)

        # Verify audio stream exists if operation is audio extraction
        op = (task.operation_type or "convert").lower()
        params = task.params or {}
        if op in ["extract_audio", "extract"] or (op == "audio" and params.get("action") not in ["mute", "remove"]):
            try:
                probe_res = probe_video(task.input_file)
                has_audio = any(s.get("codec_type") == "audio" for s in probe_res.raw_info.get("streams", []))
                if not has_audio:
                    raise RuntimeError("Input video does not contain an audio stream to extract.")
            except Exception as e:
                raise RuntimeError(str(e))

        # Build FFmpeg command arguments
        args = build_ffmpeg_args(task, output_path)

        # Run FFmpeg with progress
        run_ffmpeg(task_id, args, total_sec, r)

        # Complete task
        finished_at = datetime.now(timezone.utc)
        expires_at = finished_at + timedelta(hours=settings.result_lifetime_hours)
        task.status = TaskStatus.done
        task.output_file = output_path
        task.finished_at = finished_at
        task.expires_at = expires_at
        db.commit()

        r.hset(
            f"task:{task_id}",
            mapping={
                "status": "done",
                "progress": 100,
                "output_file": output_path,
                "finished_at": str(finished_at),
                "expires_at": str(expires_at),
            },
        )
        duration = (finished_at - started_at).total_seconds()
        logger.info(
            "[worker_id=%s task_id=%s] Task completed successfully in %.2fs.",
            worker_id,
            task_id,
            duration,
        )

    except Cancelled:
        logger.info("[worker_id=%s task_id=%s] Task was cancelled by user.", worker_id, task_id)
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        task.status = TaskStatus.cancelled
        task.finished_at = datetime.now(timezone.utc)
        task.error_message = "Task cancelled by user"
        db.commit()
        r.hset(
            f"task:{task_id}",
            mapping={
                "status": "cancelled",
                "finished_at": str(datetime.now(timezone.utc)),
            },
        )

    except Exception as e:
        logger.error("[worker_id=%s task_id=%s] Task failed: %s", worker_id, task_id, e)
        task.status = TaskStatus.failed
        task.error_message = str(e)
        task.finished_at = datetime.now(timezone.utc)
        db.commit()
        r.hset(
            f"task:{task_id}",
            mapping={
                "status": "failed",
                "error": str(e),
                "finished_at": str(datetime.now(timezone.utc)),
            },
        )

    finally:
        set_current_task(None)
        db.close()


@celery_app.task(name="worker.cleanup_expired_tasks")
def celery_cleanup_expired_tasks() -> int:
    """Periodic Celery task to clean up expired tasks and their files."""
    from app.services.cleanup import cleanup_expired_tasks
    db = SessionLocal()
    r = None
    try:
        r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception:
        pass
    try:
        purged = cleanup_expired_tasks(db, r)
        worker_id = socket.gethostname()
        logger.info("[worker_id=%s] Periodic cleanup finished, purged %d tasks.", worker_id, purged)
        return purged
    finally:
        db.close()

