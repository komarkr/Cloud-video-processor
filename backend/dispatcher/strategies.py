from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from dispatcher.registry import WorkerInfo


class BalancingStrategy(ABC):
    """Abstract base class for worker selection algorithms."""

    @abstractmethod
    def select_worker(self, candidates: List["WorkerInfo"]) -> Optional["WorkerInfo"]:
        pass


class LeastLoadedStrategy(BalancingStrategy):
    """
    Selects worker with minimum load ratio (active_tasks / capacity).
    Ties broken by lower CPU usage, then worker_id alphabetically.
    """

    def select_worker(self, candidates: List["WorkerInfo"]) -> Optional["WorkerInfo"]:
        available = [w for w in candidates if w.is_available()]
        if not available:
            return None

        return min(
            available,
            key=lambda w: (
                len(w.active_task_ids) / max(1, w.capacity),
                w.cpu,
                w.worker_id,
            ),
        )


class RoundRobinStrategy(BalancingStrategy):
    """
    Selects available workers in round-robin sequence.
    """

    def __init__(self):
        self._index = 0

    def select_worker(self, candidates: List["WorkerInfo"]) -> Optional["WorkerInfo"]:
        available = [w for w in candidates if w.is_available()]
        if not available:
            return None

        # Sort available workers deterministically before indexing
        sorted_workers = sorted(available, key=lambda w: w.worker_id)
        selected = sorted_workers[self._index % len(sorted_workers)]
        self._index = (self._index + 1) % len(sorted_workers)
        return selected


def get_balancing_strategy(name: str) -> BalancingStrategy:
    key = (name or "least_loaded").lower().strip()
    if key in ["round_robin", "rr"]:
        return RoundRobinStrategy()
    return LeastLoadedStrategy()

