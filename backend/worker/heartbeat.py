from datetime import datetime, timezone
import logging
import socket
import threading
import time
from typing import Optional

import psutil
import redis
from celery.signals import task_postrun, task_prerun, worker_ready, worker_shutdown

from app.config import settings

logger = logging.getLogger(__name__)

_current_task_id: Optional[str] = None
_task_lock = threading.Lock()

_heartbeat_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def set_current_task(task_id: Optional[str]) -> None:
    """Thread-safe update of current task id executing on this worker."""
    global _current_task_id
    with _task_lock:
        _current_task_id = str(task_id) if task_id else None


def get_current_task() -> Optional[str]:
    """Retrieve current task id executing on this worker."""
    global _current_task_id
    with _task_lock:
        return _current_task_id


def _heartbeat_loop(stop_event: threading.Event) -> None:
    """
    Background heartbeat thread loop:
    Every 3 seconds writes hb:{hostname} into Redis with TTL 10 seconds:
    - hostname
    - cpu percentage (psutil)
    - ram percentage (psutil)
    - current_task (task id or empty string)
    - timestamp (UTC ISO format)
    """
    r = None
    try:
        r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception as exc:
        logger.warning("Heartbeat thread could not connect to Redis: %s", exc)
        return

    hostname = socket.gethostname()
    key = f"hb:{hostname}"
    legacy_key = f"worker:heartbeat:{hostname}"

    # Initialize psutil cpu percent measurement
    try:
        psutil.cpu_percent(interval=None)
    except Exception:
        pass

    while not stop_event.is_set():
        try:
            cpu = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory().percent
            task = get_current_task() or ""
            timestamp = datetime.now(timezone.utc).isoformat()

            r.hset(
                key,
                mapping={
                    "hostname": hostname,
                    "cpu": str(cpu),
                    "ram": str(ram),
                    "current_task": task,
                    "timestamp": timestamp,
                },
            )
            r.expire(key, 10)
            # Legacy heartbeat key for compatibility
            r.set(legacy_key, 1, ex=10)
        except Exception as exc:
            logger.warning("Worker heartbeat write failed: %s", exc)

        stop_event.wait(3.0)


def start_heartbeat() -> None:
    """Start background heartbeat thread if not already running."""
    global _heartbeat_thread, _stop_event
    if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
        return
    _stop_event.clear()
    _heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(_stop_event,),
        name="WorkerHeartbeatThread",
        daemon=True,
    )
    _heartbeat_thread.start()
    logger.info("Worker heartbeat thread started for hostname: %s", socket.gethostname())


def stop_heartbeat() -> None:
    """Signal heartbeat thread to terminate."""
    global _stop_event
    _stop_event.set()


@worker_ready.connect
def on_worker_ready(sender, **kwargs):
    """Triggered by Celery when worker is ready to receive tasks."""
    start_heartbeat()


@worker_shutdown.connect
def on_worker_shutdown(sender, **kwargs):
    """Triggered by Celery when worker is shutting down."""
    stop_heartbeat()
    try:
        r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        hostname = socket.gethostname()
        r.delete(f"hb:{hostname}", f"worker:heartbeat:{hostname}")
    except Exception:
        pass


@task_prerun.connect
def on_task_prerun(task_id, task, **kwargs):
    """Triggered before task begins executing."""
    set_current_task(str(task_id))


@task_postrun.connect
def on_task_postrun(task_id, task, **kwargs):
    """Triggered after task finishes executing."""
    set_current_task(None)
