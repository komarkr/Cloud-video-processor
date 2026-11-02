import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("worker_server.ffmpeg_runner")

WATERMARK_POSITIONS = {
    "top_left": "10:10",
    "top_right": "main_w-overlay_w-10:10",
    "bottom_left": "10:main_h-overlay_h-10",
    "bottom_right": "main_w-overlay_w-10:main_h-overlay_h-10",
    "center": "(main_w-overlay_w)/2:(main_h-overlay_h)/2",
}


class Cancelled(Exception):
    """Raised when task execution is cancelled."""
    pass


def get_ffmpeg_executable() -> str:
    custom_path = os.environ.get("FFMPEG_PATH")
    if custom_path and os.path.exists(custom_path):
        return custom_path

    which_path = shutil.which("ffmpeg")
    if which_path:
        return which_path

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        winget_dir = os.path.join(local_app_data, "Microsoft", "WinGet", "Packages")
        if os.path.exists(winget_dir):
            for root, _, files in os.walk(winget_dir):
                if "ffmpeg.exe" in files:
                    return os.path.join(root, "ffmpeg.exe")

    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass

    return "ffmpeg"


def get_ffprobe_executable() -> str:
    custom_path = os.environ.get("FFPROBE_PATH")
    if custom_path and os.path.exists(custom_path):
        return custom_path

    which_path = shutil.which("ffprobe")
    if which_path:
        return which_path

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        winget_dir = os.path.join(local_app_data, "Microsoft", "WinGet", "Packages")
        if os.path.exists(winget_dir):
            for root, _, files in os.walk(winget_dir):
                if "ffprobe.exe" in files:
                    return os.path.join(root, "ffprobe.exe")

    return "ffprobe"


def get_output_extension(task: Any) -> str:
    if isinstance(task, dict):
        op = (task.get("operation_type") or "").lower()
        params = task.get("params") or {}
    elif isinstance(task, str):
        op = task.lower()
        params = {}
    else:
        op = (getattr(task, "operation_type", "") or "").lower()
        params = getattr(task, "params", {}) or {}

    if op in ["extract_audio", "extract"]:
        return ".mp3"
    if op == "audio" and params.get("action") not in ["mute", "remove"]:
        return ".mp3"
    return ".mp4"


def parse_ffmpeg_progress_line(line: Optional[str], total_sec: float) -> Optional[int]:
    if not line or not line.startswith("out_time_us="):
        return None
    try:
        parts = line.strip().split("=", 1)
        if len(parts) == 2 and total_sec > 0:
            val_str = parts[1].strip()
            if val_str.isdigit():
                pct = int(int(val_str) / 1e6 / total_sec * 100)
                return max(0, min(99, pct))
    except Exception:
        pass
    return None


def check_audio_stream(input_file: str) -> bool:
    cmd = [
        get_ffprobe_executable(),
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        input_file,
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        data = json.loads(res.stdout)
        streams = data.get("streams", [])
        return any(s.get("codec_type") == "audio" for s in streams)
    except Exception as e:
        logger.warning("Could not probe audio streams: %s", e)
        return False


def build_ffmpeg_args(task: Any, output_path: str) -> list[str]:
    if isinstance(task, dict):
        op = (task.get("operation_type") or "convert").lower()
        input_file = task.get("input_file")
        params = task.get("params") or {}
    else:
        op = (getattr(task, "operation_type", "convert") or "convert").lower()
        input_file = getattr(task, "input_file", None)
        params = getattr(task, "params", {}) or {}

    preset = params.get("preset", "medium")

    if op == "convert":
        return [
            "-protocol_whitelist",
            "file",
            "-i",
            str(input_file or ""),
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            "23",
            "-c:a",
            "aac",
            output_path,
        ]

    elif op == "scale":
        SCALE_CRF_TABLE = {1080: 22, 720: 23, 480: 25, 360: 28}
        raw_res = params.get("resolution") or params.get("height") or 720
        cleaned = re.sub(r"[^\d]", "", str(raw_res))
        height = int(cleaned) if cleaned else 720
        if height not in SCALE_CRF_TABLE:
            height = 720
        crf = SCALE_CRF_TABLE.get(height, 23)

        return [
            "-protocol_whitelist",
            "file",
            "-i",
            str(input_file or ""),
            "-vf",
            f"scale=-2:{height}",
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-c:a",
            "aac",
            output_path,
        ]

    elif op == "watermark":
        logo_path = params.get("logo_path")
        pos_key = params.get("position", "bottom_right")
        pos_expr = WATERMARK_POSITIONS.get(pos_key, WATERMARK_POSITIONS["bottom_right"])
        try:
            opacity = float(params.get("opacity", 1.0))
        except (ValueError, TypeError):
            opacity = 1.0

        if 0.0 < opacity < 1.0:
            filter_complex = f"[1:v]format=rgba,colorchannelmixer=aa={opacity}[wm];[0:v][wm]overlay={pos_expr}"
        else:
            filter_complex = f"[0:v][1:v]overlay={pos_expr}"

        return [
            "-protocol_whitelist",
            "file",
            "-i",
            str(input_file or ""),
            "-protocol_whitelist",
            "file",
            "-i",
            str(logo_path or ""),
            "-filter_complex",
            filter_complex,
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            "23",
            "-c:a",
            "aac",
            output_path,
        ]

    elif op in ["audio", "extract_audio", "mute", "mute_audio", "remove_audio"]:
        action = params.get("action", "").lower()
        if action in ["mute", "remove"] or op in ["mute", "mute_audio", "remove_audio"]:
            return [
                "-protocol_whitelist",
                "file",
                "-i",
                str(input_file or ""),
                "-an",
                "-c:v",
                "copy",
                output_path,
            ]
        else:
            return [
                "-protocol_whitelist",
                "file",
                "-i",
                str(input_file or ""),
                "-vn",
                "-c:a",
                "libmp3lame",
                "-q:a",
                "2",
                output_path,
            ]

    else:
        return [
            "-protocol_whitelist",
            "file",
            "-i",
            str(input_file or ""),
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            "23",
            "-c:a",
            "aac",
            output_path,
        ]


def run_ffmpeg(
    cmd_args: list[str],
    total_sec: float,
    log_file_path: str,
    cancel_event: Optional[threading.Event] = None,
    progress_callback: Optional[Callable[[int], None]] = None,
) -> None:
    if cancel_event and cancel_event.is_set():
        raise Cancelled()

    os.makedirs(os.path.dirname(os.path.abspath(log_file_path)), exist_ok=True)
    cmd = [
        get_ffmpeg_executable(),
        "-y",
        "-progress",
        "pipe:1",
        "-nostats",
        "-loglevel",
        "error",
        *cmd_args,
    ]

    with open(log_file_path, "w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True)
        last_check = 0.0

        if proc.stdout:
            for line in proc.stdout:
                pct = parse_ffmpeg_progress_line(line, total_sec)
                if pct is not None and progress_callback:
                    try:
                        progress_callback(pct)
                    except Exception:
                        pass

                now = time.time()
                if now - last_check > 0.5:
                    last_check = now
                    if cancel_event and cancel_event.is_set():
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        raise Cancelled()

        ret = proc.wait()
        if cancel_event and cancel_event.is_set():
            raise Cancelled()
        if ret != 0:
            error_content = ""
            if os.path.exists(log_file_path):
                with open(log_file_path, "r", encoding="utf-8", errors="replace") as f:
                    error_content = f.read()[-500:]
            raise RuntimeError(error_content or f"FFmpeg process failed with exit code {ret}.")

