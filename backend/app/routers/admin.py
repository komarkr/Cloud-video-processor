from datetime import datetime, timezone
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
import redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Task, TaskStatus, User
from app.routers.deps import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get(
    "/overview",
    summary="Admin overview: task statistics, queue length, worker heartbeats, recent tasks, and autoscaler logs",
)
def get_admin_overview(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Returns complete cluster status for admin dashboard:
    1. Task counts by status + total
    2. Queue length
    3. Active workers list with CPU, RAM, current_task, last_seen
    4. Recent 50 tasks (user email, operation, status, progress, worker/server, duration)
    5. Recent autoscaler scaling events
    """
    # 1. Status counts
    counts = {
        "queued": db.query(Task).filter(Task.status == TaskStatus.queued).count(),
        "running": db.query(Task).filter(Task.status == TaskStatus.running).count(),
        "done": db.query(Task).filter(Task.status == TaskStatus.done).count(),
        "failed": db.query(Task).filter(Task.status == TaskStatus.failed).count(),
        "cancelled": db.query(Task).filter(Task.status == TaskStatus.cancelled).count(),
    }
    counts["total"] = sum(counts.values())
    queue_length = counts["queued"]

    # Redis connection for heartbeats, progress, and autoscaler logs
    try:
        r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception as exc:
        logger.warning("Could not connect to Redis for admin overview: %s", exc)
        r = None

    now = datetime.now(timezone.utc)

    # 2. Worker heartbeats
    workers: List[Dict[str, Any]] = []
    if r:
        try:
            keys = r.keys("hb:*") or []
            for k in sorted(keys):
                h = r.hgetall(k)
                if not h:
                    continue
                hostname = h.get("hostname") or k.replace("hb:", "")
                cpu_raw = h.get("cpu", "0.0")
                ram_raw = h.get("ram", "0.0")
                current_task = h.get("current_task") or None
                ts_str = h.get("timestamp")

                last_seen = "щойно"
                if ts_str:
                    try:
                        dt = datetime.fromisoformat(ts_str)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        diff = max(0, int((now - dt).total_seconds()))
                        if diff <= 1:
                            last_seen = "щойно"
                        elif diff < 60:
                            last_seen = f"{diff} с тому"
                        else:
                            last_seen = f"{diff // 60} хв тому"
                    except Exception:
                        last_seen = ts_str

                try:
                    cpu = float(cpu_raw)
                except (ValueError, TypeError):
                    cpu = 0.0

                try:
                    ram = float(ram_raw)
                except (ValueError, TypeError):
                    ram = 0.0

                workers.append({
                    "hostname": hostname,
                    "cpu": cpu,
                    "ram": ram,
                    "current_task": current_task,
                    "last_seen": last_seen,
                    "timestamp": ts_str,
                })
        except Exception as exc:
            logger.warning("Error fetching heartbeats from Redis: %s", exc)

    # 3. Recent 50 tasks
    recent_tasks: List[Dict[str, Any]] = []
    try:
        recent = (
            db.query(Task, User.email)
            .join(User, Task.user_id == User.id)
            .order_by(Task.created_at.desc())
            .limit(50)
            .all()
        )
        for task, user_email in recent:
            progress = 0
            if task.status == TaskStatus.done:
                progress = 100
            elif r:
                try:
                    prog_val = r.hget(f"task:{task.id}", "progress")
                    if prog_val:
                        progress = int(prog_val)
                except Exception:
                    progress = 0

            duration_text = None
            if task.started_at and task.finished_at:
                diff_sec = max(0, int((task.finished_at - task.started_at).total_seconds()))
                if diff_sec < 60:
                    duration_text = f"{diff_sec} с"
                else:
                    duration_text = f"{diff_sec // 60} хв {diff_sec % 60} с"
            elif task.duration_sec:
                duration_text = f"~{int(task.duration_sec)} с (відео)"

            recent_tasks.append({
                "id": str(task.id),
                "user_email": user_email,
                "operation_type": task.operation_type,
                "status": task.status.value,
                "progress": progress,
                "worker_id": task.worker_id,
                "duration_sec": task.duration_sec,
                "duration_text": duration_text,
                "created_at": task.created_at.isoformat() if task.created_at else None,
                "started_at": task.started_at.isoformat() if task.started_at else None,
                "finished_at": task.finished_at.isoformat() if task.finished_at else None,
                "error_message": task.error_message,
            })
    except Exception as exc:
        logger.warning("Error querying recent tasks for admin: %s", exc)

    # 4. Autoscaler events
    autoscaler_events: List[Dict[str, Any]] = []
    if r:
        try:
            raw_logs = r.lrange("autoscaler:log", -50, -1) or []
            for item in reversed(raw_logs):
                try:
                    autoscaler_events.append(json.loads(item))
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("Error fetching autoscaler logs: %s", exc)

    return {
        "status": "ok",
        "status_counts": counts,
        "queue_length": queue_length,
        "active_workers_count": max(len(workers), 1),
        "workers": workers,
        "recent_tasks": recent_tasks,
        "autoscaler_events": autoscaler_events,
    }


@router.post(
    "/tasks/{task_id}/cancel",
    summary="Admin cancel task (allows cancelling any user's queued or running task)",
)
def admin_cancel_task(
    task_id: uuid.UUID,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin privilege to cancel any active task in the cluster."""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )

    if task.status in [TaskStatus.done, TaskStatus.failed, TaskStatus.cancelled]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel task with status '{task.status.value}'",
        )

    try:
        r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception:
        r = None

    now = datetime.now(timezone.utc)
    if task.status == TaskStatus.queued:
        task.status = TaskStatus.cancelled
        task.error_message = f"Task cancelled by administrator ({admin.email})"
        task.finished_at = now
        db.commit()

        if r:
            try:
                r.set(f"cancel:{task_id}", 1, ex=3600)
                r.hset(f"task:{task_id}", mapping={"status": "cancelled", "finished_at": str(now)})
            except Exception:
                pass

        return {"status": "ok", "message": "Queued task cancelled by admin", "task_id": str(task_id)}

    elif task.status == TaskStatus.running:
        if r:
            try:
                r.set(f"cancel:{task_id}", 1, ex=3600)
                r.hset(f"task:{task_id}", mapping={"cancellation_requested": "true"})
            except Exception:
                pass

        return {"status": "ok", "message": "Cancellation request submitted by admin for running task", "task_id": str(task_id)}


@router.post(
    "/cleanup",
    summary="Trigger immediate cleanup of expired tasks and their files",
)
def trigger_admin_cleanup(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Manually invoke cleanup of expired tasks and return count of purged tasks."""
    from app.services.cleanup import cleanup_expired_tasks
    try:
        r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception:
        r = None

    purged = cleanup_expired_tasks(db, r)
    return {
        "status": "ok",
        "message": f"Successfully purged {purged} expired tasks.",
        "purged_count": purged,
    }

