from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
from typing import Dict, List, Optional, Set


@dataclass
class WorkerInfo:
    worker_id: str
    url: str
    capacity: int = 1
    active_task_ids: Set[str] = field(default_factory=set)
    state: str = "alive"
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    consecutive_failures: int = 0
    cpu: float = 0.0
    ram: float = 0.0

    def is_available(self) -> bool:
        return self.state == "alive" and len(self.active_task_ids) < max(1, self.capacity)

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "url": self.url,
            "capacity": self.capacity,
            "active_tasks_count": len(self.active_task_ids),
            "active_task_ids": list(self.active_task_ids),
            "state": self.state,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "consecutive_failures": self.consecutive_failures,
            "cpu": self.cpu,
            "ram": self.ram,
        }


class WorkerRegistry:
    def __init__(self):
        self._workers: Dict[str, WorkerInfo] = {}
        self._lock = threading.Lock()

    def register(self, worker_id: str, url: str, capacity: int = 1) -> WorkerInfo:
        with self._lock:
            existing = self._workers.get(worker_id)
            if existing:
                existing.url = url
                existing.capacity = max(1, capacity)
                existing.state = "alive"
                existing.last_seen = datetime.now(timezone.utc)
                existing.consecutive_failures = 0
                return existing

            new_worker = WorkerInfo(
                worker_id=worker_id,
                url=url,
                capacity=max(1, capacity),
            )
            self._workers[worker_id] = new_worker
            return new_worker

    def deregister(self, worker_id: str) -> Optional[WorkerInfo]:
        with self._lock:
            return self._workers.pop(worker_id, None)

    def get_worker(self, worker_id: str) -> Optional[WorkerInfo]:
        with self._lock:
            return self._workers.get(worker_id)

    def get_all_workers(self) -> List[WorkerInfo]:
        with self._lock:
            return list(self._workers.values())

    def get_alive_workers(self) -> List[WorkerInfo]:
        with self._lock:
            return [w for w in self._workers.values() if w.state == "alive"]

    def assign_task(self, worker_id: str, task_id: str) -> bool:
        with self._lock:
            w = self._workers.get(worker_id)
            if w:
                w.active_task_ids.add(task_id)
                return True
            return False

    def unassign_task(self, worker_id: str, task_id: str) -> None:
        with self._lock:
            w = self._workers.get(worker_id)
            if w and task_id in w.active_task_ids:
                w.active_task_ids.remove(task_id)

    def record_success(
        self,
        worker_id: str,
        cpu: Optional[float] = None,
        ram: Optional[float] = None,
        active_task_ids: Optional[List[str]] = None,
    ) -> None:
        with self._lock:
            w = self._workers.get(worker_id)
            if w:
                w.state = "alive"
                w.last_seen = datetime.now(timezone.utc)
                w.consecutive_failures = 0
                if cpu is not None:
                    w.cpu = cpu
                if ram is not None:
                    w.ram = ram
                if active_task_ids is not None:
                    # Sync active tasks with worker's report
                    w.active_task_ids = set(active_task_ids)

    def record_failure(self, worker_id: str, threshold: int = 3) -> bool:
        """Increment failure count. Mark dead if threshold reached. Return True if dead."""
        with self._lock:
            w = self._workers.get(worker_id)
            if not w:
                return False

            w.consecutive_failures += 1
            if w.consecutive_failures >= threshold:
                w.state = "dead"
                return True
            return False
