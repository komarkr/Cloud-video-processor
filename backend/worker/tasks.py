from datetime import datetime, timezone
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

logger = logging.getLogger(__name__)


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
    """Build FFmpeg command line arguments based on operation_type and params."""
    op = (task.operation_type or "convert").lower()
    params = task.params or {}

    if op == "convert":
        return [
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

    else:
        # Default convert operation
        return [
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
    3. Build operation arguments and run FFmpeg with live progress tracking.
    4. On success: set status 'done', output_file, finished_at.
    5. On Cancelled: delete partial file and set status 'cancelled'.
    6. On error: set status 'failed', error_message.
    """
    task_uuid = uuid.UUID(task_id)
    r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    db = SessionLocal()
    worker_id = socket.gethostname()
    os.makedirs(settings.output_dir, exist_ok=True)
    output_path = os.path.join(settings.output_dir, f"{task_id}.mp4")

    try:
        task = db.query(Task).filter(Task.id == task_uuid).first()
        if not task:
            logger.error("Task %s not found in database.", task_id)
            return

        # Check pre-cancellation
        if task.status == TaskStatus.cancelled or r.exists(f"cancel:{task_id}"):
            task.status = TaskStatus.cancelled
            task.finished_at = datetime.now(timezone.utc)
            db.commit()
            r.hset(f"task:{task_id}", mapping={"status": "cancelled"})
            return

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

        # Get video duration for progress calculation
        total_sec = 1.0
        try:
            probe_res = probe_video(task.input_file)
            total_sec = max(0.1, probe_res.duration)
        except Exception as e:
            logger.warning("Could not probe video duration for task %s: %s", task_id, e)

        # Build FFmpeg command arguments
        args = build_ffmpeg_args(task, output_path)

        # Run FFmpeg with progress
        run_ffmpeg(task_id, args, total_sec, r)

        # Complete task
        finished_at = datetime.now(timezone.utc)
        task.status = TaskStatus.done
        task.output_file = output_path
        task.finished_at = finished_at
        db.commit()

        r.hset(
            f"task:{task_id}",
            mapping={
                "status": "done",
                "progress": 100,
                "output_file": output_path,
                "finished_at": str(finished_at),
            },
        )
        logger.info("Task %s completed successfully.", task_id)

    except Cancelled:
        logger.info("Task %s was cancelled by user.", task_id)
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
        logger.error("Task %s failed: %s", task_id, e)
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
        db.close()
