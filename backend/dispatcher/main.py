import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import shutil
import uuid
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel
import redis
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import Task, TaskStatus
from dispatcher.client import WorkerClient
from dispatcher.registry import WorkerInfo, WorkerRegistry
from dispatcher.strategies import get_balancing_strategy
from worker_server.ffmpeg_runner import get_output_extension

logger = logging.getLogger("dispatcher")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] (dispatcher) %(message)s")

DISPATCHER_PORT = int(os.getenv("DISPATCHER_PORT", "8100"))
DISPATCHER_URL = os.getenv("DISPATCHER_URL", f"http://dispatcher:{DISPATCHER_PORT}")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "super_secret_worker_token")
BALANCING_STRATEGY = os.getenv("BALANCING_STRATEGY", "least_loaded")
DISPATCH_INTERVAL = float(os.getenv("DISPATCH_INTERVAL", "1.0"))
WORKER_POLL_INTERVAL = float(os.getenv("WORKER_POLL_INTERVAL", "3.0"))
WORKER_FAIL_THRESHOLD = int(os.getenv("WORKER_FAIL_THRESHOLD", "3"))
MAX_TASK_ATTEMPTS = int(os.getenv("MAX_TASK_ATTEMPTS", "3"))

if WORKER_TOKEN in ["", "super_secret_worker_token", "default_token", "change_me"]:
    logger.warning("WORKER_TOKEN is using an insecure or default placeholder value! Change in production.")

registry = WorkerRegistry()
strategy = get_balancing_strategy(BALANCING_STRATEGY)
worker_client = WorkerClient(worker_token=WORKER_TOKEN)
active_tasks: Dict[str, str] = {}  # task_id -> worker_id

dispatch_task_handle: Optional[asyncio.Task] = None
monitor_task_handle: Optional[asyncio.Task] = None
health_task_handle: Optional[asyncio.Task] = None


session_factory = SessionLocal


def get_redis_client() -> Optional[redis.Redis]:
    try:
        return redis.Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception as exc:
        logger.debug("Could not connect to Redis: %s", exc)
        return None


redis_getter = get_redis_client


def verify_worker_token(x_worker_token: Optional[str] = Header(None, alias="X-Worker-Token")):
    if not WORKER_TOKEN or x_worker_token != WORKER_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Worker-Token header",
        )


def log_dispatch_event(
    r: Optional[redis.Redis],
    event: str,
    task_id: Optional[str] = None,
    worker_id: Optional[str] = None,
    detail: Optional[str] = None,
):
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "task_id": str(task_id) if task_id else None,
        "worker_id": str(worker_id) if worker_id else None,
        "detail": detail or "",
    }
    if r:
        try:
            r.lpush("dispatcher:log", json.dumps(payload))
            r.ltrim("dispatcher:log", 0, 99)
        except Exception as exc:
            logger.debug("Could not push event to Redis dispatcher:log: %s", exc)


def _requeue_or_fail_task_sync(task_id: str, reason: str):
    worker_id = active_tasks.pop(task_id, None)
    if worker_id:
        registry.unassign_task(worker_id, task_id)

    r = redis_getter()
    attempts = 1
    if r:
        try:
            attempts = r.hincrby(f"task:{task_id}", "attempts", 1)
        except Exception:
            pass

    db = session_factory()
    try:
        t_uuid = uuid.UUID(task_id)
        task = db.query(Task).filter(Task.id == t_uuid).first()
        if not task:
            return

        if attempts > MAX_TASK_ATTEMPTS:
            msg = f"Task failed after exceeding {MAX_TASK_ATTEMPTS} attempts: {reason}"
            task.status = TaskStatus.failed
            task.error_message = msg
            task.finished_at = datetime.now(timezone.utc)
            db.commit()
            if r:
                r.hset(
                    f"task:{task_id}",
                    mapping={"status": "failed", "error": msg, "progress": 0},
                )
            log_dispatch_event(r, "failed", task_id, worker_id, msg)
            logger.warning("Task %s failed permanently: %s", task_id, msg)
        else:
            task.status = TaskStatus.queued
            task.worker_id = None
            task.started_at = None
            db.commit()
            if r:
                r.hset(
                    f"task:{task_id}",
                    mapping={"status": "queued", "progress": 0, "worker_id": ""},
                )
            detail = f"Requeued attempt {attempts}/{MAX_TASK_ATTEMPTS}: {reason}"
            log_dispatch_event(r, "requeued", task_id, worker_id, detail)
            logger.info("Task %s requeued: %s", task_id, detail)
    except Exception as exc:
        db.rollback()
        logger.error("Error in _requeue_or_fail_task_sync for %s: %s", task_id, exc)
    finally:
        db.close()


def recover_running_tasks_sync():
    """Recover unmonitored running tasks back to queued status on startup."""
    db = session_factory()
    r = redis_getter()
    try:
        running_tasks = db.query(Task).filter(Task.status == TaskStatus.running).all()
        count = len(running_tasks)
        for t in running_tasks:
            t.status = TaskStatus.queued
            t.worker_id = None
            t.started_at = None
            if r:
                try:
                    r.hset(
                        f"task:{t.id}",
                        mapping={"status": "queued", "progress": 0, "worker_id": ""},
                    )
                except Exception:
                    pass
        db.commit()
        if count > 0:
            logger.info("Recovered %d running tasks back to queued status on Dispatcher startup.", count)
    except Exception as exc:
        db.rollback()
        logger.error("Error during startup task recovery: %s", exc)
    finally:
        db.close()


class WorkerRegisterRequest(BaseModel):
    worker_id: str
    url: str
    capacity: int = 1


class WorkerDeregisterRequest(BaseModel):
    worker_id: str


async def dispatch_cycle():
    """Single pass of the task distribution cycle."""
    def get_queued_tasks_sync() -> List[dict]:
        db = session_factory()
        try:
            tasks_list = (
                db.query(Task)
                .filter(Task.status == TaskStatus.queued)
                .order_by(Task.created_at.asc())
                .all()
            )
            res = []
            for t in tasks_list:
                res.append({
                    "id": str(t.id),
                    "operation_type": t.operation_type,
                    "params": t.params or {},
                    "input_file": t.input_file,
                    "duration_sec": t.duration_sec,
                })
            return res
        finally:
            db.close()

    queued_tasks = await asyncio.to_thread(get_queued_tasks_sync)
    if not queued_tasks:
        return

    r = redis_getter()

    for item in queued_tasks:
        task_id = item["id"]

        # Re-check task status from database
        def is_still_queued_sync() -> bool:
            db = session_factory()
            try:
                t = db.query(Task).filter(Task.id == uuid.UUID(task_id)).first()
                return t is not None and t.status == TaskStatus.queued
            finally:
                db.close()

        if not await asyncio.to_thread(is_still_queued_sync):
            continue

        # Prepare parameters and logo
        params = dict(item["params"])
        params.pop("logo_path", None)

        logo_candidate = os.path.join(settings.upload_dir, f"{task_id}_logo.png")
        logo_path = logo_candidate if os.path.exists(logo_candidate) else None

        input_file = item["input_file"]
        if not input_file or not os.path.exists(input_file):
            logger.error("Input file %s for task %s not found on disk. Failing task.", input_file, task_id)
            await asyncio.to_thread(_requeue_or_fail_task_sync, task_id, "Input file missing from storage")
            continue

        task_dispatched = False
        tried_worker_ids = set()

        while True:
            candidates = [
                w for w in registry.get_alive_workers()
                if w.is_available() and w.worker_id not in tried_worker_ids
            ]
            if not candidates:
                break

            selected_worker = strategy.select_worker(candidates)
            if not selected_worker:
                break

            tried_worker_ids.add(selected_worker.worker_id)

            try:
                status_code, resp = await worker_client.execute(
                    worker_url=selected_worker.url,
                    task_id=task_id,
                    operation_type=item["operation_type"],
                    params=params,
                    duration_sec=item["duration_sec"],
                    input_file_path=input_file,
                    logo_path=logo_path,
                )
            except Exception as exc:
                registry.record_failure(selected_worker.worker_id, threshold=WORKER_FAIL_THRESHOLD)
                logger.warning("Error dispatching task %s to worker %s: %s", task_id, selected_worker.worker_id, exc)
                continue

            if status_code in [200, 202]:
                started_at = datetime.now(timezone.utc)

                def mark_task_running_sync():
                    db = session_factory()
                    try:
                        t = db.query(Task).filter(Task.id == uuid.UUID(task_id)).first()
                        if t:
                            t.status = TaskStatus.running
                            t.worker_id = selected_worker.worker_id
                            t.started_at = started_at
                            db.commit()
                    except Exception as exc:
                        db.rollback()
                        logger.error("Error setting running state for %s: %s", task_id, exc)
                    finally:
                        db.close()

                await asyncio.to_thread(mark_task_running_sync)

                if r:
                    try:
                        r.hset(
                            f"task:{task_id}",
                            mapping={
                                "status": "running",
                                "progress": 0,
                                "worker_id": selected_worker.worker_id,
                                "started_at": started_at.isoformat(),
                            },
                        )
                    except Exception:
                        pass

                active_tasks[task_id] = selected_worker.worker_id
                registry.assign_task(selected_worker.worker_id, task_id)

                load_str = f"{len(selected_worker.active_task_ids)}/{selected_worker.capacity}"
                detail = f"dispatch task={task_id} -> worker={selected_worker.worker_id} strategy={BALANCING_STRATEGY} load={load_str}"
                logger.info(detail)
                log_dispatch_event(r, "dispatched", task_id, selected_worker.worker_id, detail)
                task_dispatched = True
                break

            elif status_code in [503, 507]:
                logger.info("Worker %s busy or full disk (%d), trying next candidate", selected_worker.worker_id, status_code)
                continue
            else:
                registry.record_failure(selected_worker.worker_id, threshold=WORKER_FAIL_THRESHOLD)
                logger.warning("Worker %s rejected task %s with status %d", selected_worker.worker_id, task_id, status_code)
                continue

        if not task_dispatched:
            any_available = any(w.is_available() for w in registry.get_alive_workers())
            if not any_available:
                break


async def monitor_cycle():
    """Single pass of active tasks status polling and completion handling."""
    if not active_tasks:
        return

    r = redis_getter()

    for task_id, worker_id in list(active_tasks.items()):
        worker = registry.get_worker(worker_id)
        if not worker or worker.state == "dead":
            continue

        # Check cooperative cancellation requested via Redis
        if r and r.exists(f"cancel:{task_id}"):
            try:
                await worker_client.cancel(worker.url, task_id)
            except Exception as exc:
                logger.debug("Error requesting cancellation for %s: %s", task_id, exc)

        # Check cancellation in DB
        def check_db_cancellation_sync() -> bool:
            db = session_factory()
            try:
                t = db.query(Task).filter(Task.id == uuid.UUID(task_id)).first()
                return t is not None and t.status == TaskStatus.cancelled
            finally:
                db.close()

        if await asyncio.to_thread(check_db_cancellation_sync):
            try:
                await worker_client.cancel(worker.url, task_id)
            except Exception:
                pass

        try:
            status_code, resp = await worker_client.get_progress(worker.url, task_id)
        except Exception as exc:
            logger.debug("Failed polling progress for %s from %s: %s", task_id, worker.url, exc)
            continue

        if status_code == 404:
            logger.warning("Worker %s lost task %s (404); requeuing.", worker_id, task_id)
            await asyncio.to_thread(_requeue_or_fail_task_sync, task_id, "Worker lost task state")
            continue

        if status_code != 200:
            continue

        task_status = resp.get("status")
        progress_val = resp.get("progress", 0)

        # Sync progress to Redis for live SSE
        if r:
            try:
                r.hset(
                    f"task:{task_id}",
                    mapping={
                        "status": task_status,
                        "progress": progress_val,
                        "worker_id": worker_id,
                    },
                )
            except Exception:
                pass

        if task_status == "done":
            def get_task_info_sync():
                db = session_factory()
                try:
                    t = db.query(Task).filter(Task.id == uuid.UUID(task_id)).first()
                    if t:
                        return {"operation_type": t.operation_type, "params": t.params}
                    return {}
                finally:
                    db.close()

            task_info = await asyncio.to_thread(get_task_info_sync)
            ext = get_output_extension(task_info)
            target_path = os.path.join(settings.output_dir, f"{task_id}{ext}")

            downloaded = await worker_client.download_result(worker.url, task_id, target_path)
            if downloaded:
                finished_at = datetime.now(timezone.utc)
                expires_at = finished_at + timedelta(hours=settings.result_lifetime_hours)

                def update_done_sync():
                    db = session_factory()
                    try:
                        t = db.query(Task).filter(Task.id == uuid.UUID(task_id)).first()
                        if t:
                            t.status = TaskStatus.done
                            t.output_file = target_path
                            t.finished_at = finished_at
                            t.expires_at = expires_at
                            db.commit()
                    except Exception as exc:
                        db.rollback()
                        logger.error("Error updating done task in DB: %s", exc)
                    finally:
                        db.close()

                await asyncio.to_thread(update_done_sync)

                if r:
                    try:
                        r.hset(
                            f"task:{task_id}",
                            mapping={
                                "status": "done",
                                "progress": 100,
                                "output_file": target_path,
                                "finished_at": finished_at.isoformat(),
                                "expires_at": expires_at.isoformat(),
                            },
                        )
                    except Exception:
                        pass

                active_tasks.pop(task_id, None)
                registry.unassign_task(worker_id, task_id)
                await worker_client.delete_task(worker.url, task_id)
                log_dispatch_event(r, "completed", task_id, worker_id, f"Completed in {target_path}")
                logger.info("Task %s completed and saved to %s", task_id, target_path)
            else:
                await asyncio.to_thread(_requeue_or_fail_task_sync, task_id, "Failed to download output file from worker")

        elif task_status == "failed":
            error_msg = resp.get("error", "Task processing failed on worker")

            def update_failed_sync():
                db = session_factory()
                try:
                    t = db.query(Task).filter(Task.id == uuid.UUID(task_id)).first()
                    if t:
                        t.status = TaskStatus.failed
                        t.error_message = error_msg
                        t.finished_at = datetime.now(timezone.utc)
                        db.commit()
                except Exception as exc:
                    db.rollback()
                    logger.error("Error updating failed task in DB: %s", exc)
                finally:
                    db.close()

            await asyncio.to_thread(update_failed_sync)

            if r:
                try:
                    r.hset(
                        f"task:{task_id}",
                        mapping={"status": "failed", "error": error_msg},
                    )
                except Exception:
                    pass

            active_tasks.pop(task_id, None)
            registry.unassign_task(worker_id, task_id)
            await worker_client.delete_task(worker.url, task_id)
            log_dispatch_event(r, "failed", task_id, worker_id, error_msg)

        elif task_status == "cancelled":
            def update_cancelled_sync():
                db = session_factory()
                try:
                    t = db.query(Task).filter(Task.id == uuid.UUID(task_id)).first()
                    if t:
                        t.status = TaskStatus.cancelled
                        t.error_message = "Task cancelled by user"
                        t.finished_at = datetime.now(timezone.utc)
                        db.commit()
                except Exception as exc:
                    db.rollback()
                    logger.error("Error updating cancelled task in DB: %s", exc)
                finally:
                    db.close()

            await asyncio.to_thread(update_cancelled_sync)

            if r:
                try:
                    r.hset(
                        f"task:{task_id}",
                        mapping={"status": "cancelled", "finished_at": datetime.now(timezone.utc).isoformat()},
                    )
                except Exception:
                    pass

            active_tasks.pop(task_id, None)
            registry.unassign_task(worker_id, task_id)
            await worker_client.delete_task(worker.url, task_id)
            log_dispatch_event(r, "cancelled", task_id, worker_id, "Cancelled by user")


async def health_check_cycle():
    """Poll workers in parallel and publish heartbeat info to Redis."""
    workers = registry.get_all_workers()
    if not workers:
        return

    r = redis_getter()

    async def check_single_worker(w: WorkerInfo):
        try:
            status_code, resp = await worker_client.get_status(w.url, timeout=2.0)
            if status_code == 200:
                cpu = float(resp.get("cpu_percent", 0.0))
                ram = float(resp.get("ram_percent", 0.0))
                active_ids = resp.get("active_task_ids", [])
                registry.record_success(w.worker_id, cpu=cpu, ram=ram, active_task_ids=active_ids)

                if r:
                    hb_key = f"hb:{w.worker_id}"
                    legacy_key = f"worker:heartbeat:{w.worker_id}"
                    current_task = active_ids[0] if active_ids else ""
                    now_str = datetime.now(timezone.utc).isoformat()
                    try:
                        r.hset(
                            hb_key,
                            mapping={
                                "hostname": w.worker_id,
                                "cpu": str(cpu),
                                "ram": str(ram),
                                "current_task": current_task,
                                "capacity": str(w.capacity),
                                "timestamp": now_str,
                            },
                        )
                        r.expire(hb_key, 10)
                        r.set(legacy_key, 1, ex=10)
                    except Exception:
                        pass
                return
        except Exception as exc:
            logger.debug("Health check failed for worker %s: %s", w.worker_id, exc)

        is_dead = registry.record_failure(w.worker_id, threshold=WORKER_FAIL_THRESHOLD)
        if is_dead:
            logger.warning("Worker %s is DEAD after %d consecutive failures.", w.worker_id, WORKER_FAIL_THRESHOLD)
            if r:
                try:
                    r.delete(f"hb:{w.worker_id}", f"worker:heartbeat:{w.worker_id}")
                except Exception:
                    pass
            log_dispatch_event(
                r,
                "worker_dead",
                worker_id=w.worker_id,
                detail=f"Worker marked dead after {WORKER_FAIL_THRESHOLD} failures",
            )
            for t_id in list(w.active_task_ids):
                await asyncio.to_thread(
                    _requeue_or_fail_task_sync,
                    t_id,
                    f"Worker {w.worker_id} died",
                )
            registry.deregister(w.worker_id)

    await asyncio.gather(*(check_single_worker(w) for w in workers), return_exceptions=True)


async def _background_dispatch_loop():
    while True:
        try:
            await dispatch_cycle()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Error in dispatch_loop: %s", exc)

        try:
            await asyncio.sleep(DISPATCH_INTERVAL)
        except asyncio.CancelledError:
            break


async def _background_monitor_loop():
    while True:
        try:
            await monitor_cycle()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Error in monitor_loop: %s", exc)

        try:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            break


async def _background_health_loop():
    while True:
        try:
            await health_check_cycle()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Error in health_loop: %s", exc)

        try:
            await asyncio.sleep(WORKER_POLL_INTERVAL)
        except asyncio.CancelledError:
            break


@asynccontextmanager
async def lifespan(app: FastAPI):
    global dispatch_task_handle, monitor_task_handle, health_task_handle

    os.makedirs(settings.upload_dir, exist_ok=True)
    os.makedirs(settings.output_dir, exist_ok=True)
    os.makedirs(settings.log_dir, exist_ok=True)

    # Recover any running tasks back to queued status on startup
    await asyncio.to_thread(recover_running_tasks_sync)

    dispatch_task_handle = asyncio.create_task(_background_dispatch_loop())
    monitor_task_handle = asyncio.create_task(_background_monitor_loop())
    health_task_handle = asyncio.create_task(_background_health_loop())

    yield

    for handle in [dispatch_task_handle, monitor_task_handle, health_task_handle]:
        if handle:
            handle.cancel()
            try:
                await handle
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="CVP Dispatcher Service",
    description="Task Dispatcher & Load Balancer for Cloud Video Processor",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", summary="Health check endpoint (no auth required)")
def health_check():
    return {"status": "ok"}


@app.post(
    "/internal/workers/register",
    dependencies=[Depends(verify_worker_token)],
    summary="Worker self-registration endpoint",
)
def register_worker(req: WorkerRegisterRequest):
    w = registry.register(worker_id=req.worker_id, url=req.url, capacity=req.capacity)
    r = redis_getter()
    log_dispatch_event(
        r,
        "worker_registered",
        worker_id=req.worker_id,
        detail=f"Registered worker={req.worker_id} url={req.url} capacity={req.capacity}",
    )
    logger.info("Registered worker %s at %s (capacity=%d)", req.worker_id, req.url, req.capacity)
    return {"status": "ok", "worker": w.to_dict()}


@app.post(
    "/internal/workers/deregister",
    dependencies=[Depends(verify_worker_token)],
    summary="Worker deregistration endpoint",
)
def deregister_worker(req: WorkerDeregisterRequest):
    removed = registry.deregister(req.worker_id)
    r = redis_getter()
    if r:
        try:
            r.delete(f"hb:{req.worker_id}", f"worker:heartbeat:{req.worker_id}")
        except Exception:
            pass

    if removed:
        log_dispatch_event(
            r,
            "worker_deregistered",
            worker_id=req.worker_id,
            detail=f"Deregistered worker={req.worker_id}",
        )
        logger.info("Deregistered worker %s", req.worker_id)
    return {"status": "ok"}


@app.get(
    "/internal/workers",
    dependencies=[Depends(verify_worker_token)],
    summary="List registered workers for debugging",
)
def list_workers():
    workers = [w.to_dict() for w in registry.get_all_workers()]
    return {
        "status": "ok",
        "strategy": BALANCING_STRATEGY,
        "total_workers": len(workers),
        "active_tasks_dispatched": len(active_tasks),
        "workers": workers,
    }
