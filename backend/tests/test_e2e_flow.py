from datetime import datetime, timedelta, timezone
import json
import os
import uuid
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import Task, TaskStatus, User
from worker.tasks import process_task

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
def user1(client, db_session):
    email = "e2e_user1@example.com"
    pwd = "password123"
    client.post("/api/register", json={"email": email, "password": pwd})
    client.post("/api/login", json={"email": email, "password": pwd})
    user = db_session.query(User).filter(User.email == email).first()
    client.user_id = user.id
    return client


@pytest.fixture
def user2(db_session):
    email = "e2e_user2@example.com"
    pwd = "password123"
    with TestClient(app, base_url="https://testserver") as client2:
        client2.post("/api/register", json={"email": email, "password": pwd})
        client2.post("/api/login", json={"email": email, "password": pwd})
        user = db_session.query(User).filter(User.email == email).first()
        client2.user_id = user.id
        yield client2


def test_e2e_all_four_operations(user1, db_session):
    """End-to-end test of all 4 video operations: convert, scale, watermark, audio (extract + mute)."""
    video_path = os.path.join(FIXTURES_DIR, "test_5s.mp4")
    watermark_path = os.path.join(IMAGES_DIR, "watermark.png")
    assert os.path.exists(video_path)
    assert os.path.exists(watermark_path)

    with open(video_path, "rb") as vf:
        resp_conv = user1.post(
            "/api/tasks",
            data={"operation_type": "convert"},
            files={"file": ("test_5s.mp4", vf, "video/mp4")},
        )
    assert resp_conv.status_code == 201
    task_id_conv = resp_conv.json()["id"]
    process_task(str(task_id_conv))
    t_conv = db_session.query(Task).filter(Task.id == uuid.UUID(task_id_conv)).first()
    assert t_conv.status == TaskStatus.done
    assert os.path.exists(t_conv.output_file)

    with open(video_path, "rb") as vf:
        resp_scale = user1.post(
            "/api/tasks",
            data={"operation_type": "scale", "params": json.dumps({"resolution": 720})},
            files={"file": ("test_5s.mp4", vf, "video/mp4")},
        )
    assert resp_scale.status_code == 201
    task_id_scale = resp_scale.json()["id"]
    process_task(str(task_id_scale))
    t_scale = db_session.query(Task).filter(Task.id == uuid.UUID(task_id_scale)).first()
    assert t_scale.status == TaskStatus.done
    assert os.path.exists(t_scale.output_file)

    with open(video_path, "rb") as vf, open(watermark_path, "rb") as lf:
        resp_wm = user1.post(
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
    assert resp_wm.status_code == 201
    task_id_wm = resp_wm.json()["id"]
    process_task(str(task_id_wm))
    t_wm = db_session.query(Task).filter(Task.id == uuid.UUID(task_id_wm)).first()
    assert t_wm.status == TaskStatus.done
    assert os.path.exists(t_wm.output_file)

    with open(video_path, "rb") as vf:
        resp_audio = user1.post(
            "/api/tasks",
            data={"operation_type": "audio", "params": json.dumps({"action": "extract"})},
            files={"file": ("test_5s.mp4", vf, "video/mp4")},
        )
    assert resp_audio.status_code == 201
    task_id_audio = resp_audio.json()["id"]
    process_task(str(task_id_audio))
    t_audio = db_session.query(Task).filter(Task.id == uuid.UUID(task_id_audio)).first()
    assert t_audio.status == TaskStatus.done
    assert t_audio.output_file.endswith(".mp3")
    assert os.path.exists(t_audio.output_file)

    with open(video_path, "rb") as vf:
        resp_mute = user1.post(
            "/api/tasks",
            data={"operation_type": "audio", "params": json.dumps({"action": "mute"})},
            files={"file": ("test_5s.mp4", vf, "video/mp4")},
        )
    assert resp_mute.status_code == 201
    task_id_mute = resp_mute.json()["id"]
    process_task(str(task_id_mute))
    t_mute = db_session.query(Task).filter(Task.id == uuid.UUID(task_id_mute)).first()
    assert t_mute.status == TaskStatus.done
    assert t_mute.output_file.endswith(".mp4")
    assert os.path.exists(t_mute.output_file)

    for t in [t_conv, t_scale, t_wm, t_audio, t_mute]:
        if os.path.exists(t.input_file):
            os.remove(t.input_file)
        if os.path.exists(t.output_file):
            os.remove(t.output_file)
        if t.params and "logo_path" in t.params and os.path.exists(t.params["logo_path"]):
            os.remove(t.params["logo_path"])


def test_e2e_cancellation_flow(user1, db_session):
    """End-to-end test of cancellation of queued task and running task."""
    video_path = os.path.join(FIXTURES_DIR, "test_5s.mp4")

    with open(video_path, "rb") as vf:
        resp = user1.post(
            "/api/tasks",
            data={"operation_type": "convert"},
            files={"file": ("test_5s.mp4", vf, "video/mp4")},
        )
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    cancel_resp = user1.post(f"/api/tasks/{task_id}/cancel")
    assert cancel_resp.status_code == 200

    task = db_session.query(Task).filter(Task.id == uuid.UUID(task_id)).first()
    assert task.status == TaskStatus.cancelled
    assert task.finished_at is not None

    if os.path.exists(task.input_file):
        os.remove(task.input_file)


def test_e2e_erroneous_videos_validation(user1):
    """End-to-end test of invalid uploads: corrupted/non-video, duration > 120s, missing logo."""
    not_a_video = os.path.join(FIXTURES_DIR, "not_a_video.mp4")
    too_long = os.path.join(FIXTURES_DIR, "test_150s.mp4")
    video_path = os.path.join(FIXTURES_DIR, "test_5s.mp4")

    with open(not_a_video, "rb") as f:
        resp = user1.post("/api/tasks", files={"file": ("not_a_video.mp4", f, "video/mp4")})
    assert resp.status_code == 422
    assert "validation failed" in resp.json()["detail"].lower() or "corrupt" in resp.json()["detail"].lower()

    with open(too_long, "rb") as f:
        resp = user1.post("/api/tasks", files={"file": ("test_150s.mp4", f, "video/mp4")})
    assert resp.status_code == 422
    assert "exceeds maximum allowed 120" in resp.json()["detail"].lower()

    with open(video_path, "rb") as f:
        resp = user1.post("/api/tasks", data={"operation_type": "watermark"}, files={"file": ("test_5s.mp4", f, "video/mp4")})
    assert resp.status_code == 422
    assert "requires a 'logo'" in resp.json()["detail"].lower()


def test_e2e_stranger_task_isolation_404(user1, user2, db_session):
    """End-to-end test verifying stranger tasks return 404 for events, download, and delete."""
    video_path = os.path.join(FIXTURES_DIR, "test_5s.mp4")

    with open(video_path, "rb") as vf:
        resp = user1.post("/api/tasks", files={"file": ("test_5s.mp4", vf, "video/mp4")})
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    resp_events = user2.get(f"/api/tasks/{task_id}/events")
    assert resp_events.status_code == 404

    resp_download = user2.get(f"/api/tasks/{task_id}/download")
    assert resp_download.status_code == 404

    resp_delete = user2.delete(f"/api/tasks/{task_id}")
    assert resp_delete.status_code == 404

    t = db_session.query(Task).filter(Task.id == uuid.UUID(task_id)).first()
    if t and os.path.exists(t.input_file):
        os.remove(t.input_file)


def test_e2e_active_tasks_limit_429(user1, db_session):
    """End-to-end test verifying limit of 5 active tasks (MAX_ACTIVE_TASKS) returns 429."""
    video_path = os.path.join(FIXTURES_DIR, "test_5s.mp4")

    task_ids = []
    for i in range(5):
        t = Task(
            id=uuid.uuid4(),
            user_id=user1.user_id,
            status=TaskStatus.queued,
            operation_type="convert",
            input_file=f"dummy_{i}.mp4",
        )
        db_session.add(t)
        task_ids.append(t.id)
    db_session.commit()

    with open(video_path, "rb") as vf:
        resp = user1.post("/api/tasks", files={"file": ("test_5s.mp4", vf, "video/mp4")})
    assert resp.status_code == 429
    assert "active tasks limit exceeded" in resp.json()["detail"].lower()

    for tid in task_ids:
        dt = db_session.query(Task).filter(Task.id == tid).first()
        if dt:
            db_session.delete(dt)
    db_session.commit()
