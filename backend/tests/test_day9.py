from datetime import datetime, timedelta, timezone
import json
import uuid
import pytest
from app.config import settings
from app.models import Task, TaskStatus, User
from app.services.estimation import (
    alive_workers,
    estimate_wait_sec,
    get_queue_position,
    recent_speed,
)
from worker.app import celery_app


def test_celery_configuration_and_limits():
    """Verify Celery app has prefetch 1, acks_late, reject_on_worker_lost, and soft_time_limit 600."""
    conf = celery_app.conf
    assert conf.worker_prefetch_multiplier == 1
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.task_soft_time_limit == 600
    assert conf.task_time_limit == 660


def test_queue_position_calculation(db_session):
    """Test 1-based queue_position ordering by created_at."""
    user = User(
        email="queue_user@example.com",
        hashed_password="mockpassword",
        is_admin=False,
    )
    db_session.add(user)
    db_session.commit()

    base_time = datetime.now(timezone.utc)
    t1 = Task(
        id=uuid.uuid4(),
        user_id=user.id,
        status=TaskStatus.queued,
        operation_type="convert",
        input_file="/data/uploads/t1.mp4",
        duration_sec=30.0,
        created_at=base_time - timedelta(minutes=5),
    )
    t2 = Task(
        id=uuid.uuid4(),
        user_id=user.id,
        status=TaskStatus.queued,
        operation_type="convert",
        input_file="/data/uploads/t2.mp4",
        duration_sec=45.0,
        created_at=base_time - timedelta(minutes=3),
    )
    t3 = Task(
        id=uuid.uuid4(),
        user_id=user.id,
        status=TaskStatus.queued,
        operation_type="convert",
        input_file="/data/uploads/t3.mp4",
        duration_sec=60.0,
        created_at=base_time - timedelta(minutes=1),
    )
    db_session.add_all([t1, t2, t3])
    db_session.commit()

    assert get_queue_position(db_session, t1) == 1
    assert get_queue_position(db_session, t2) == 2
    assert get_queue_position(db_session, t3) == 3


def test_estimate_wait_sec_calculation(db_session):
    """Verify estimate_wait_sec formula with queued tasks ahead, running tasks, and multiple workers."""
    from tests.conftest import FakeRedis

    mock_r = FakeRedis()
    mock_r.set("worker:heartbeat:worker-1", 1)
    mock_r.set("worker:heartbeat:worker-2", 1)

    assert alive_workers(mock_r) == 2

    user = User(
        email="eta_user@example.com",
        hashed_password="mockpassword",
        is_admin=False,
    )
    db_session.add(user)
    db_session.commit()

    base_time = datetime.now(timezone.utc)

    # 1 running task with 50% progress
    t_running = Task(
        id=uuid.uuid4(),
        user_id=user.id,
        status=TaskStatus.running,
        operation_type="convert",
        input_file="/data/uploads/tr.mp4",
        duration_sec=60.0,
        created_at=base_time - timedelta(minutes=10),
    )
    mock_r.hset(f"task:{t_running.id}", "progress", 50)

    # 1 queued task ahead
    t_ahead = Task(
        id=uuid.uuid4(),
        user_id=user.id,
        status=TaskStatus.queued,
        operation_type="convert",
        input_file="/data/uploads/ta.mp4",
        duration_sec=60.0,
        created_at=base_time - timedelta(minutes=5),
    )

    # The current task (position 2)
    t_curr = Task(
        id=uuid.uuid4(),
        user_id=user.id,
        status=TaskStatus.queued,
        operation_type="convert",
        input_file="/data/uploads/tc.mp4",
        duration_sec=40.0,
        created_at=base_time,
    )

    db_session.add_all([t_running, t_ahead, t_curr])
    db_session.commit()

    # running_left = 60 * (1 - 0.5) = 30
    # ahead = 60
    # total wait numerator = 90
    # speed = 1.0, workers = 2 -> 90 / 2 = 45 sec
    wait = estimate_wait_sec(db_session, mock_r, t_curr)
    assert wait == 45


def test_recent_speed_calculation(db_session):
    """Test recent_speed computes duration_sec / elapsed_sec for completed tasks."""
    user = User(
        email="speed_user@example.com",
        hashed_password="mockpassword",
        is_admin=False,
    )
    db_session.add(user)
    db_session.commit()

    now = datetime.now(timezone.utc)
    # Task took 30s to process a 60s video -> speed = 2.0
    t = Task(
        id=uuid.uuid4(),
        user_id=user.id,
        status=TaskStatus.done,
        operation_type="convert",
        input_file="/data/uploads/ts.mp4",
        duration_sec=60.0,
        started_at=now - timedelta(seconds=30),
        finished_at=now,
        created_at=now - timedelta(seconds=40),
    )
    db_session.add(t)
    db_session.commit()

    speed = recent_speed(db_session)
    assert pytest.approx(speed, 0.1) == 2.0


def test_queue_overflow_503_and_retry_after(client, monkeypatch, db_session):
    """When queued tasks reach MAX_QUEUE, API returns 503 with Retry-After header."""
    db_session.query(Task).delete()
    db_session.commit()

    # Login user
    client.post(
        "/api/auth/register",
        json={"email": "overflow@example.com", "password": "password123"},
    )
    login_resp = client.post(
        "/api/auth/login",
        json={"email": "overflow@example.com", "password": "password123"},
    )
    assert login_resp.status_code == 200

    # Set MAX_QUEUE to 1
    monkeypatch.setattr(settings, "max_queue", 1)

    user = db_session.query(User).filter(User.email == "overflow@example.com").first()
    # Create 1 queued task to fill the queue
    existing = Task(
        id=uuid.uuid4(),
        user_id=user.id,
        status=TaskStatus.queued,
        operation_type="convert",
        input_file="/data/uploads/existing.mp4",
        duration_sec=10.0,
    )
    db_session.add(existing)
    db_session.commit()

    # Mock probe_video so upload proceeds to queue check
    from app.services.probe import VideoProbeResult
    mock_res = VideoProbeResult(
        duration=15.0,
        width=1280,
        height=720,
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
        codec_name="h264",
        size=1024,
        raw_info={"streams": [{"codec_type": "video"}]},
    )
    monkeypatch.setattr("app.routers.tasks.probe_video", lambda p: mock_res)

    files = {"file": ("test_overflow.mp4", b"fake video content", "video/mp4")}
    data = {"operation_type": "convert"}
    response = client.post("/api/tasks", files=files, data=data)

    assert response.status_code == 503
    assert "Retry-After" in response.headers
    assert response.headers["Retry-After"] == "30"
    assert "maximum capacity" in response.json()["detail"].lower()


def test_api_returns_queue_position_and_eta(client, monkeypatch, db_session):
    """API returns queue_position, eta_sec, and duration_sec for queued tasks."""
    db_session.query(Task).delete()
    db_session.commit()

    # Register and login
    client.post(
        "/api/auth/register",
        json={"email": "eta_api@example.com", "password": "password123"},
    )
    client.post(
        "/api/auth/login",
        json={"email": "eta_api@example.com", "password": "password123"},
    )

    from app.services.probe import VideoProbeResult
    mock_res = VideoProbeResult(
        duration=25.0,
        width=1280,
        height=720,
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
        codec_name="h264",
        size=1024,
        raw_info={"streams": [{"codec_type": "video"}]},
    )
    monkeypatch.setattr("app.routers.tasks.probe_video", lambda p: mock_res)

    files = {"file": ("test_eta.mp4", b"fake video content", "video/mp4")}
    data = {"operation_type": "convert"}
    res = client.post("/api/tasks", files=files, data=data)

    assert res.status_code == 201
    task_data = res.json()
    assert task_data["status"] == "queued"
    assert task_data["queue_position"] == 1
    assert task_data["eta_sec"] is not None
    assert task_data["duration_sec"] == 25.0

    # Test GET /api/tasks
    list_res = client.get("/api/tasks")
    assert list_res.status_code == 200
    my_tasks = list_res.json()
    found = next((t for t in my_tasks if t["id"] == task_data["id"]), None)
    assert found is not None
    assert found["queue_position"] == 1
    assert found["eta_sec"] is not None

    # Test GET /api/tasks/{task_id}
    detail_res = client.get(f"/api/tasks/{task_data['id']}")
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert detail["queue_position"] == 1
    assert detail["eta_sec"] is not None
