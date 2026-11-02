import io
import os
import shutil
import tempfile
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from worker_server.main import WORKER_TOKEN, app, tasks, tasks_lock
from worker_server.ffmpeg_runner import (
    Cancelled,
    WATERMARK_POSITIONS,
    build_ffmpeg_args,
    check_audio_stream,
    get_output_extension,
    parse_ffmpeg_progress_line,
    run_ffmpeg,
)


@pytest.fixture(autouse=True)
def clean_tasks_state():
    with tasks_lock:
        tasks.clear()
    yield
    with tasks_lock:
        tasks.clear()


@pytest.fixture
def client():
    return TestClient(app)


def test_health_check_does_not_require_token(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_auth_token_required_for_endpoints(client):
    response = client.get("/status")
    assert response.status_code == 401

    response = client.get("/status", headers={"X-Worker-Token": "wrong-token"})
    assert response.status_code == 401

    response = client.get("/status", headers={"X-Worker-Token": WORKER_TOKEN})
    assert response.status_code == 200
    data = response.json()
    assert "worker_id" in data
    assert "capacity" in data
    assert data["active_tasks"] == 0


def test_status_endpoint_returns_metrics(client):
    response = client.get("/status", headers={"X-Worker-Token": WORKER_TOKEN})
    assert response.status_code == 200
    data = response.json()
    assert "cpu_percent" in data
    assert "ram_percent" in data
    assert "uptime_sec" in data
    assert isinstance(data["active_task_ids"], list)


def test_execute_and_progress_flow(client, monkeypatch):
    task_id = str(uuid.uuid4())

    def fake_run_ffmpeg(cmd_args, total_sec, log_file_path, cancel_event=None, progress_callback=None):
        if progress_callback:
            progress_callback(50)
        time.sleep(0.05)

    monkeypatch.setattr("worker_server.main.run_ffmpeg", fake_run_ffmpeg)
    monkeypatch.setattr("worker_server.main.probe_video", lambda p: MagicMock(duration=10.0))

    video_bytes = b"fake video content for testing"
    response = client.post(
        "/execute",
        headers={"X-Worker-Token": WORKER_TOKEN},
        data={"task_id": task_id, "operation_type": "convert"},
        files={"file": ("test.mp4", io.BytesIO(video_bytes), "video/mp4")},
    )
    assert response.status_code == 202
    res_json = response.json()
    assert res_json["task_id"] == task_id
    assert res_json["status"] == "running"

    time.sleep(0.15)

    prog_resp = client.get(
        f"/tasks/{task_id}/progress",
        headers={"X-Worker-Token": WORKER_TOKEN},
    )
    assert prog_resp.status_code == 200
    p_data = prog_resp.json()
    assert p_data["task_id"] == task_id
    assert p_data["status"] == "done"
    assert p_data["progress"] == 100


def test_execute_is_idempotent(client, monkeypatch):
    task_id = str(uuid.uuid4())

    def fake_run_ffmpeg(cmd_args, total_sec, log_file_path, cancel_event=None, progress_callback=None):
        time.sleep(0.5)

    monkeypatch.setattr("worker_server.main.run_ffmpeg", fake_run_ffmpeg)

    video_bytes = b"fake video bytes"
    resp1 = client.post(
        "/execute",
        headers={"X-Worker-Token": WORKER_TOKEN},
        data={"task_id": task_id, "operation_type": "convert"},
        files={"file": ("test.mp4", io.BytesIO(video_bytes), "video/mp4")},
    )
    assert resp1.status_code == 202

    resp2 = client.post(
        "/execute",
        headers={"X-Worker-Token": WORKER_TOKEN},
        data={"task_id": task_id, "operation_type": "convert"},
        files={"file": ("test.mp4", io.BytesIO(video_bytes), "video/mp4")},
    )
    assert resp2.status_code == 200
    assert resp2.json()["task_id"] == task_id


def test_capacity_exceeded_returns_503(client, monkeypatch):
    monkeypatch.setattr("worker_server.main.WORKER_CAPACITY", 1)

    def fake_run_ffmpeg(cmd_args, total_sec, log_file_path, cancel_event=None, progress_callback=None):
        time.sleep(1.0)

    monkeypatch.setattr("worker_server.main.run_ffmpeg", fake_run_ffmpeg)

    t1 = str(uuid.uuid4())
    t2 = str(uuid.uuid4())
    video_bytes = b"sample video"

    r1 = client.post(
        "/execute",
        headers={"X-Worker-Token": WORKER_TOKEN},
        data={"task_id": t1, "operation_type": "convert"},
        files={"file": ("test1.mp4", io.BytesIO(video_bytes), "video/mp4")},
    )
    assert r1.status_code == 202

    r2 = client.post(
        "/execute",
        headers={"X-Worker-Token": WORKER_TOKEN},
        data={"task_id": t2, "operation_type": "convert"},
        files={"file": ("test2.mp4", io.BytesIO(video_bytes), "video/mp4")},
    )
    assert r2.status_code == 503
    assert "capacity reached" in r2.json()["detail"].lower()


def test_insufficient_storage_returns_507(client, monkeypatch):
    monkeypatch.setattr(
        "shutil.disk_usage",
        lambda path: MagicMock(total=100_000_000, used=99_000_000, free=10_000_000),
    )

    t = str(uuid.uuid4())
    r = client.post(
        "/execute",
        headers={"X-Worker-Token": WORKER_TOKEN},
        data={"task_id": t, "operation_type": "convert"},
        files={"file": ("test.mp4", io.BytesIO(b"data"), "video/mp4")},
    )
    assert r.status_code == 507


def test_cancel_task_endpoint(client, monkeypatch):
    task_id = str(uuid.uuid4())

    def fake_run_ffmpeg(cmd_args, total_sec, log_file_path, cancel_event=None, progress_callback=None):
        while not (cancel_event and cancel_event.is_set()):
            time.sleep(0.02)
        raise Cancelled()

    monkeypatch.setattr("worker_server.main.run_ffmpeg", fake_run_ffmpeg)

    client.post(
        "/execute",
        headers={"X-Worker-Token": WORKER_TOKEN},
        data={"task_id": task_id, "operation_type": "convert"},
        files={"file": ("test.mp4", io.BytesIO(b"video bytes"), "video/mp4")},
    )

    cancel_resp = client.post(
        f"/tasks/{task_id}/cancel",
        headers={"X-Worker-Token": WORKER_TOKEN},
    )
    assert cancel_resp.status_code == 200

    time.sleep(0.1)

    prog_resp = client.get(
        f"/tasks/{task_id}/progress",
        headers={"X-Worker-Token": WORKER_TOKEN},
    )
    assert prog_resp.status_code == 200
    assert prog_resp.json()["status"] == "cancelled"


def test_result_download_and_conflict_handling(client, monkeypatch):
    task_id = str(uuid.uuid4())

    def fake_run_ffmpeg(cmd_args, total_sec, log_file_path, cancel_event=None, progress_callback=None):
        time.sleep(0.5)

    monkeypatch.setattr("worker_server.main.run_ffmpeg", fake_run_ffmpeg)

    client.post(
        "/execute",
        headers={"X-Worker-Token": WORKER_TOKEN},
        data={"task_id": task_id, "operation_type": "convert"},
        files={"file": ("test.mp4", io.BytesIO(b"video"), "video/mp4")},
    )

    r_409 = client.get(f"/tasks/{task_id}/result", headers={"X-Worker-Token": WORKER_TOKEN})
    assert r_409.status_code == 409

    with tasks_lock:
        task_state = tasks[task_id]
        task_state.status = "done"
        dummy_out = task_state.input_file
        task_state.output_file = dummy_out

    r_done = client.get(f"/tasks/{task_id}/result", headers={"X-Worker-Token": WORKER_TOKEN})
    assert r_done.status_code == 200


def test_delete_task_idempotent_cleanup(client):
    task_id = str(uuid.uuid4())

    r1 = client.delete(f"/tasks/{task_id}", headers={"X-Worker-Token": WORKER_TOKEN})
    assert r1.status_code == 200

    video_bytes = b"video data"
    client.post(
        "/execute",
        headers={"X-Worker-Token": WORKER_TOKEN},
        data={"task_id": task_id, "operation_type": "convert"},
        files={"file": ("test.mp4", io.BytesIO(video_bytes), "video/mp4")},
    )
    assert task_id in tasks

    r2 = client.delete(f"/tasks/{task_id}", headers={"X-Worker-Token": WORKER_TOKEN})
    assert r2.status_code == 200
    assert task_id not in tasks


def test_build_ffmpeg_args_all_operations():
    t_conv = {"operation_type": "convert", "input_file": "/tmp/in.mp4", "params": {"preset": "fast"}}
    args_conv = build_ffmpeg_args(t_conv, "/tmp/out.mp4")
    assert "-c:v" in args_conv and "libx264" in args_conv
    assert "-preset" in args_conv and "fast" in args_conv

    t_scale = {"operation_type": "scale", "input_file": "/tmp/in.mp4", "params": {"resolution": 480}}
    args_scale = build_ffmpeg_args(t_scale, "/tmp/out.mp4")
    assert "scale=-2:480" in args_scale
    assert "-crf" in args_scale and "25" in args_scale

    t_wm = {
        "operation_type": "watermark",
        "input_file": "/tmp/in.mp4",
        "params": {"logo_path": "/tmp/logo.png", "position": "top_left", "opacity": 0.5},
    }
    args_wm = build_ffmpeg_args(t_wm, "/tmp/out.mp4")
    assert "-filter_complex" in args_wm
    assert "colorchannelmixer=aa=0.5" in str(args_wm)
    assert "overlay=10:10" in str(args_wm)

    t_mute = {"operation_type": "audio", "input_file": "/tmp/in.mp4", "params": {"action": "mute"}}
    args_mute = build_ffmpeg_args(t_mute, "/tmp/out.mp4")
    assert "-an" in args_mute
    assert "-c:v" in args_mute and "copy" in args_mute

    t_extract = {"operation_type": "extract_audio", "input_file": "/tmp/in.mp4", "params": {}}
    args_extract = build_ffmpeg_args(t_extract, "/tmp/out.mp3")
    assert "-vn" in args_extract
    assert "-c:a" in args_extract and "libmp3lame" in args_extract

