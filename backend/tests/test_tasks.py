import os
import uuid
import pytest
from app.config import settings
from app.models import Task, TaskStatus, User
from app.services.security import hash_password

FIXTURES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "fixtures", "videos"))


@pytest.fixture
def auth_client(client, db_session):
    """Register and login a test user, returning client with authenticated session."""
    email = "taskuser@example.com"
    pwd = "password123"
    client.post("/api/register", json={"email": email, "password": pwd})
    client.post("/api/login", json={"email": email, "password": pwd})

    user = db_session.query(User).filter(User.email == email).first()
    client.user_id = user.id
    return client


def test_create_task_good_video_5s(auth_client, db_session):
    """Test uploading a valid 5s video creates a task with status 'queued'."""
    video_path = os.path.join(FIXTURES_DIR, "test_5s.mp4")
    assert os.path.exists(video_path)

    with open(video_path, "rb") as f:
        response = auth_client.post(
            "/api/tasks",
            data={"operation_type": "convert", "params": '{"format": "mp4"}'},
            files={"file": ("test_5s.mp4", f, "video/mp4")},
        )

    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "queued"
    assert data["operation_type"] == "convert"
    assert data["params"] == {"format": "mp4"}
    assert "id" in data
    assert os.path.exists(data["input_file"])

    # Clean up file created by test
    if os.path.exists(data["input_file"]):
        os.remove(data["input_file"])


def test_create_task_boundary_video_120s(auth_client):
    """Test uploading a 120s boundary video succeeds (<= 120s limit)."""
    video_path = os.path.join(FIXTURES_DIR, "test_120s.mp4")
    assert os.path.exists(video_path)

    with open(video_path, "rb") as f:
        response = auth_client.post(
            "/api/tasks",
            data={"operation_type": "scale"},
            files={"file": ("test_120s.mp4", f, "video/mp4")},
        )

    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "queued"
    assert os.path.exists(data["input_file"])

    # Clean up
    if os.path.exists(data["input_file"]):
        os.remove(data["input_file"])


def test_create_task_too_long_video_150s_returns_422(auth_client):
    """Test uploading a 150s video fails validation (> 120s) and cleans up file."""
    video_path = os.path.join(FIXTURES_DIR, "test_150s.mp4")
    assert os.path.exists(video_path)

    initial_files = set(os.listdir(settings.upload_dir)) if os.path.exists(settings.upload_dir) else set()

    with open(video_path, "rb") as f:
        response = auth_client.post(
            "/api/tasks",
            files={"file": ("test_150s.mp4", f, "video/mp4")},
        )

    assert response.status_code == 422
    data = response.json()
    assert "exceeds maximum allowed 120" in data["detail"].lower()

    # Ensure no leftover file exists in upload dir
    current_files = set(os.listdir(settings.upload_dir)) if os.path.exists(settings.upload_dir) else set()
    assert current_files == initial_files


def test_create_task_not_a_video_returns_422(auth_client):
    """Test uploading a non-video file with .mp4 extension returns 422 and cleans up file."""
    video_path = os.path.join(FIXTURES_DIR, "not_a_video.mp4")
    assert os.path.exists(video_path)

    initial_files = set(os.listdir(settings.upload_dir)) if os.path.exists(settings.upload_dir) else set()

    with open(video_path, "rb") as f:
        response = auth_client.post(
            "/api/tasks",
            files={"file": ("not_a_video.mp4", f, "video/mp4")},
        )

    assert response.status_code == 422
    data = response.json()
    assert "invalid" in data["detail"].lower() or "corrupt" in data["detail"].lower()

    # Ensure file was deleted
    current_files = set(os.listdir(settings.upload_dir)) if os.path.exists(settings.upload_dir) else set()
    assert current_files == initial_files


def test_create_task_oversized_resolution_returns_422(auth_client):
    """Test uploading a 2560x1440 video returns 422 (exceeds 1920x1080) and cleans up file."""
    video_path = os.path.join(FIXTURES_DIR, "test_oversized_res.mp4")
    assert os.path.exists(video_path)

    initial_files = set(os.listdir(settings.upload_dir)) if os.path.exists(settings.upload_dir) else set()

    with open(video_path, "rb") as f:
        response = auth_client.post(
            "/api/tasks",
            files={"file": ("test_oversized_res.mp4", f, "video/mp4")},
        )

    assert response.status_code == 422
    data = response.json()
    assert "1920x1080" in data["detail"]

    # Ensure file was deleted
    current_files = set(os.listdir(settings.upload_dir)) if os.path.exists(settings.upload_dir) else set()
    assert current_files == initial_files


def test_create_task_file_size_exceeds_limit_returns_422(auth_client, monkeypatch):
    """Test that exceeding MAX_FILE_MB aborts streaming write, deletes file, and returns 422."""
    # Temporarily set max_file_mb to 0 (or a very tiny number like 0.01 MB ~ 10KB)
    monkeypatch.setattr(settings, "max_file_mb", 0.01)

    video_path = os.path.join(FIXTURES_DIR, "test_5s.mp4")  # ~26KB > 10KB
    initial_files = set(os.listdir(settings.upload_dir)) if os.path.exists(settings.upload_dir) else set()

    with open(video_path, "rb") as f:
        response = auth_client.post(
            "/api/tasks",
            files={"file": ("test_5s.mp4", f, "video/mp4")},
        )

    assert response.status_code == 422
    data = response.json()
    assert "file size exceeds" in data["detail"].lower()

    # Ensure file was deleted
    current_files = set(os.listdir(settings.upload_dir)) if os.path.exists(settings.upload_dir) else set()
    assert current_files == initial_files


def test_create_task_active_limit_exceeded_returns_429(auth_client, db_session):
    """Test that when a user already has 5 active tasks (queued/running), uploading returns 429."""
    user_id = auth_client.user_id

    # Pre-seed 5 active tasks for this user
    for i in range(5):
        t = Task(
            id=uuid.uuid4(),
            user_id=user_id,
            status=TaskStatus.queued if i % 2 == 0 else TaskStatus.running,
            operation_type="convert",
            input_file=f"dummy_{i}.mp4",
        )
        db_session.add(t)
    db_session.commit()

    video_path = os.path.join(FIXTURES_DIR, "test_5s.mp4")
    with open(video_path, "rb") as f:
        response = auth_client.post(
            "/api/tasks",
            files={"file": ("test_5s.mp4", f, "video/mp4")},
        )

    assert response.status_code == 429
    data = response.json()
    assert "active tasks limit exceeded" in data["detail"].lower()


def test_create_task_global_queue_full_returns_503(auth_client, db_session, monkeypatch):
    """Test that when global queue reaches MAX_QUEUE, uploading returns 503."""
    # Temporarily set MAX_QUEUE to 2 for fast test
    monkeypatch.setattr(settings, "max_queue", 2)

    user_id = auth_client.user_id
    # Add 2 queued tasks to hit global limit
    for i in range(2):
        t = Task(
            id=uuid.uuid4(),
            user_id=user_id,
            status=TaskStatus.queued,
            operation_type="convert",
            input_file=f"dummy_q_{i}.mp4",
        )
        db_session.add(t)
    db_session.commit()

    video_path = os.path.join(FIXTURES_DIR, "test_5s.mp4")
    with open(video_path, "rb") as f:
        response = auth_client.post(
            "/api/tasks",
            files={"file": ("test_5s.mp4", f, "video/mp4")},
        )

    assert response.status_code == 503
    data = response.json()
    assert "queue is at maximum capacity" in data["detail"].lower()


def test_get_tasks_returns_only_own_tasks(client, db_session):
    """Test GET /api/tasks returns only tasks belonging to the authenticated user."""
    pwd = "password123"
    # Register and login user A
    email_a = "user_a@example.com"
    client.post("/api/register", json={"email": email_a, "password": pwd})
    client.post("/api/login", json={"email": email_a, "password": pwd})
    user_a = db_session.query(User).filter(User.email == email_a).first()

    task_a = Task(
        id=uuid.uuid4(),
        user_id=user_a.id,
        status=TaskStatus.queued,
        operation_type="convert",
        input_file="file_a.mp4",
    )
    db_session.add(task_a)

    # Register user B
    email_b = "user_b@example.com"
    client.post("/api/register", json={"email": email_b, "password": pwd})
    user_b = db_session.query(User).filter(User.email == email_b).first()

    task_b = Task(
        id=uuid.uuid4(),
        user_id=user_b.id,
        status=TaskStatus.queued,
        operation_type="scale",
        input_file="file_b.mp4",
    )
    db_session.add(task_b)
    db_session.commit()

    # User A is currently logged in -> sees only task A
    response_a = client.get("/api/tasks")
    assert response_a.status_code == 200
    a_tasks = response_a.json()
    assert len(a_tasks) == 1
    assert a_tasks[0]["id"] == str(task_a.id)

    # Now login as user B -> sees only task B
    client.post("/api/login", json={"email": email_b, "password": pwd})
    response_b = client.get("/api/tasks")
    assert response_b.status_code == 200
    b_tasks = response_b.json()
    assert len(b_tasks) == 1
    assert b_tasks[0]["id"] == str(task_b.id)


def test_get_task_by_id_ownership_verification(client, db_session):
    """Test GET /api/tasks/{id} allows owner and returns 403 for non-owner."""
    pwd = "password123"
    email_a = "owner_a@example.com"
    client.post("/api/register", json={"email": email_a, "password": pwd})
    client.post("/api/login", json={"email": email_a, "password": pwd})
    user_a = db_session.query(User).filter(User.email == email_a).first()

    task_a = Task(
        id=uuid.uuid4(),
        user_id=user_a.id,
        status=TaskStatus.done,
        operation_type="watermark",
        input_file="file_a.mp4",
    )
    db_session.add(task_a)
    db_session.commit()

    # User A (owner) requests task -> 200 OK
    res_owner = client.get(f"/api/tasks/{task_a.id}")
    assert res_owner.status_code == 200
    assert res_owner.json()["id"] == str(task_a.id)

    # Register and login User B
    email_b = "intruder_b@example.com"
    client.post("/api/register", json={"email": email_b, "password": pwd})
    client.post("/api/login", json={"email": email_b, "password": pwd})

    # User B requests User A's task -> 403 Forbidden
    res_other = client.get(f"/api/tasks/{task_a.id}")
    assert res_other.status_code == 403
    assert "forbidden" in res_other.json()["detail"].lower()

    # Request non-existent task -> 404
    non_existent = uuid.uuid4()
    res_404 = client.get(f"/api/tasks/{non_existent}")
    assert res_404.status_code == 404
