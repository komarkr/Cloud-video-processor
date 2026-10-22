from datetime import datetime, timezone
import json
import uuid
import pytest

from app.models import Task, TaskStatus, User
from app.services.security import hash_password
from tests.conftest import FakeRedis


def test_admin_overview_requires_auth(client):
    """GET /api/admin/overview without authentication returns 401."""
    resp = client.get("/api/admin/overview")
    assert resp.status_code == 401


def test_admin_overview_forbidden_for_regular_user(client, db_session):
    """GET /api/admin/overview with non-admin user returns 403 Forbidden."""
    client.post("/api/auth/register", json={"email": "regular@example.com", "password": "password123"})
    client.post("/api/auth/login", json={"email": "regular@example.com", "password": "password123"})

    resp = client.get("/api/admin/overview")
    assert resp.status_code == 403
    assert "Admin privileges required" in resp.json()["detail"]


def test_admin_overview_success(client, db_session, monkeypatch):
    """GET /api/admin/overview with admin user returns 200 and complete cluster metrics."""
    # Ensure admin user exists and login
    admin_email = "admin_day11@example.com"
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

    # Create regular user with tasks
    reg_user = User(
        id=uuid.uuid4(),
        email="client_video@example.com",
        hashed_password=hash_password("pw123456"),
        is_admin=False,
    )
    db_session.add(reg_user)
    db_session.commit()

    # Pre-seed tasks of various statuses
    t_queued = Task(
        id=uuid.uuid4(),
        user_id=reg_user.id,
        status=TaskStatus.queued,
        operation_type="convert",
        input_file="/data/uploads/q.mp4",
        duration_sec=40.0,
    )
    t_running = Task(
        id=uuid.uuid4(),
        user_id=reg_user.id,
        status=TaskStatus.running,
        operation_type="scale",
        input_file="/data/uploads/r.mp4",
        worker_id="worker-node-1",
        duration_sec=60.0,
        started_at=datetime.now(timezone.utc),
    )
    t_done = Task(
        id=uuid.uuid4(),
        user_id=reg_user.id,
        status=TaskStatus.done,
        operation_type="watermark",
        input_file="/data/uploads/d.mp4",
        output_file="/data/outputs/d.mp4",
        worker_id="worker-node-2",
        duration_sec=30.0,
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add_all([t_queued, t_running, t_done])
    db_session.commit()

    # Mock Redis with worker heartbeat and autoscaler event
    mock_r = FakeRedis()
    mock_r.hset(
        "hb:worker-node-1",
        mapping={
            "hostname": "worker-node-1",
            "cpu": "34.5",
            "ram": "55.0",
            "current_task": str(t_running.id),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
    mock_r.rpush(
        "autoscaler:log",
        json.dumps({
            "action": "scale_up",
            "workers": 3,
            "queued": 5,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }),
    )

    import redis
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: mock_r)

    # Login as admin
    login_resp = client.post("/api/auth/login", json={"email": admin_email, "password": admin_pw})
    assert login_resp.status_code == 200

    resp = client.get("/api/admin/overview")
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "ok"
    assert "status_counts" in data
    assert data["status_counts"]["queued"] >= 1
    assert data["status_counts"]["running"] >= 1
    assert data["status_counts"]["done"] >= 1
    assert data["queue_length"] >= 1

    # Check workers
    assert len(data["workers"]) >= 1
    w = next((w for w in data["workers"] if w["hostname"] == "worker-node-1"), None)
    assert w is not None
    assert w["cpu"] == 34.5
    assert w["ram"] == 55.0
    assert w["current_task"] == str(t_running.id)

    # Check recent tasks list
    assert len(data["recent_tasks"]) >= 3
    found_task = next((t for t in data["recent_tasks"] if t["id"] == str(t_running.id)), None)
    assert found_task is not None
    assert found_task["user_email"] == "client_video@example.com"
    assert found_task["operation_type"] == "scale"
    assert found_task["worker_id"] == "worker-node-1"

    # Check autoscaler events
    assert len(data["autoscaler_events"]) >= 1
    assert data["autoscaler_events"][0]["action"] == "scale_up"


def test_admin_cancel_any_task(client, db_session):
    """Admin can cancel another user's task via POST /api/admin/tasks/{id}/cancel."""
    admin_email = "admin_cancel@example.com"
    admin_pw = "admin_secret_123"

    admin = User(
        id=uuid.uuid4(),
        email=admin_email,
        hashed_password=hash_password(admin_pw),
        is_admin=True,
    )
    regular = User(
        id=uuid.uuid4(),
        email="victim@example.com",
        hashed_password=hash_password("user_pass_123"),
        is_admin=False,
    )
    db_session.add_all([admin, regular])
    db_session.commit()

    task = Task(
        id=uuid.uuid4(),
        user_id=regular.id,
        status=TaskStatus.queued,
        operation_type="convert",
        input_file="/data/uploads/victim.mp4",
        duration_sec=45.0,
    )
    db_session.add(task)
    db_session.commit()

    # Regular user cannot use admin cancel endpoint
    client.post("/api/auth/login", json={"email": "victim@example.com", "password": "user_pass_123"})
    forbidden_resp = client.post(f"/api/admin/tasks/{task.id}/cancel")
    assert forbidden_resp.status_code == 403

    # Admin cancels task
    client.post("/api/auth/login", json={"email": admin_email, "password": admin_pw})
    cancel_resp = client.post(f"/api/admin/tasks/{task.id}/cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["status"] == "ok"

    db_session.refresh(task)
    assert task.status == TaskStatus.cancelled
    assert "cancelled by administrator" in task.error_message.lower()

    # Cancelling an already cancelled task returns 400
    err_resp = client.post(f"/api/admin/tasks/{task.id}/cancel")
    assert err_resp.status_code == 400
