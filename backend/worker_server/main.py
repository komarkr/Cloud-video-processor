import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
import shutil
import socket
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
import httpx
import psutil

from app.services.probe import probe_video
from worker_server.ffmpeg_runner import (
    Cancelled,
    build_ffmpeg_args,
    check_audio_stream,
    get_output_extension,
    run_ffmpeg,
)

logger = logging.getLogger("worker_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] (worker_server) %(message)s")

WORKER_ID = os.getenv("WORKER_ID") or socket.gethostname()
WORKER_PORT = int(os.getenv("WORKER_PORT", "9000"))
WORKER_CAPACITY = int(os.getenv("WORKER_CAPACITY", "1"))
DISPATCHER_URL = os.getenv("DISPATCHER_URL", "")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "super_secret_worker_token")
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "100"))


def _resolve_default_data_dir() -> str:
    if os.name != "nt":
        return "/tmp/cvp-worker"
    return os.path.join(tempfile.gettempdir(), "cvp-worker")


WORKER_DATA_DIR = os.getenv("WORKER_DATA_DIR") or _resolve_default_data_dir()


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


WORKER_PUBLIC_URL = os.getenv("WORKER_PUBLIC_URL") or f"http://{_get_local_ip()}:{WORKER_PORT}"

if WORKER_TOKEN in ["", "super_secret_worker_token", "default_token", "change_me"]:
    logger.warning("WORKER_TOKEN is using an insecure or default placeholder value! Change in production.")


@dataclass
class TaskState:
    task_id: str
    status: str
    progress: int = 0
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    input_file: Optional[str] = None
    output_file: Optional[str] = None
    logo_file: Optional[str] = None
    log_file: Optional[str] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)


tasks: Dict[str, TaskState] = {}
tasks_lock = threading.Lock()
start_time = time.time()
registration_task: Optional[asyncio.Task] = None


def verify_worker_token(x_worker_token: Optional[str] = Header(None, alias="X-Worker-Token")):
    if not WORKER_TOKEN or x_worker_token != WORKER_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Worker-Token header",
        )


def _ensure_worker_dirs():
    os.makedirs(os.path.join(WORKER_DATA_DIR, "inputs"), exist_ok=True)
    os.makedirs(os.path.join(WORKER_DATA_DIR, "outputs"), exist_ok=True)
    os.makedirs(os.path.join(WORKER_DATA_DIR, "logs"), exist_ok=True)


async def _registration_loop():
    if not DISPATCHER_URL:
        return
    url = f"{DISPATCHER_URL.rstrip('/')}/internal/workers/register"
    payload = {
        "worker_id": WORKER_ID,
        "url": WORKER_PUBLIC_URL,
        "capacity": WORKER_CAPACITY,
    }
    headers = {"X-Worker-Token": WORKER_TOKEN}

    delay = 1.0
    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.post(url, json=payload, headers=headers)
                if res.status_code in [200, 201]:
                    logger.info("Worker %s successfully registered with Dispatcher at %s", WORKER_ID, url)
                    delay = 10.0
                else:
                    logger.warning("Dispatcher registration returned status %d", res.status_code)
                    delay = min(10.0, delay * 2)
        except Exception as exc:
            logger.debug("Registration attempt failed: %s. Retrying in %.1fs...", exc, delay)
            delay = min(10.0, delay * 2)

        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            break


@asynccontextmanager
async def lifespan(app: FastAPI):
    global registration_task
    _ensure_worker_dirs()
    try:
        psutil.cpu_percent(interval=None)
    except Exception:
        pass

    if DISPATCHER_URL:
        registration_task = asyncio.create_task(_registration_loop())

    yield

    if registration_task:
        registration_task.cancel()
        try:
            await registration_task
        except asyncio.CancelledError:
            pass

    if DISPATCHER_URL:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post(
                    f"{DISPATCHER_URL.rstrip('/')}/internal/workers/deregister",
                    json={"worker_id": WORKER_ID},
                    headers={"X-Worker-Token": WORKER_TOKEN},
                )
        except Exception:
            pass


app = FastAPI(
    title="CVP Worker Server",
    description="HTTP Worker Server for Cloud Video Processor",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", summary="Health check endpoint (no auth required)")
def health_check():
    return {"status": "ok"}


@app.get("/status", dependencies=[Depends(verify_worker_token)], summary="Worker status and resource metrics")
def get_status():
    with tasks_lock:
        active_ids = [t.task_id for t in tasks.values() if t.status == "running"]

    try:
        cpu = psutil.cpu_percent(interval=None)
    except Exception:
        cpu = 0.0

    try:
        ram = psutil.virtual_memory().percent
    except Exception:
        ram = 0.0

    uptime = time.time() - start_time

    return {
        "worker_id": WORKER_ID,
        "url": WORKER_PUBLIC_URL,
        "capacity": WORKER_CAPACITY,
        "active_tasks": len(active_ids),
        "active_task_ids": active_ids,
        "cpu_percent": round(cpu, 1),
        "ram_percent": round(ram, 1),
        "uptime_sec": int(uptime),
    }


def _execute_processing_sync(task_state: TaskState, op: str, params: dict, duration_sec: Optional[float]):
    try:
        if task_state.cancel_event.is_set():
            raise Cancelled()

        total_sec = float(duration_sec or 0.0)
        if total_sec <= 0:
            try:
                probe_res = probe_video(task_state.input_file or "")
                total_sec = max(0.1, float(probe_res.duration))
            except Exception:
                total_sec = 1.0

        if op in ["extract_audio", "extract"] or (op == "audio" and params.get("action") not in ["mute", "remove"]):
            has_audio = check_audio_stream(task_state.input_file or "")
            if not has_audio:
                raise RuntimeError("Input video does not contain an audio stream to extract.")

        ext = get_output_extension({"operation_type": op, "params": params})
        outputs_dir = os.path.join(WORKER_DATA_DIR, "outputs")
        output_path = os.path.join(outputs_dir, f"{task_state.task_id}{ext}")
        task_state.output_file = output_path

        cmd_args = build_ffmpeg_args(
            {"operation_type": op, "input_file": task_state.input_file, "params": params},
            output_path,
        )

        def on_progress(pct: int):
            with tasks_lock:
                task_state.progress = min(99, max(0, pct))

        run_ffmpeg(
            cmd_args=cmd_args,
            total_sec=total_sec,
            log_file_path=task_state.log_file or os.path.join(WORKER_DATA_DIR, "logs", f"{task_state.task_id}.log"),
            cancel_event=task_state.cancel_event,
            progress_callback=on_progress,
        )

        with tasks_lock:
            task_state.status = "done"
            task_state.progress = 100
            task_state.finished_at = datetime.now(timezone.utc).isoformat()
            logger.info("Task %s completed successfully on worker %s", task_state.task_id, WORKER_ID)

    except Cancelled:
        logger.info("Task %s was cancelled on worker %s", task_state.task_id, WORKER_ID)
        with tasks_lock:
            task_state.status = "cancelled"
            task_state.finished_at = datetime.now(timezone.utc).isoformat()
            task_state.error = "Task cancelled by user"
        if task_state.output_file and os.path.exists(task_state.output_file):
            try:
                os.remove(task_state.output_file)
            except OSError:
                pass

    except Exception as exc:
        logger.error("Task %s failed on worker %s: %s", task_state.task_id, WORKER_ID, exc)
        with tasks_lock:
            task_state.status = "failed"
            task_state.error = str(exc)
            task_state.finished_at = datetime.now(timezone.utc).isoformat()


@app.post("/execute", dependencies=[Depends(verify_worker_token)], summary="Execute video task")
async def execute_task(
    task_id: str = Form(...),
    operation_type: str = Form("convert"),
    params: Optional[str] = Form(None),
    duration_sec: Optional[float] = Form(None),
    file: UploadFile = File(...),
    logo: Optional[UploadFile] = File(None),
):
    with tasks_lock:
        existing = tasks.get(task_id)
        if existing:
            return JSONResponse(
                status_code=200,
                content={
                    "task_id": existing.task_id,
                    "status": existing.status,
                    "progress": existing.progress,
                    "error": existing.error,
                    "started_at": existing.started_at,
                    "finished_at": existing.finished_at,
                },
            )

        active_count = sum(1 for t in tasks.values() if t.status == "running")
        if active_count >= WORKER_CAPACITY:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Worker {WORKER_ID} capacity reached ({active_count}/{WORKER_CAPACITY})",
            )

    _ensure_worker_dirs()
    disk = shutil.disk_usage(WORKER_DATA_DIR)
    if disk.free < 50 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail="Insufficient storage space on worker",
        )

    parsed_params: Dict[str, Any] = {}
    if params:
        try:
            p = json.loads(params)
            if isinstance(p, dict):
                parsed_params = p
        except Exception:
            parsed_params = {}

    parsed_params.pop("logo_path", None)

    inputs_dir = os.path.join(WORKER_DATA_DIR, "inputs")
    logs_dir = os.path.join(WORKER_DATA_DIR, "logs")

    original_name = file.filename or "video.mp4"
    _, ext = os.path.splitext(original_name)
    safe_ext = ext.lower() if ext.lower() in [".mp4", ".mov", ".mkv", ".webm", ".avi"] else ".mp4"
    input_file_path = os.path.join(inputs_dir, f"{task_id}{safe_ext}")

    max_bytes = MAX_FILE_MB * 1024 * 1024
    total_bytes = 0

    try:
        with open(input_file_path, "wb") as out_f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    out_f.close()
                    if os.path.exists(input_file_path):
                        os.remove(input_file_path)
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Video file exceeds maximum allowed size of {MAX_FILE_MB} MB.",
                    )
                out_f.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        if os.path.exists(input_file_path):
            os.remove(input_file_path)
        raise HTTPException(status_code=500, detail=f"Failed to save input video: {exc}")

    logo_file_path: Optional[str] = None
    if logo and logo.filename:
        logo_file_path = os.path.join(inputs_dir, f"{task_id}_logo.png")
        try:
            with open(logo_file_path, "wb") as logo_out:
                while True:
                    l_chunk = await logo.read(256 * 1024)
                    if not l_chunk:
                        break
                    logo_out.write(l_chunk)
            parsed_params["logo_path"] = logo_file_path
        except Exception as exc:
            if os.path.exists(logo_file_path):
                os.remove(logo_file_path)
            if os.path.exists(input_file_path):
                os.remove(input_file_path)
            raise HTTPException(status_code=500, detail=f"Failed to save logo: {exc}")

    started_at = datetime.now(timezone.utc).isoformat()
    log_file_path = os.path.join(logs_dir, f"{task_id}.log")

    task_state = TaskState(
        task_id=task_id,
        status="running",
        progress=0,
        started_at=started_at,
        input_file=input_file_path,
        logo_file=logo_file_path,
        log_file=log_file_path,
    )

    with tasks_lock:
        tasks[task_id] = task_state

    threading.Thread(
        target=_execute_processing_sync,
        args=(task_state, operation_type, parsed_params, duration_sec),
        name=f"WorkerTask-{task_id}",
        daemon=True,
    ).start()

    return JSONResponse(
        status_code=202,
        content={
            "task_id": task_id,
            "status": "running",
            "progress": 0,
            "started_at": started_at,
        },
    )


@app.get("/tasks/{task_id}/progress", dependencies=[Depends(verify_worker_token)], summary="Get task progress")
def get_task_progress(task_id: str):
    with tasks_lock:
        task = tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        return {
            "task_id": task.task_id,
            "status": task.status,
            "progress": task.progress,
            "error": task.error,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
        }


@app.post("/tasks/{task_id}/cancel", dependencies=[Depends(verify_worker_token)], summary="Cancel active task")
def cancel_task(task_id: str):
    with tasks_lock:
        task = tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        task.cancel_event.set()
        return {
            "task_id": task.task_id,
            "status": task.status if task.status in ["done", "failed", "cancelled"] else "cancelling",
        }


@app.get("/tasks/{task_id}/result", dependencies=[Depends(verify_worker_token)], summary="Download finished result")
def get_task_result(task_id: str):
    with tasks_lock:
        task = tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        if task.status != "done":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Task is in status '{task.status}', result not ready",
            )

        if not task.output_file or not os.path.exists(task.output_file):
            raise HTTPException(status_code=404, detail="Result file not found on disk")

        return FileResponse(
            task.output_file,
            media_type="application/octet-stream",
            filename=os.path.basename(task.output_file),
        )


@app.delete("/tasks/{task_id}", dependencies=[Depends(verify_worker_token)], summary="Clean up task files and state")
def delete_task_state(task_id: str):
    with tasks_lock:
        task = tasks.pop(task_id, None)

    if task:
        task.cancel_event.set()
        for f in [task.input_file, task.output_file, task.logo_file, task.log_file]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass

    return {"status": "ok", "task_id": task_id}
