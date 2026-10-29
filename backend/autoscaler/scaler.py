from datetime import datetime, timezone
import json
import logging
import os
import socket
import time
from typing import Any, Dict, List, Optional

import redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import Task, TaskStatus

logger = logging.getLogger("autoscaler")


class Autoscaler:
    """
    Autoscaling controller for Celery workers:
    - Monitors database queued tasks count
    - Scales up when queued > workers * PER_WORKER (up to MAX_WORKERS)
    - Scales down idle autoscaled workers when queued == 0 after idle_for seconds (down to MIN_WORKERS)
    - Logs scaling events to Redis list 'autoscaler:log'
    """

    def __init__(
        self,
        docker_client=None,
        redis_client: Optional[redis.Redis] = None,
        min_workers: int = 2,
        max_workers: int = 4,
        per_worker: int = 2,
        idle_for: int = 60,
    ):
        self.client = docker_client
        self.r = redis_client
        self.min_workers = min_workers
        self.max_workers = max_workers
        self.per_worker = per_worker
        self.idle_for = idle_for
        self.idle_since: Dict[str, float] = {}

    def get_redis(self) -> redis.Redis:
        if self.r is None:
            self.r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        return self.r

    def count_queued(self, db: Optional[Session] = None) -> int:
        """Count number of tasks currently in queued status."""
        should_close = False
        if db is None:
            db = SessionLocal()
            should_close = True
        try:
            return db.scalar(
                select(func.count(Task.id)).where(Task.status == TaskStatus.queued)
            ) or 0
        finally:
            if should_close:
                db.close()

    def log_event(self, action: str, count: int, extra: Optional[Dict[str, Any]] = None) -> None:
        """Append scaling event to Redis list 'autoscaler:log'."""
        try:
            r = self.get_redis()
            event = {
                "action": action,
                "workers": count,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **(extra or {}),
            }
            r.rpush("autoscaler:log", json.dumps(event))
            r.ltrim("autoscaler:log", -100, -1)
            logger.info("Autoscaler event logged: %s (current workers: %d)", action, count)
        except Exception as exc:
            logger.warning("Could not log autoscaler event: %s", exc)

    def get_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Retrieve recent autoscaler log events from Redis."""
        try:
            r = self.get_redis()
            raw_list = r.lrange("autoscaler:log", -limit, -1) or []
            events = []
            for item in raw_list:
                try:
                    events.append(json.loads(item))
                except Exception:
                    pass
            return events
        except Exception:
            return []

    def get_worker_heartbeats(self) -> List[Dict[str, Any]]:
        """Read all active hb:* keys from Redis."""
        try:
            r = self.get_redis()
            keys = r.keys("hb:*") or []
            results = []
            for k in keys:
                h = r.hgetall(k)
                if h:
                    results.append(h)
            return results
        except Exception:
            return []

    def discover_worker_params(self, workers: list) -> tuple:
        """Discover image, network, volume, and environment from existing containers or env."""
        worker_image = os.getenv("WORKER_IMAGE", "cloud-video-processor-worker")
        if workers and getattr(workers[0], "image", None):
            tags = getattr(workers[0].image, "tags", [])
            if tags:
                worker_image = tags[0]
            else:
                worker_image = getattr(workers[0].image, "id", worker_image)

        network = os.getenv("DOCKER_NETWORK")
        if not network and workers:
            try:
                nets = workers[0].attrs.get("NetworkSettings", {}).get("Networks", {})
                if nets:
                    network = list(nets.keys())[0]
            except Exception:
                pass
        if not network:
            network = "cloud-video-processor_default"

        media_volume = os.getenv("MEDIA_VOLUME")
        if not media_volume and workers:
            try:
                mounts = workers[0].attrs.get("Mounts", [])
                for m in mounts:
                    if m.get("Destination") == "/data":
                        media_volume = m.get("Name") or m.get("Source")
                        break
            except Exception:
                pass
        if not media_volume:
            media_volume = "cloud-video-processor_media"

        worker_env = {
            "DATABASE_URL": os.getenv("DATABASE_URL", ""),
            "REDIS_URL": os.getenv("REDIS_URL", "redis://redis:6379/0"),
            "UPLOAD_DIR": os.getenv("UPLOAD_DIR", "/data/uploads"),
            "OUTPUT_DIR": os.getenv("OUTPUT_DIR", "/data/outputs"),
            "LOG_DIR": os.getenv("LOG_DIR", "/data/logs"),
            "CVP_ROLE": "worker",
        }
        if workers:
            try:
                w_env_list = workers[0].attrs.get("Config", {}).get("Env", [])
                for item in w_env_list:
                    if "=" in item:
                        k, v = item.split("=", 1)
                        if k not in ["HOSTNAME", "HOME"]:
                            worker_env[k] = v
            except Exception:
                pass

        return worker_image, network, media_volume, worker_env

    def is_worker_busy(self, worker) -> bool:
        """Check whether a worker has an active task running via Redis heartbeat."""
        try:
            r = self.get_redis()
            hostname = getattr(worker, "name", None) or getattr(worker, "id", "")[:12]
            h = r.hgetall(f"hb:{hostname}")
            if h and h.get("current_task"):
                return True
            short_id = getattr(worker, "id", "")[:12]
            if short_id:
                h_short = r.hgetall(f"hb:{short_id}")
                if h_short and h_short.get("current_task"):
                    return True
        except Exception:
            pass
        return False

    def stop_idle_autoscaled(self, workers: list, idle_for: Optional[int] = None) -> int:
        """
        Stop autoscaled workers (with label cvp.autoscaled="1") that have no running task
        and have been idle for at least idle_for seconds.
        Ensures total workers count does not drop below min_workers.
        Returns number of stopped workers.
        """
        if idle_for is None:
            idle_for = self.idle_for

        stopped_count = 0
        current_worker_ids = {w.id for w in workers if hasattr(w, "id")}

        for tracked_id in list(self.idle_since.keys()):
            if tracked_id not in current_worker_ids:
                del self.idle_since[tracked_id]

        total_workers = len(workers)
        now = time.time()

        for w in list(workers):
            labels = getattr(w, "labels", {}) or {}
            if labels.get("cvp.autoscaled") != "1":
                continue

            if total_workers - stopped_count <= self.min_workers:
                break

            busy = self.is_worker_busy(w)
            w_id = getattr(w, "id", str(id(w)))

            if busy:
                self.idle_since.pop(w_id, None)
                continue

            if w_id not in self.idle_since:
                self.idle_since[w_id] = now
            else:
                elapsed = now - self.idle_since[w_id]
                if elapsed >= idle_for:
                    logger.info(
                        "Stopping idle autoscaled worker %s (idle for %.1fs, threshold %ds)",
                        getattr(w, "name", w_id[:12]),
                        elapsed,
                        idle_for,
                    )
                    try:
                        w.stop(timeout=10)
                        w.remove()
                    except Exception as exc:
                        logger.warning("Error stopping container %s: %s", w_id, exc)

                    self.idle_since.pop(w_id, None)
                    stopped_count += 1
                    self.log_event("scale_down", total_workers - stopped_count)

        return stopped_count

    def step(self, db: Optional[Session] = None) -> Dict[str, Any]:
        """
        Execute one evaluation step of the autoscaling algorithm:
        1. Count queued tasks.
        2. Query existing worker containers.
        3. If queued > n * per_worker and n < max_workers -> scale up.
        4. If queued == 0 -> stop idle autoscaled workers.
        Returns step summary dict.
        """
        if self.client is None:
            return {"status": "no_docker_client", "workers": 0, "queued": 0}

        queued = self.count_queued(db)
        try:
            workers = self.client.containers.list(filters={"label": "cvp.role=worker"})
        except Exception as exc:
            logger.warning("Could not list containers from Docker daemon: %s", exc)
            return {"status": "docker_error", "error": str(exc), "queued": queued}

        n = len(workers)
        action_taken = "none"

        if queued > n * self.per_worker and n < self.max_workers:
            image, network, media_volume, env = self.discover_worker_params(workers)
            worker_cmd = ["celery", "-A", "worker.app", "worker", "--concurrency=1", "--loglevel=info"]

            try:
                self.client.containers.run(
                    image,
                    worker_cmd,
                    detach=True,
                    network=network,
                    labels={"cvp.role": "worker", "cvp.autoscaled": "1"},
                    environment=env,
                    volumes={media_volume: {"bind": "/data", "mode": "rw"}},
                    nano_cpus=2_000_000_000,
                )
                action_taken = "scale_up"
                self.log_event("scale_up", n + 1, {"queued": queued})
                n += 1
            except Exception as exc:
                logger.error("Failed to run autoscaled worker container: %s", exc)
                action_taken = f"scale_up_failed: {exc}"

        elif queued == 0:
            stopped = self.stop_idle_autoscaled(workers, idle_for=self.idle_for)
            if stopped > 0:
                action_taken = f"scaled_down_{stopped}"
                n -= stopped

        return {
            "status": "ok",
            "action": action_taken,
            "queued": queued,
            "workers": n,
            "min_workers": self.min_workers,
            "max_workers": self.max_workers,
            "per_worker": self.per_worker,
        }
