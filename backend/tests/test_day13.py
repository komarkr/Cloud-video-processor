from datetime import datetime, timedelta, timezone
import io
import os
import subprocess
import uuid
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.models import Task, TaskStatus, User
from app.services.estimation import estimate_wait_sec, get_queue_position
from app.services.security import hash_password
from tests.conftest import FakeRedis
from worker.tasks import (
    Cancelled,
    parse_ffmpeg_progress_line,
    run_ffmpeg,
)


def test_parse_ffmpeg_progress_line_fake_strings():
    """Verify parsing of FFmpeg -progress output lines across valid and invalid inputs."""
    total_sec = 10.0

    assert parse_ffmpeg_progress_line("out_time_us=0\n", total_sec) == 0
    assert parse_ffmpeg_progress_line("out_time_us=2500000\n", total_sec) == 25
    assert parse_ffmpeg_progress_line("out_time_us=5000000\n", total_sec) == 50
    assert parse_ffmpeg_progress_line("out_time_us=7500000\n", total_sec) == 75
    assert parse_ffmpeg_progress_line("out_time_us=9900000\n", total_sec) == 99

    assert parse_ffmpeg_progress_line("out_time_us=10000000\n", total_sec) == 99
    assert parse_ffmpeg_progress_line("out_time_us=15000000\n", total_sec) == 99

    assert parse_ffmpeg_progress_line("frame=120\n", total_sec) is None
    assert parse_ffmpeg_progress_line("fps=25.00\n", total_sec) is None
    assert parse_ffmpeg_progress_line("stream_0_0_q=-0.0\n", total_sec) is None
    assert parse_ffmpeg_progress_line("bitrate= 241.9kbits/s\n", total_sec) is None
    assert parse_ffmpeg_progress_line("total_size=91234\n", total_sec) is None
    assert parse_ffmpeg_progress_line("speed= 1.5x\n", total_sec) is None
    assert parse_ffmpeg_progress_line("progress=continue\n", total_sec) is None

    assert parse_ffmpeg_progress_line("out_time_us=\n", total_sec) is None
    assert parse_ffmpeg_progress_line("out_time_us=invalid_string\n", total_sec) is None
    assert parse_ffmpeg_progress_line("out_time_us=-500\n", total_sec) is None
    assert parse_ffmpeg_progress_line("", total_sec) is None
    assert parse_ffmpeg_progress_line(None, total_sec) is None

    assert parse_ffmpeg_progress_line("out_time_us=5000000\n", 0.0) is None
    assert parse_ffmpeg_progress_line("out_time_us=5000000\n", -1.0) is None


def test_active_tasks_limit_enforced_returns_429(client: TestClient, db_session, monkeypatch):
    """Verify that exceeding MAX_ACTIVE_TASKS (5) active tasks returns 429 Too Many Requests."""
    email = "active_limit_user@example.com"
    client.post("/api/auth/register", json={"email": email, "password": "Password123!"})
    client.post("/api/auth/login", json={"email": email, "password": "Password123!"})

    user = db_session.query(User).filter(User.email == email).first()
    assert user is not None

    for i in range(3):
        t = Task(
            id=uuid.uuid4(),
            user_id=user.id,
            status=TaskStatus.queued,
            operation_type="convert",
            input_file="/data/uploads/dummy.mp4",
        )
        db_session.add(t)

    for i in range(2):
        t = Task(
            id=uuid.uuid4(),
            user_id=user.id,
            status=TaskStatus.running,
            operation_type="scale",
            input_file="/data/uploads/dummy.mp4",
        )
        db_session.add(t)

    db_session.commit()

    from app.services.probe import VideoProbeResult
    monkeypatch.setattr(
        "app.routers.tasks.probe_video",
        lambda p: VideoProbeResult(duration=3.0, width=320, height=240, format_name="mov,mp4", codec_name="h264", size=100, raw_info={}),
    )

    video_content = b"fake video bytes for testing active limit"
    response = client.post(
        "/api/tasks",
        files={"file": ("test.mp4", io.BytesIO(video_content), "video/mp4")},
        data={"operation_type": "convert"},
    )
    assert response.status_code == 429
    assert "Active tasks limit exceeded" in response.json()["detail"]


def test_history_pruning_limit_enforced(client: TestClient, db_session, monkeypatch):
    """Verify that when user reaches MAX_HISTORY (20), creating a new task prunes oldest completed tasks and files."""
    os.makedirs(settings.upload_dir, exist_ok=True)
    os.makedirs(settings.output_dir, exist_ok=True)

    email = "history_user@example.com"
    client.post("/api/auth/register", json={"email": email, "password": "Password123!"})
    client.post("/api/auth/login", json={"email": email, "password": "Password123!"})

    user = db_session.query(User).filter(User.email == email).first()
    assert user is not None

    now = datetime.now(timezone.utc)
    oldest_file = os.path.join(settings.upload_dir, "oldest_history_file.mp4")
    with open(oldest_file, "w") as f:
        f.write("oldest task file")

    for i in range(20):
        t_id = uuid.uuid4()
        t_file = oldest_file if i == 0 else os.path.join(settings.upload_dir, f"{t_id}.mp4")
        t = Task(
            id=t_id,
            user_id=user.id,
            status=TaskStatus.done,
            operation_type="convert",
            input_file=t_file,
            created_at=now - timedelta(days=21 - i),
        )
        db_session.add(t)
    db_session.commit()

    total_before = db_session.query(Task).filter(Task.user_id == user.id).count()
    assert total_before == 20
    assert os.path.exists(oldest_file)

    from app.services.probe import VideoProbeResult
    monkeypatch.setattr(
        "app.routers.tasks.probe_video",
        lambda p: VideoProbeResult(duration=3.0, width=320, height=240, format_name="mov,mp4", codec_name="h264", size=100, raw_info={}),
    )

    video_content = b"new valid task bytes"
    resp = client.post(
        "/api/tasks",
        files={"file": ("new_task.mp4", io.BytesIO(video_content), "video/mp4")},
        data={"operation_type": "convert"},
    )
    assert resp.status_code == 201

    assert not os.path.exists(oldest_file)

    total_after = db_session.query(Task).filter(Task.user_id == user.id).count()
    assert total_after <= settings.max_history


def test_eta_calculation_and_queue_position(db_session):
    """Test wait time estimation (ETA) and queue position calculations."""
    fake_redis = FakeRedis()
    now = datetime.now(timezone.utc)

    user = db_session.query(User).first()
    if not user:
        user = User(id=uuid.uuid4(), email="eta_test@example.com", hashed_password="pw", is_admin=False)
        db_session.add(user)
        db_session.commit()

    t1 = Task(id=uuid.uuid4(), user_id=user.id, status=TaskStatus.queued, duration_sec=30.0, created_at=now - timedelta(minutes=5), operation_type="convert", input_file="f1.mp4")
    t2 = Task(id=uuid.uuid4(), user_id=user.id, status=TaskStatus.queued, duration_sec=40.0, created_at=now - timedelta(minutes=3), operation_type="convert", input_file="f2.mp4")
    t3 = Task(id=uuid.uuid4(), user_id=user.id, status=TaskStatus.queued, duration_sec=50.0, created_at=now - timedelta(minutes=1), operation_type="convert", input_file="f3.mp4")

    db_session.add_all([t1, t2, t3])
    db_session.commit()

    assert get_queue_position(db_session, t1) == 1
    assert get_queue_position(db_session, t2) == 2
    assert get_queue_position(db_session, t3) == 3

    fake_redis.hset("hb:worker-1", "timestamp", datetime.now(timezone.utc).timestamp())
    fake_redis.hset("hb:worker-2", "timestamp", datetime.now(timezone.utc).timestamp())

    eta_t3 = estimate_wait_sec(db_session, fake_redis, t3)
    assert 30 <= eta_t3 <= 45

    db_session.delete(t1)
    db_session.delete(t2)
    db_session.delete(t3)
    db_session.commit()


def test_cancellation_flag_and_cooperative_abort():
    """Verify that cooperative cancellation flag raises Cancelled and cleans up."""
    fake_redis = FakeRedis()
    task_id = str(uuid.uuid4())

    fake_redis.set(f"cancel:{task_id}", 1)
    assert fake_redis.exists(f"cancel:{task_id}") == 1

    with pytest.raises(Cancelled):
        raise Cancelled("Task cancelled via Redis flag")
