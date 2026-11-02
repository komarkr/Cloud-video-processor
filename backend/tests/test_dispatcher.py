import asyncio
from datetime import datetime, timezone
import io
import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.models import Task, TaskStatus, User
import dispatcher.main
from dispatcher.client import WorkerClient
from dispatcher.main import (
    MAX_TASK_ATTEMPTS,
    WORKER_TOKEN,
    active_tasks,
    app,
    dispatch_cycle,
    health_check_cycle,
    monitor_cycle,
    recover_running_tasks_sync,
    registry,
    worker_client,
)
from dispatcher.registry import WorkerInfo, WorkerRegistry
from dispatcher.strategies import (
    LeastLoadedStrategy,
    RoundRobinStrategy,
    get_balancing_strategy,
)
from tests.conftest import FakeRedis, TestingSessionLocal, _global_fake_redis


@pytest.fixture(autouse=True)
def clean_dispatcher_state(db_session):
    dispatcher.main.session_factory = sessionmaker(bind=db_session.connection())
    dispatcher.main.redis_getter = lambda: _global_fake_redis
    for w in list(registry.get_all_workers()):
        registry.deregister(w.worker_id)
    active_tasks.clear()
    yield
    for w in list(registry.get_all_workers()):
        registry.deregister(w.worker_id)
    active_tasks.clear()


def test_least_loaded_strategy():
    strategy = LeastLoadedStrategy()

    w1 = WorkerInfo(worker_id="w1", url="http://w1:9000", capacity=1, active_task_ids={"t1"}, cpu=10.0)
    w2 = WorkerInfo(worker_id="w2", url="http://w2:9000", capacity=2, active_task_ids={"t2"}, cpu=30.0)
    w3 = WorkerInfo(worker_id="w3", url="http://w3:9000", capacity=2, active_task_ids=set(), cpu=50.0)

    # w1 is at capacity (1/1). w2 load is 1/2=0.5. w3 load is 0/2=0.0.
    selected = strategy.select_worker([w1, w2, w3])
    assert selected is not None
    assert selected.worker_id == "w3"

    # Now w3 has 1 task (1/2=0.5, cpu=50). w2 has 1 task (1/2=0.5, cpu=30).
    w3.active_task_ids.add("t3")
    selected2 = strategy.select_worker([w1, w2, w3])
    assert selected2 is not None
    assert selected2.worker_id == "w2"  # Lower CPU wins tie-break


def test_round_robin_strategy():
    strategy = RoundRobinStrategy()

    w1 = WorkerInfo(worker_id="w1", url="http://w1:9000", capacity=2)
    w2 = WorkerInfo(worker_id="w2", url="http://w2:9000", capacity=2)
    w3 = WorkerInfo(worker_id="w3", url="http://w3:9000", capacity=2)

    workers = [w1, w2, w3]
    picked = [strategy.select_worker(workers).worker_id for _ in range(4)]
    assert picked == ["w1", "w2", "w3", "w1"]


def test_no_available_workers_returns_none():
    strategy = LeastLoadedStrategy()
    w1 = WorkerInfo(worker_id="w1", url="http://w1:9000", capacity=1, active_task_ids={"t1"})
    assert strategy.select_worker([w1]) is None


def test_worker_registry_lifecycle():
    reg = WorkerRegistry()
    w = reg.register("w1", "http://w1:9000", capacity=2)
    assert w.worker_id == "w1"
    assert w.capacity == 2
    assert w.is_available() is True

    reg.assign_task("w1", "t1")
    assert "t1" in w.active_task_ids
    assert w.is_available() is True

    reg.assign_task("w1", "t2")
    assert w.is_available() is False

    reg.unassign_task("w1", "t1")
    assert w.is_available() is True

    dead = reg.record_failure("w1", threshold=3)
    assert dead is False
    assert reg.record_failure("w1", threshold=3) is False
    assert reg.record_failure("w1", threshold=3) is True
    assert w.state == "dead"
    assert w.is_available() is False

    reg.deregister("w1")
    assert reg.get_worker("w1") is None


def test_dispatcher_api_endpoints():
    client = TestClient(app)

    # Health check is public
    h_res = client.get("/health")
    assert h_res.status_code == 200

    # Internal endpoints require token
    assert client.get("/internal/workers").status_code == 401

    headers = {"X-Worker-Token": WORKER_TOKEN}

    # Register worker
    reg_res = client.post(
        "/internal/workers/register",
        headers=headers,
        json={"worker_id": "api-w1", "url": "http://10.0.0.1:9000", "capacity": 2},
    )
    assert reg_res.status_code == 200
    assert reg_res.json()["worker"]["worker_id"] == "api-w1"

    # List workers
    list_res = client.get("/internal/workers", headers=headers)
    assert list_res.status_code == 200
    assert list_res.json()["total_workers"] == 1

    # Deregister worker
    dereg_res = client.post(
        "/internal/workers/deregister",
        headers=headers,
        json={"worker_id": "api-w1"},
    )
    assert dereg_res.status_code == 200
    assert client.get("/internal/workers", headers=headers).json()["total_workers"] == 0


def test_dispatch_cycle_no_workers_leaves_tasks_queued(db_session, tmp_path):
    user = db_session.query(User).first()
    if not user:
        user = User(id=uuid.uuid4(), email="disp_test@example.com", hashed_password="pw", is_admin=False)
        db_session.add(user)
        db_session.commit()

    task_file = str(tmp_path / "video.mp4")
    with open(task_file, "wb") as f:
        f.write(b"video data")

    t = Task(
        id=uuid.uuid4(),
        user_id=user.id,
        status=TaskStatus.queued,
        operation_type="convert",
        input_file=task_file,
    )
    db_session.add(t)
    db_session.commit()

    asyncio.run(dispatch_cycle())

    db_session.refresh(t)
    assert t.status == TaskStatus.queued
    assert t.worker_id is None

    db_session.delete(t)
    db_session.commit()


def test_dispatch_cycle_success_updates_task_to_running(db_session, tmp_path, monkeypatch):
    user = db_session.query(User).first()
    if not user:
        user = User(id=uuid.uuid4(), email="disp_test2@example.com", hashed_password="pw", is_admin=False)
        db_session.add(user)
        db_session.commit()

    task_file = str(tmp_path / "video2.mp4")
    with open(task_file, "wb") as f:
        f.write(b"video data")

    t = Task(
        id=uuid.uuid4(),
        user_id=user.id,
        status=TaskStatus.queued,
        operation_type="convert",
        input_file=task_file,
    )
    db_session.add(t)
    db_session.commit()

    registry.register("worker-test-1", "http://worker-1:9000", capacity=1)

    mock_execute = AsyncMock(return_value=(202, {"status": "running"}))
    monkeypatch.setattr(worker_client, "execute", mock_execute)

    asyncio.run(dispatch_cycle())

    db_session.refresh(t)
    assert t.status == TaskStatus.running
    assert t.worker_id == "worker-test-1"
    assert str(t.id) in active_tasks

    db_session.delete(t)
    db_session.commit()


def test_dispatch_fallback_to_second_worker_on_failure(db_session, tmp_path, monkeypatch):
    user = db_session.query(User).first()
    if not user:
        user = User(id=uuid.uuid4(), email="disp_test3@example.com", hashed_password="pw", is_admin=False)
        db_session.add(user)
        db_session.commit()

    task_file = str(tmp_path / "video3.mp4")
    with open(task_file, "wb") as f:
        f.write(b"video data")

    t = Task(
        id=uuid.uuid4(),
        user_id=user.id,
        status=TaskStatus.queued,
        operation_type="convert",
        input_file=task_file,
    )
    db_session.add(t)
    db_session.commit()

    registry.register("worker-busy", "http://worker-busy:9000", capacity=1)
    registry.register("worker-ready", "http://worker-ready:9000", capacity=1)

    async def mock_exec(worker_url, **kwargs):
        if "worker-busy" in worker_url:
            return 503, {"detail": "busy"}
        return 202, {"status": "running"}

    monkeypatch.setattr(worker_client, "execute", mock_exec)

    asyncio.run(dispatch_cycle())

    db_session.refresh(t)
    assert t.status == TaskStatus.running
    assert t.worker_id == "worker-ready"

    db_session.delete(t)
    db_session.commit()


def test_monitor_cycle_completes_done_task(db_session, tmp_path, monkeypatch):
    user = db_session.query(User).first()
    if not user:
        user = User(id=uuid.uuid4(), email="disp_test4@example.com", hashed_password="pw", is_admin=False)
        db_session.add(user)
        db_session.commit()

    t_id = uuid.uuid4()
    t = Task(
        id=t_id,
        user_id=user.id,
        status=TaskStatus.running,
        worker_id="worker-w1",
        started_at=datetime.now(timezone.utc),
        operation_type="convert",
        input_file="input.mp4",
    )
    db_session.add(t)
    db_session.commit()

    registry.register("worker-w1", "http://worker-w1:9000", capacity=1)
    registry.assign_task("worker-w1", str(t_id))
    active_tasks[str(t_id)] = "worker-w1"

    monkeypatch.setattr(worker_client, "get_progress", AsyncMock(return_value=(200, {"status": "done", "progress": 100})))
    monkeypatch.setattr(worker_client, "download_result", AsyncMock(return_value=True))
    monkeypatch.setattr(worker_client, "delete_task", AsyncMock(return_value=True))

    asyncio.run(monitor_cycle())

    db_session.refresh(t)
    assert t.status == TaskStatus.done
    assert str(t_id) not in active_tasks

    db_session.delete(t)
    db_session.commit()


def test_monitor_cycle_worker_404_requeues_task(db_session, monkeypatch):
    user = db_session.query(User).first()
    if not user:
        user = User(id=uuid.uuid4(), email="disp_test5@example.com", hashed_password="pw", is_admin=False)
        db_session.add(user)
        db_session.commit()

    t_id = uuid.uuid4()
    t = Task(
        id=t_id,
        user_id=user.id,
        status=TaskStatus.running,
        worker_id="worker-reboot",
        started_at=datetime.now(timezone.utc),
        operation_type="convert",
        input_file="input.mp4",
    )
    db_session.add(t)
    db_session.commit()

    registry.register("worker-reboot", "http://worker-reboot:9000", capacity=1)
    registry.assign_task("worker-reboot", str(t_id))
    active_tasks[str(t_id)] = "worker-reboot"

    monkeypatch.setattr(worker_client, "get_progress", AsyncMock(return_value=(404, {"detail": "lost"})))

    asyncio.run(monitor_cycle())

    db_session.refresh(t)
    assert t.status == TaskStatus.queued
    assert t.worker_id is None
    assert str(t_id) not in active_tasks

    db_session.delete(t)
    db_session.commit()


def test_health_check_dead_worker_requeues_task(db_session, monkeypatch):
    user = db_session.query(User).first()
    if not user:
        user = User(id=uuid.uuid4(), email="disp_test6@example.com", hashed_password="pw", is_admin=False)
        db_session.add(user)
        db_session.commit()

    t_id = uuid.uuid4()
    t = Task(
        id=t_id,
        user_id=user.id,
        status=TaskStatus.running,
        worker_id="worker-dying",
        started_at=datetime.now(timezone.utc),
        operation_type="convert",
        input_file="input.mp4",
    )
    db_session.add(t)
    db_session.commit()

    registry.register("worker-dying", "http://worker-dying:9000", capacity=1)
    registry.assign_task("worker-dying", str(t_id))
    active_tasks[str(t_id)] = "worker-dying"

    # Mock health failure
    monkeypatch.setattr(worker_client, "get_status", AsyncMock(side_effect=Exception("Connection refused")))

    async def run_checks():
        await health_check_cycle()
        await health_check_cycle()
        await health_check_cycle()

    asyncio.run(run_checks())

    assert registry.get_worker("worker-dying") is None

    db_session.refresh(t)
    assert t.status == TaskStatus.queued
    assert t.worker_id is None
    assert str(t_id) not in active_tasks

    db_session.delete(t)
    db_session.commit()


def test_startup_recovery(db_session):
    user = db_session.query(User).first()
    if not user:
        user = User(id=uuid.uuid4(), email="disp_rec@example.com", hashed_password="pw", is_admin=False)
        db_session.add(user)
        db_session.commit()

    t = Task(
        id=uuid.uuid4(),
        user_id=user.id,
        status=TaskStatus.running,
        worker_id="old-worker",
        operation_type="convert",
        input_file="in.mp4",
    )
    db_session.add(t)
    db_session.commit()

    recover_running_tasks_sync()

    db_session.refresh(t)
    assert t.status == TaskStatus.queued
    assert t.worker_id is None

    db_session.delete(t)
    db_session.commit()
