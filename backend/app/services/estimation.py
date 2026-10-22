import logging
from typing import Optional
import redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Task, TaskStatus

logger = logging.getLogger(__name__)


def alive_workers(r: Optional[redis.Redis] = None) -> int:
    """Determine the number of currently active/healthy workers."""
    if r:
        try:
            # 1. Check Redis worker heartbeats
            heartbeats = r.keys("worker:heartbeat:*")
            if heartbeats:
                return len(heartbeats)
        except Exception:
            pass

    # 2. Try Celery ping with short timeout
    try:
        from worker.app import celery_app
        insp = celery_app.control.inspect(timeout=0.2)
        ping_res = insp.ping()
        if ping_res:
            return len(ping_res)
    except Exception:
        pass

    # Default fallback: at least 1 worker
    return 1


def recent_speed(db: Session) -> float:
    """
    Calculate average processing speed:
    duration_sec / (finished_at - started_at) over the last 20 completed tasks.
    Returns 1.0 if not enough data.
    """
    try:
        completed = (
            db.scalars(
                select(Task)
                .where(
                    Task.status == TaskStatus.done,
                    Task.started_at.is_not(None),
                    Task.finished_at.is_not(None),
                    Task.duration_sec.is_not(None),
                    Task.duration_sec > 0,
                )
                .order_by(Task.finished_at.desc())
                .limit(20)
            ).all()
        )

        if not completed:
            return 1.0

        ratios = []
        for t in completed:
            if t.finished_at and t.started_at:
                elapsed = (t.finished_at - t.started_at).total_seconds()
                if elapsed > 0:
                    ratios.append(float(t.duration_sec) / elapsed)

        return sum(ratios) / len(ratios) if ratios else 1.0
    except Exception as exc:
        logger.warning("Could not calculate recent_speed: %s", exc)
        return 1.0


def estimate_wait_sec(db: Session, r: Optional[redis.Redis], task: Task) -> int:
    """
    Calculate estimated wait time in seconds for a queued task:
    (ahead + running_left) / (speed * max(alive_workers, 1))
    """
    try:
        ahead = db.scalar(
            select(func.coalesce(func.sum(Task.duration_sec), 0.0))
            .where(
                Task.status == TaskStatus.queued,
                Task.id != task.id,
                Task.created_at < task.created_at,
            )
        ) or 0.0

        running_tasks = db.scalars(
            select(Task).where(Task.status == TaskStatus.running)
        ).all()

        running_left = 0.0
        for t in running_tasks:
            t_dur = float(t.duration_sec or 0.0)
            pct = 0
            if r:
                try:
                    prog_val = r.hget(f"task:{t.id}", "progress")
                    if prog_val:
                        pct = int(prog_val)
                except Exception:
                    pct = 0
            running_left += t_dur * (1.0 - min(100, max(0, pct)) / 100.0)

        speed = recent_speed(db) or 1.0
        workers_count = max(alive_workers(r), 1)

        wait_sec = int((float(ahead) + float(running_left)) / (speed * workers_count))
        return max(0, wait_sec)
    except Exception as exc:
        logger.warning("Could not estimate wait time for task %s: %s", task.id, exc)
        return 0


def get_queue_position(db: Session, task: Task) -> int:
    """Return 1-based position in queue among tasks created before this one."""
    count = db.scalar(
        select(func.count(Task.id))
        .where(
            Task.status == TaskStatus.queued,
            Task.id != task.id,
            Task.created_at < task.created_at,
        )
    ) or 0
    return count + 1


def enrich_task_out(task: Task, db: Session, r: Optional[redis.Redis]) -> Task:
    """Populate queue_position and eta_sec on Task object for API responses."""
    if task.status == TaskStatus.queued:
        task.queue_position = get_queue_position(db, task)
        task.eta_sec = estimate_wait_sec(db, r, task)
    else:
        task.queue_position = None
        task.eta_sec = None
    return task
