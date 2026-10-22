import os
import uuid
import pytest
import redis

from app.config import settings
from app.models import Task, TaskStatus, User
from app.services.probe import probe_video
from worker.tasks import process_task

FIXTURES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "fixtures", "videos"))


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
def auth_client(client, db_session):
    """Register and login a test user, returning client with authenticated session."""
    email = "workeruser@example.com"
    pwd = "password123"
    client.post("/api/register", json={"email": email, "password": pwd})
    client.post("/api/login", json={"email": email, "password": pwd})

    user = db_session.query(User).filter(User.email == email).first()
    client.user_id = user.id
    return client


def test_process_task_convert_success(db_session, auth_client):
    """Test process_task successfully converts video, updates DB and Redis."""
    video_path = os.path.join(FIXTURES_DIR, "test_5s.mp4")
    assert os.path.exists(video_path)

    task_id = uuid.uuid4()
    task = Task(
        id=task_id,
        user_id=auth_client.user_id,
        status=TaskStatus.queued,
        operation_type="convert",
        params={"format": "mp4"},
        input_file=video_path,
    )
    db_session.add(task)
    db_session.commit()

    # Run worker task synchronously
    process_task(str(task_id))

    # Verify task in DB
    db_session.expire_all()
    updated = db_session.query(Task).filter(Task.id == task_id).first()
    assert updated.status == TaskStatus.done
    assert updated.output_file is not None
    assert os.path.exists(updated.output_file)
    assert updated.started_at is not None
    assert updated.finished_at is not None
    assert updated.worker_id is not None

    # Verify output video is valid
    probe_res = probe_video(updated.output_file)
    assert probe_res.duration > 0
    assert probe_res.width > 0

    # Clean up generated output file
    if os.path.exists(updated.output_file):
        os.remove(updated.output_file)


def test_process_task_scale_success(db_session, auth_client):
    """Test process_task successfully scales video resolution to 360p."""
    video_path = os.path.join(FIXTURES_DIR, "test_5s.mp4")
    assert os.path.exists(video_path)

    task_id = uuid.uuid4()
    task = Task(
        id=task_id,
        user_id=auth_client.user_id,
        status=TaskStatus.queued,
        operation_type="scale",
        params={"resolution": 360},
        input_file=video_path,
    )
    db_session.add(task)
    db_session.commit()

    # Run worker task synchronously
    process_task(str(task_id))

    db_session.expire_all()
    updated = db_session.query(Task).filter(Task.id == task_id).first()
    assert updated.status == TaskStatus.done
    assert updated.output_file is not None
    assert os.path.exists(updated.output_file)

    # Verify scaled resolution is height 360
    probe_res = probe_video(updated.output_file)
    assert probe_res.height == 360

    if os.path.exists(updated.output_file):
        os.remove(updated.output_file)


def test_process_task_pre_cancelled_exits_early(db_session, auth_client):
    """Test process_task does not execute if task was already marked cancelled."""
    video_path = os.path.join(FIXTURES_DIR, "test_5s.mp4")

    task_id = uuid.uuid4()
    task = Task(
        id=task_id,
        user_id=auth_client.user_id,
        status=TaskStatus.cancelled,
        operation_type="convert",
        input_file=video_path,
    )
    db_session.add(task)
    db_session.commit()

    process_task(str(task_id))

    db_session.expire_all()
    updated = db_session.query(Task).filter(Task.id == task_id).first()
    assert updated.status == TaskStatus.cancelled
    assert updated.output_file is None


def test_cancel_endpoint_queued_task(auth_client, db_session):
    """Test cancelling a queued task immediately marks it cancelled in DB."""
    task_id = uuid.uuid4()
    task = Task(
        id=task_id,
        user_id=auth_client.user_id,
        status=TaskStatus.queued,
        operation_type="convert",
        input_file="dummy.mp4",
    )
    db_session.add(task)
    db_session.commit()

    response = auth_client.post(f"/api/tasks/{task_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    db_session.expire_all()
    updated = db_session.query(Task).filter(Task.id == task_id).first()
    assert updated.status == TaskStatus.cancelled


def test_cancel_endpoint_running_task(auth_client, db_session):
    """Test cancelling a running task sets cancel flag in Redis."""
    task_id = uuid.uuid4()
    task = Task(
        id=task_id,
        user_id=auth_client.user_id,
        status=TaskStatus.running,
        operation_type="convert",
        input_file="dummy.mp4",
    )
    db_session.add(task)
    db_session.commit()

    response = auth_client.post(f"/api/tasks/{task_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_cancel_endpoint_already_done_returns_400(auth_client, db_session):
    """Test cancelling an already completed task returns 400 Bad Request."""
    task_id = uuid.uuid4()
    task = Task(
        id=task_id,
        user_id=auth_client.user_id,
        status=TaskStatus.done,
        operation_type="convert",
        input_file="dummy.mp4",
    )
    db_session.add(task)
    db_session.commit()

    response = auth_client.post(f"/api/tasks/{task_id}/cancel")
    assert response.status_code == 400
    assert "cannot cancel task with status 'done'" in response.json()["detail"].lower()


def test_cancel_endpoint_forbidden_for_non_owner(auth_client, client, db_session):
    """Test user cannot cancel another user's task (403 Forbidden)."""
    task_id = uuid.uuid4()
    task = Task(
        id=task_id,
        user_id=auth_client.user_id,
        status=TaskStatus.queued,
        operation_type="convert",
        input_file="dummy.mp4",
    )
    db_session.add(task)
    db_session.commit()

    # Another user logs in
    client.post("/api/register", json={"email": "attacker@example.com", "password": "password123"})
    client.post("/api/login", json={"email": "attacker@example.com", "password": "password123"})

    response = client.post(f"/api/tasks/{task_id}/cancel")
    assert response.status_code == 403
    assert "forbidden" in response.json()["detail"].lower()
