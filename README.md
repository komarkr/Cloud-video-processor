# Cloud Video Processor

A distributed cloud video processing service demonstrating scalable architecture. Built with FastAPI, Nginx for load balancing, Celery, Redis, and PostgreSQL. Uses FFmpeg for async media operations like conversion and scaling, plus SSE for real-time task progress tracking.

---

## Features

* **Format Conversion:** Convert videos from various allowed formats to MP4 (H.264 + AAC).
* **Resolution Scaling:** Compress or scale videos down to `1080p`, `720p`, `480p`, or `360p`.
* **Watermarking:** Add a custom PNG watermark (up to 2 MB) with adjustable position and transparency.
* **Audio Processing:** Extract audio to MP3 or remove the audio track completely.
* **Face Blurring (OpenCV):** Frame-by-frame face detection and blurring using a custom algorithm.
* **Real-time Progress:** Live progress tracking via Server-Sent Events (SSE).
* **Task Management:** Users can view task history, download processed files, or cancel running tasks.

---

## System Limits

| Limit | Maximum Value |
| :--- | :--- |
| **File Size** | 100 MB |
| **Video Duration**| 120 seconds |
| **Active Tasks** | 5 per user *(queued + running)* |
| **Task History** | 20 per user *(older tasks are auto-deleted)* |
| **Global Queue** | 100 tasks *(rejects new tasks if overloaded)* |
| **Result Lifetime**| 24 hours |

---

## Technology Stack

* **Backend API:** Python 3.12, FastAPI, Uvicorn, Pydantic
* **Video Processing:** FFmpeg, ffprobe, OpenCV
* **Task Queue & Workers:** Celery, Redis
* **Database:** PostgreSQL 16, SQLAlchemy 2, Alembic
* **Infrastructure:** Nginx (least_conn), Docker, Docker Compose

---

## Architecture Overview

1. **Load Balancing:** Nginx distributes incoming HTTP requests across multiple FastAPI backend instances.
2. **Task Creation:** The API validates the uploaded file using `ffprobe`. If valid, it saves the file, creates a record in PostgreSQL, and pushes the task to the Redis queue.
3. **Asynchronous Processing:** Celery Workers pick up tasks from Redis, execute the corresponding FFmpeg or OpenCV operations, and write progress updates back to Redis.
4. **Live Updates:** The frontend receives real-time progress updates through SSE.
5. **Autoscaling:** A background controller monitors the Redis queue length and dynamically starts or stops worker containers based on the load.

---

## Local Setup

**1. Clone the repository:**
```bash
git clone https://github.com/komarkr/cloud-video-processor.git
cd cloud-video-processor
