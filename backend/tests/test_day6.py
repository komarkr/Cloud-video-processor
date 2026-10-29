from datetime import datetime, timedelta, timezone
import json
import os
import uuid
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import get_db
from app.main import app
from app.models import Task, TaskStatus, User
from worker.tasks import build_ffmpeg_args, get_output_extension, process_task

FIXTURES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "fixtures", "videos"))
IMAGES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "fixtures", "images"))


@pytest.fixture(autouse=True)
def bind_worker_session(monkeypatch, db_session):
    """Ensure worker.tasks uses the active test database session."""
    class NoCloseSession:
        def __init__(self, s):
            self._s = s

        def __getattr__(self, item):
            if item == "close":
                return lambda: None
            return getattr(self._s, item)

    import worker.tasks
    monkeypatch.setattr(worker.tasks, "SessionLocal", lambda: NoCloseSession(db_session))


@pytest.fixture
def user_a(client, db_session):
    email = "usera@example.com"
    pwd = "password123"
    client.post("/api/register", json={"email": email, "password": pwd})
    client.post("/api/login", json={"email": email, "password": pwd})
    user = db_session.query(User).filter(User.email == email).first()
    client.user_id = user.id
    return client


@pytest.fixture
def user_b(db_session):
    email = "userb@example.com"
    pwd = "password123"
    with TestClient(app, base_url="https://testserver") as b_client:
        b_client.post("/api/register", json={"email": email, "password": pwd})
        b_client.post("/api/login", json={"email": email, "password": pwd})
        user = db_session.query(User).filter(User.email == email).first()
        b_client.user_id = user.id
        yield b_client


def test_sse_events_stream_and_stranger_404(user_a, user_b, db_session):
    """Test SSE events stream for owner and 404 for strangers."""
    task_id = uuid.uuid4()
    task = Task(
        id=task_id,
        user_id=user_a.user_id,
        status=TaskStatus.done,
        operation_type="convert",
        input_file="dummy.mp4",
        output_file="dummy_out.mp4",
    )
    db_session.add(task)
    db_session.commit()

    resp_b = user_b.get(f"/api/tasks/{task_id}/events")
    assert resp_b.status_code == 404

    resp_a = user_a.get(f"/api/tasks/{task_id}/events")
    assert resp_a.status_code == 200
    assert "text/event-stream" in resp_a.headers["content-type"]
    assert "data:" in resp_a.text
    assert '"status": "done"' in resp_a.text


def test_download_success_and_expiry(user_a, user_b, db_session):
    """Test download endpoint: success, not-done, expired, and stranger checks."""
    os.makedirs(settings.output_dir, exist_ok=True)
    temp_out = os.path.join(settings.output_dir, "test_download.mp4")
    with open(temp_out, "wb") as f:
        f.write(b"sample video data")

    now = datetime.now(timezone.utc)
    task_id = uuid.uuid4()
    task = Task(
        id=task_id,
        user_id=user_a.user_id,
        status=TaskStatus.done,
        operation_type="convert",
        input_file="in.mp4",
        output_file=temp_out,
        expires_at=now + timedelta(hours=24),
    )
    db_session.add(task)
    db_session.commit()

    resp = user_a.get(f"/api/tasks/{task_id}/download")
    assert resp.status_code == 200
    assert resp.content == b"sample video data"

    resp_stranger = user_b.get(f"/api/tasks/{task_id}/download")
    assert resp_stranger.status_code == 404

    task.status = TaskStatus.running
    db_session.commit()
    resp_running = user_a.get(f"/api/tasks/{task_id}/download")
    assert resp_running.status_code == 400

    task.status = TaskStatus.done
    task.expires_at = now - timedelta(hours=1)
    db_session.commit()
    resp_exp = user_a.get(f"/api/tasks/{task_id}/download")
    assert resp_exp.status_code == 410

    if os.path.exists(temp_out):
        os.remove(temp_out)


def test_delete_task_active_and_completed(user_a, user_b, db_session):
    """Test deleting task: cancel if active, clean files, remove DB record."""
    dummy_in = os.path.join(settings.upload_dir, "dummy_in.mp4")
    dummy_out = os.path.join(settings.output_dir, "dummy_out.mp4")
    os.makedirs(settings.upload_dir, exist_ok=True)
    os.makedirs(settings.output_dir, exist_ok=True)
    with open(dummy_in, "wb") as f:
        f.write(b"in")
    with open(dummy_out, "wb") as f:
        f.write(b"out")

    task_id = uuid.uuid4()
    task = Task(
        id=task_id,
        user_id=user_a.user_id,
        status=TaskStatus.running,
        operation_type="convert",
        input_file=dummy_in,
        output_file=dummy_out,
    )
    db_session.add(task)
    db_session.commit()

    resp_b = user_b.delete(f"/api/tasks/{task_id}")
    assert resp_b.status_code == 404

    resp_a = user_a.delete(f"/api/tasks/{task_id}")
    assert resp_a.status_code == 200
    assert resp_a.json()["status"] == "ok"

    assert not os.path.exists(dummy_in)
    assert not os.path.exists(dummy_out)
    deleted_task = db_session.query(Task).filter(Task.id == task_id).first()
    assert deleted_task is None


def test_history_limit_pruning(user_a, db_session):
    """Test history limit: when user has 20 tasks, creating 21st prunes oldest completed."""
    video_path = os.path.join(FIXTURES_DIR, "test_5s.mp4")
    assert os.path.exists(video_path)

    base_time = datetime.now(timezone.utc) - timedelta(days=10)
    for i in range(20):
        t = Task(
            id=uuid.uuid4(),
            user_id=user_a.user_id,
            status=TaskStatus.done,
            operation_type="convert",
            input_file="fake_in.mp4",
            output_file="fake_out.mp4",
            created_at=base_time + timedelta(minutes=i),
        )
        db_session.add(t)
    db_session.commit()

    assert db_session.query(Task).filter(Task.user_id == user_a.user_id).count() == 20
    oldest_before = (
        db_session.query(Task)
        .filter(Task.user_id == user_a.user_id)
        .order_by(Task.created_at.asc())
        .first()
    )

    with open(video_path, "rb") as f:
        resp = user_a.post(
            "/api/tasks",
            data={"operation_type": "convert"},
            files={"file": ("test_5s.mp4", f, "video/mp4")},
        )
    assert resp.status_code == 201

    total_count = db_session.query(Task).filter(Task.user_id == user_a.user_id).count()
    assert total_count == 20

    assert db_session.query(Task).filter(Task.id == oldest_before.id).first() is None

    new_task_data = resp.json()
    if os.path.exists(new_task_data["input_file"]):
        os.remove(new_task_data["input_file"])


def test_watermark_validation_and_processing(user_a, db_session):
    """Test watermark operation: missing logo, invalid format, large logo, and successful run."""
    video_path = os.path.join(FIXTURES_DIR, "test_5s.mp4")
    valid_logo = os.path.join(IMAGES_DIR, "watermark.png")
    not_a_png = os.path.join(IMAGES_DIR, "not_a_png.png")
    large_logo = os.path.join(IMAGES_DIR, "large_logo.png")

    with open(video_path, "rb") as vf:
        resp = user_a.post(
            "/api/tasks",
            data={"operation_type": "watermark"},
            files={"file": ("test_5s.mp4", vf, "video/mp4")},
        )
    assert resp.status_code == 422
    assert "requires a 'logo'" in resp.json()["detail"].lower()

    with open(video_path, "rb") as vf, open(not_a_png, "rb") as lf:
        resp = user_a.post(
            "/api/tasks",
            data={"operation_type": "watermark"},
            files={
                "file": ("test_5s.mp4", vf, "video/mp4"),
                "logo": ("not_a_png.png", lf, "image/png"),
            },
        )
    assert resp.status_code == 422
    assert "valid png" in resp.json()["detail"].lower()

    with open(video_path, "rb") as vf, open(large_logo, "rb") as lf:
        resp = user_a.post(
            "/api/tasks",
            data={"operation_type": "watermark"},
            files={
                "file": ("test_5s.mp4", vf, "video/mp4"),
                "logo": ("large_logo.png", lf, "image/png"),
            },
        )
    assert resp.status_code == 422
    assert "exceeds maximum allowed size of 2 mb" in resp.json()["detail"].lower()

    with open(video_path, "rb") as vf, open(valid_logo, "rb") as lf:
        resp = user_a.post(
            "/api/tasks",
            data={
                "operation_type": "watermark",
                "params": json.dumps({"position": "bottom_right", "opacity": 0.8}),
            },
            files={
                "file": ("test_5s.mp4", vf, "video/mp4"),
                "logo": ("watermark.png", lf, "image/png"),
            },
        )
    assert resp.status_code == 201
    task_data = resp.json()
    task_id = task_data["id"]

    process_task(str(task_id))

    task = db_session.query(Task).filter(Task.id == uuid.UUID(task_id)).first()
    assert task.status == TaskStatus.done
    assert task.output_file is not None
    assert os.path.exists(task.output_file)
    assert task.expires_at is not None

    if os.path.exists(task.input_file):
        os.remove(task.input_file)
    if os.path.exists(task.output_file):
        os.remove(task.output_file)
    if task.params and "logo_path" in task.params and os.path.exists(task.params["logo_path"]):
        os.remove(task.params["logo_path"])


def test_audio_operations(user_a, db_session):
    """Test audio extraction to MP3 and audio mute to MP4."""
    video_path = os.path.join(FIXTURES_DIR, "test_5s.mp4")

    with open(video_path, "rb") as vf:
        resp = user_a.post(
            "/api/tasks",
            data={"operation_type": "audio", "params": json.dumps({"action": "extract"})},
            files={"file": ("test_5s.mp4", vf, "video/mp4")},
        )
    assert resp.status_code == 201
    task_id_extract = resp.json()["id"]

    process_task(str(task_id_extract))
    task_extract = db_session.query(Task).filter(Task.id == uuid.UUID(task_id_extract)).first()
    assert task_extract.status == TaskStatus.done
    assert task_extract.output_file.endswith(".mp3")
    assert os.path.exists(task_extract.output_file)

    with open(video_path, "rb") as vf:
        resp_mute = user_a.post(
            "/api/tasks",
            data={"operation_type": "audio", "params": json.dumps({"action": "mute"})},
            files={"file": ("test_5s.mp4", vf, "video/mp4")},
        )
    assert resp_mute.status_code == 201
    task_id_mute = resp_mute.json()["id"]

    process_task(str(task_id_mute))
    task_mute = db_session.query(Task).filter(Task.id == uuid.UUID(task_id_mute)).first()
    assert task_mute.status == TaskStatus.done
    assert task_mute.output_file.endswith(".mp4")
    assert os.path.exists(task_mute.output_file)

    for t in [task_extract, task_mute]:
        if os.path.exists(t.input_file):
            os.remove(t.input_file)
        if os.path.exists(t.output_file):
            os.remove(t.output_file)
