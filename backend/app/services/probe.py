import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set

from app.config import settings

logger = logging.getLogger(__name__)

ALLOWED_FORMATS: Set[str] = {"mov", "mp4", "matroska", "webm", "avi"}


class VideoValidationError(Exception):
    """Exception raised when video probing or validation fails."""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class VideoProbeResult:
    duration: float
    width: int
    height: int
    format_name: str
    codec_name: str
    size: int
    raw_info: Dict[str, Any]


def get_ffprobe_executable() -> str:
    """Find ffprobe path, checking system PATH, environment, and common installation dirs."""
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


def probe_video(file_path: str) -> VideoProbeResult:
    """
    Probe a video file using ffprobe and validate:
    1. Media file is readable and not corrupt.
    2. Presence of a valid video stream.
    3. Duration <= MAX_DURATION_SEC (120 s).
    4. Resolution <= 1920x1080.
    5. Container format is in allowed list (mov, mp4, matroska, webm, avi).
    """
    if not os.path.exists(file_path):
        raise VideoValidationError("File does not exist on disk.", status_code=422)

    ffprobe_bin = get_ffprobe_executable()
    cmd = [
        ffprobe_bin,
        "-protocol_whitelist",
        "file",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        file_path,
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        logger.error("ffprobe executable '%s' not found.", ffprobe_bin)
        raise VideoValidationError(
            "Media processing engine is unavailable (ffprobe missing).",
            status_code=500,
        )
    except subprocess.TimeoutExpired:
        raise VideoValidationError(
            "Video probe timed out. The file may be corrupt.",
            status_code=422,
        )

    if proc.returncode != 0 or not proc.stdout.strip():
        raise VideoValidationError(
            "Invalid file format: file is corrupt or not a recognized media file.",
            status_code=422,
        )

    try:
        metadata = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise VideoValidationError(
            "Failed to parse media metadata from probe output.",
            status_code=422,
        )

    format_info = metadata.get("format", {})
    streams = metadata.get("streams", [])

    if not format_info and not streams:
        raise VideoValidationError(
            "No media format or stream information could be read.",
            status_code=422,
        )

    format_name_raw = format_info.get("format_name", "").lower()
    detected_formats = {fmt.strip() for fmt in format_name_raw.split(",") if fmt.strip()}
    if not (detected_formats & ALLOWED_FORMATS):
        allowed_str = ", ".join(sorted(ALLOWED_FORMATS))
        raise VideoValidationError(
            f"Unsupported container format '{format_name_raw}'. Allowed formats: {allowed_str}.",
            status_code=422,
        )

    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        raise VideoValidationError(
            "No video stream found in file (audio-only or empty files are not supported).",
            status_code=422,
        )

    vstream = video_streams[0]

    try:
        width = int(vstream.get("width") or 0)
        height = int(vstream.get("height") or 0)
    except (ValueError, TypeError):
        width, height = 0, 0

    if width <= 0 or height <= 0:
        raise VideoValidationError(
            "Invalid or indeterminate video resolution.",
            status_code=422,
        )

    if width > 1920 or height > 1080:
        raise VideoValidationError(
            f"Video resolution {width}x{height} exceeds maximum allowed 1920x1080.",
            status_code=422,
        )

    duration_val = format_info.get("duration") or vstream.get("duration")
    if not duration_val:
        raise VideoValidationError(
            "Could not determine video duration.",
            status_code=422,
        )

    try:
        duration = float(duration_val)
    except (ValueError, TypeError):
        raise VideoValidationError(
            "Invalid duration format in media file.",
            status_code=422,
        )

    if duration <= 0:
        raise VideoValidationError(
            "Video duration must be greater than 0 seconds.",
            status_code=422,
        )

    if duration > settings.max_duration_sec:
        raise VideoValidationError(
            f"Video duration ({duration:.1f}s) exceeds maximum allowed {settings.max_duration_sec}s.",
            status_code=422,
        )

    size = int(format_info.get("size") or os.path.getsize(file_path))
    codec_name = vstream.get("codec_name", "unknown")

    return VideoProbeResult(
        duration=duration,
        width=width,
        height=height,
        format_name=format_name_raw,
        codec_name=codec_name,
        size=size,
        raw_info=metadata,
    )
