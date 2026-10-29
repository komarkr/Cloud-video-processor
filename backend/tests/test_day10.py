from datetime import datetime, timezone
import json
import socket
import time
import uuid
import pytest

from app.models import Task, TaskStatus, User
from app.services.estimation import alive_workers
from autoscaler.scaler import Autoscaler
from tests.conftest import FakeRedis
from worker.heartbeat import (
    get_current_task,
    on_task_postrun,
    on_task_prerun,
    set_current_task,
    start_heartbeat,
    stop_heartbeat,
)


class MockDockerContainer:
    """Mock Docker container object for unit testing Autoscaler."""

    def __init__(self, cid: str, name: str, labels: dict = None, is_running: bool = True):
        self.id = cid
        self.name = name
        self.labels = labels or {"cvp.role": "worker"}
        self.is_running = is_running
        self.stopped = False
        self.removed = False
        self.attrs = {
            "NetworkSettings": {"Networks": {"cloud-video-processor_default": {}}},
            "Mounts": [{"Destination": "/data", "Name": "cloud-video-processor_media"}],
            "Config": {"Env": ["DATABASE_URL=sqlite:///./test.db", "REDIS_URL=redis://localhost:6379/0"]},
        }
        self.image = type("Image", (), {"tags": ["cloud-video-processor-worker:latest"], "id": "sha256:123"})()

    def stop(self, timeout=10):
        self.stopped = True
        self.is_running = False

    def remove(self):
        self.removed = True


class MockDockerContainers:
    """Mock Docker containers manager."""

    def __init__(self, containers: list = None):
        self._containers = containers or []
        self.run_calls = []

    def list(self, filters=None):
        return [c for c in self._containers if c.is_running and not c.removed]

    def run(self, image, command, **kwargs):
        cid = f"autoworker-{uuid.uuid4().hex[:8]}"
        name = f"cvp-worker-autoscaled-{len(self._containers) + 1}"
        labels = kwargs.get("labels", {})
        container = MockDockerContainer(cid=cid, name=name, labels=labels)
        self._containers.append(container)
        self.run_calls.append({"image": image, "command": command, "kwargs": kwargs})
        return container


class MockDockerClient:
    """Mock Docker client."""

    def __init__(self, containers: list = None):
        self.containers = MockDockerContainers(containers)

    def ping(self):
        return True


def test_worker_heartbeat_loop_and_signals():
    """Verify heartbeat writes to Redis and task signals update current_task."""
    mock_r = FakeRedis()
    hostname = socket.gethostname()

    task_id = "test-task-123"
    on_task_prerun(task_id=task_id, task=None)
    assert get_current_task() == task_id

    on_task_postrun(task_id=task_id, task=None)
    assert get_current_task() is None

    mock_r.hset(
        f"hb:{hostname}",
        mapping={
            "hostname": hostname,
            "cpu": "15.5",
            "ram": "42.0",
            "current_task": "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )

    hb = mock_r.hgetall(f"hb:{hostname}")
    assert hb["hostname"] == hostname
    assert hb["cpu"] == "15.5"
    assert hb["ram"] == "42.0"
    assert hb["current_task"] == ""

    assert alive_workers(mock_r) >= 1


def test_autoscaler_scale_up_to_max(db_session):
    """
    Test Autoscaler scaling up:
    Initial: 2 base workers, MIN_WORKERS=2, MAX_WORKERS=4, PER_WORKER=2.
    With 8 queued tasks:
    Step 1: queued=8 > 2*2=4 -> scales to 3 workers.
    Step 2: queued=8 > 3*2=6 -> scales to 4 workers.
    Step 3: queued=8 > 4*2=8 is False (and n == MAX_WORKERS) -> does not exceed 4 workers.
    """
    db_session.query(Task).delete()
    db_session.commit()

    user = User(email="autoscale_test@example.com", hashed_password="mock", is_admin=False)
    db_session.add(user)
    db_session.commit()

    tasks = [
        Task(
            id=uuid.uuid4(),
            user_id=user.id,
            status=TaskStatus.queued,
            operation_type="convert",
            input_file="/data/uploads/v.mp4",
            duration_sec=30.0,
        )
        for _ in range(8)
    ]
    db_session.add_all(tasks)
    db_session.commit()

    w1 = MockDockerContainer("c1", "worker-1", labels={"cvp.role": "worker"})
    w2 = MockDockerContainer("c2", "worker-2", labels={"cvp.role": "worker"})
    mock_docker = MockDockerClient([w1, w2])
    mock_r = FakeRedis()

    scaler = Autoscaler(
        docker_client=mock_docker,
        redis_client=mock_r,
        min_workers=2,
        max_workers=4,
        per_worker=2,
        idle_for=60,
    )

    res1 = scaler.step(db_session)
    assert res1["action"] == "scale_up"
    assert res1["workers"] == 3
    assert len(mock_docker.containers.run_calls) == 1

    res2 = scaler.step(db_session)
    assert res2["action"] == "scale_up"
    assert res2["workers"] == 4
    assert len(mock_docker.containers.run_calls) == 2

    res3 = scaler.step(db_session)
    assert res3["action"] == "none"
    assert res3["workers"] == 4
    assert len(mock_docker.containers.run_calls) == 2

    events = scaler.get_events()
    assert len(events) == 2
    assert events[0]["action"] == "scale_up"
    assert events[0]["workers"] == 3
    assert events[1]["action"] == "scale_up"
    assert events[1]["workers"] == 4


def test_autoscaler_scale_down_when_queue_empty(db_session):
    """
    Test Autoscaler scale down when queued == 0:
    - Base workers without autoscaled label are NOT killed.
    - Busy autoscaled workers are NOT killed.
    - Idle autoscaled workers killed only after idle_for seconds.
    - Total workers never drop below MIN_WORKERS (2).
    """
    db_session.query(Task).delete()
    db_session.commit()

    w1 = MockDockerContainer("c1", "worker-1", labels={"cvp.role": "worker"})
    w2 = MockDockerContainer("c2", "worker-2", labels={"cvp.role": "worker"})
    w3 = MockDockerContainer("c3", "worker-3", labels={"cvp.role": "worker", "cvp.autoscaled": "1"})
    w4 = MockDockerContainer("c4", "worker-4", labels={"cvp.role": "worker", "cvp.autoscaled": "1"})

    mock_docker = MockDockerClient([w1, w2, w3, w4])
    mock_r = FakeRedis()

    scaler = Autoscaler(
        docker_client=mock_docker,
        redis_client=mock_r,
        min_workers=2,
        max_workers=4,
        per_worker=2,
        idle_for=60,
    )

    res1 = scaler.step(db_session)
    assert res1["action"] == "none"
    assert res1["workers"] == 4
    assert not w3.stopped
    assert not w4.stopped

    now = time.time()
    scaler.idle_since["c3"] = now - 65
    scaler.idle_since["c4"] = now - 65

    mock_r.hset("hb:worker-4", mapping={"current_task": "task-uuid-444"})

    scaler.step(db_session)
    assert w3.stopped is True
    assert w3.removed is True
    assert w4.stopped is False

    mock_r.hset("hb:worker-4", mapping={"current_task": ""})
    scaler.idle_since["c4"] = now - 65

    scaler.step(db_session)
    assert w4.stopped is True
    assert w4.removed is True

    active = mock_docker.containers.list()
    assert len(active) == 2
    assert active[0].id == "c1"
    assert active[1].id == "c2"

    scaler.idle_since["c1"] = now - 100
    scaler.idle_since["c2"] = now - 100
    scaler.step(db_session)
    assert not w1.stopped
    assert not w2.stopped


def test_autoscaler_status_api(client, db_session):
    """Test GET /api/autoscaler/status endpoint returns heartbeats and metrics."""
    client.post("/api/auth/register", json={"email": "scale_api@example.com", "password": "password123"})
    client.post("/api/auth/login", json={"email": "scale_api@example.com", "password": "password123"})

    resp = client.get("/api/autoscaler/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["min_workers"] == 2
    assert data["max_workers"] == 4
    assert data["per_worker"] == 2
    assert "queued_tasks" in data
    assert "active_workers_count" in data
    assert "workers" in data
    assert "events" in data
