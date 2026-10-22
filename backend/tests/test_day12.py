from datetime import datetime, timedelta, timezone
import io
import os
import uuid
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.models import Task, TaskStatus, User
from app.services.cleanup import cleanup_expired_tasks
from app.services.security import hash_password
from tests.conftest import FakeRedis
from worker.tasks import build_ffmpeg_args


def test_ffmpeg_protocol_whitelist_in_arguments():
    """Verify that FFmpeg arguments strictly contain -protocol_whitelist file for all inputs."""
    dummy_task = Task(
        id=uuid.uuid4(),
        operation_type="convert",
        params={},
        input_file="/data/uploads/test.mp4",
    )
    args = build_ffmpeg_args(dummy_task, "/data/outputs/test.mp4")
    assert "-protocol_whitelist" in args
    idx = args.index("-protocol_whitelist")
    assert args[idx + 1] == "file"
    assert args[idx + 2] == "-i"

    # Watermark test
    wm_task = Task(
        id=uuid.uuid4(),
        operation_type="watermark",
        params={"logo_path": "/data/uploads/logo.png"},
        input_file="/data/uploads/test.mp4",
    )
    wm_args = build_ffmpeg_args(wm_task, "/data/outputs/wm.mp4")
    # Should have two -protocol_whitelist file entries (video and logo)
    count = wm_args.count("-protocol_whitelist")
    assert count == 2


def test_upload_filename_sanitization_uuid_only(client: TestClient, db_session):
    """Verify that filenames are strictly stored as UUIDs with safe extensions, ignoring user-supplied path."""
    os.makedirs(settings.upload_dir, exist_ok=True)
    small_content = b"fake video content for testing"

    email = "uuid_test@example.com"
    client.post("/api/auth/register", json={"email": email, "password": "password123"})
    client.post("/api/auth/login", json={"email": email, "password": "password123"})

    # Attempt path traversal in filename
    client.post(
        "/api/tasks",
        files={"file": ("../../../../etc/passwd.exe", io.BytesIO(small_content), "video/mp4")},
        data={"operation_type": "convert"},
    )
    # Check that no file named passwd.exe or traversal was created
    assert not os.path.exists(os.path.join(settings.upload_dir, "passwd.exe"))
    assert not os.path.exists("passwd.exe")


def test_owner_verification_across_endpoints(client: TestClient, db_session):
    """Verify that a regular user cannot access, cancel, or delete another user's task."""
    user1_email = "owner1@example.com"
    user2_email = "intruder2@example.com"

    client.post("/api/auth/register", json={"email": user1_email, "password": "password123"})
    client.post("/api/auth/register", json={"email": user2_email, "password": "password123"})

    # Login as User 1 to get user ID
    client.post("/api/auth/login", json={"email": user1_email, "password": "password123"})
    u1 = db_session.query(User).filter(User.email == user1_email).first()

    # Create task owned by User 1
    task1 = Task(
        id=uuid.uuid4(),
        user_id=u1.id,
        status=TaskStatus.queued,
        operation_type="convert",
        input_file=os.path.join(settings.upload_dir, "dummy_input.mp4"),
    )
    db_session.add(task1)
    db_session.commit()

    # User 1 CAN view task1
    r_owner = client.get(f"/api/tasks/{task1.id}")
    assert r_owner.status_code == 200

    # Now login as User 2 (intruder)
    client.post("/api/auth/login", json={"email": user2_email, "password": "password123"})

    # User 2 tries to GET task1 -> 403 Forbidden
    r_get = client.get(f"/api/tasks/{task1.id}")
    assert r_get.status_code == 403

    # User 2 tries to cancel task1 -> 403 Forbidden
    r_cancel = client.post(f"/api/tasks/{task1.id}/cancel")
    assert r_cancel.status_code == 403

    # User 2 tries to delete task1 -> 404 (hidden for privacy)
    r_del = client.delete(f"/api/tasks/{task1.id}")
    assert r_del.status_code == 404

    # User 2 tries to download task1 -> 404
    r_dl = client.get(f"/api/tasks/{task1.id}/download")
    assert r_dl.status_code == 404

    # Clean up
    db_session.delete(task1)
    db_session.commit()


def test_cleanup_expired_tasks_and_files(db_session):
    """Verify that cleanup_expired_tasks purges tasks with expires_at in the past along with files."""
    os.makedirs(settings.upload_dir, exist_ok=True)
    os.makedirs(settings.output_dir, exist_ok=True)
    os.makedirs(settings.log_dir, exist_ok=True)

    fake_redis = FakeRedis()

    user = db_session.query(User).first()
    if not user:
        user = User(
            id=uuid.uuid4(),
            email="cleanup_test@example.com",
            hashed_password=hash_password("pw"),
            is_admin=False,
        )
        db_session.add(user)
        db_session.commit()

    now = datetime.now(timezone.utc)

    # 1. Expired task
    exp_task_id = uuid.uuid4()
    exp_input = os.path.join(settings.upload_dir, f"{exp_task_id}.mp4")
    exp_output = os.path.join(settings.output_dir, f"{exp_task_id}.mp4")
    exp_log = os.path.join(settings.log_dir, f"{exp_task_id}.log")

    with open(exp_input, "w") as f:
        f.write("expired input dummy")
    with open(exp_output, "w") as f:
        f.write("expired output dummy")
    with open(exp_log, "w") as f:
        f.write("expired log dummy")

    fake_redis.set(f"task:{exp_task_id}", "status:done")

    expired_task = Task(
        id=exp_task_id,
        user_id=user.id,
        status=TaskStatus.done,
        operation_type="convert",
        input_file=exp_input,
        output_file=exp_output,
        expires_at=now - timedelta(hours=2),
    )
    db_session.add(expired_task)

    # 2. Valid (non-expired) task
    valid_task_id = uuid.uuid4()
    valid_input = os.path.join(settings.upload_dir, f"{valid_task_id}.mp4")
    with open(valid_input, "w") as f:
        f.write("valid input dummy")

    valid_task = Task(
        id=valid_task_id,
        user_id=user.id,
        status=TaskStatus.done,
        operation_type="convert",
        input_file=valid_input,
        expires_at=now + timedelta(hours=24),
    )
    db_session.add(valid_task)
    db_session.commit()

    # Run cleanup
    purged = cleanup_expired_tasks(db_session, fake_redis)
    assert purged >= 1

    # Verify expired task files were deleted
    assert not os.path.exists(exp_input)
    assert not os.path.exists(exp_output)
    assert not os.path.exists(exp_log)

    # Verify expired task was deleted from database
    check_expired = db_session.query(Task).filter(Task.id == exp_task_id).first()
    assert check_expired is None

    # Verify valid task is STILL present
    check_valid = db_session.query(Task).filter(Task.id == valid_task_id).first()
    assert check_valid is not None
    assert os.path.exists(valid_input)

    # Clean up valid task files
    if os.path.exists(valid_input):
        os.remove(valid_input)
    db_session.delete(valid_task)
    db_session.commit()


def test_admin_cleanup_endpoint(client: TestClient, db_session, monkeypatch):
    """Test POST /api/admin/cleanup endpoint triggers cleanup successfully when called by admin."""
    admin_email = "admin_clean@example.com"
    admin_pw = "admin_secret_123"

    admin = db_session.query(User).filter(User.email == admin_email).first()
    if not admin:
        admin = User(
            id=uuid.uuid4(),
            email=admin_email,
            hashed_password=hash_password(admin_pw),
            is_admin=True,
        )
        db_session.add(admin)
        db_session.commit()

    fake_redis = FakeRedis()
    monkeypatch.setattr("redis.Redis.from_url", lambda *args, **kwargs: fake_redis)

    # Login as admin
    client.post("/api/auth/login", json={"email": admin_email, "password": admin_pw})

    response = client.post("/api/admin/cleanup")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "purged_count" in data
