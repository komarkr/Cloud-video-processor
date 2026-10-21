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

| Limit | Environment Variable | Default Value | Description |
| :--- | :--- | :--- | :--- |
| **File Size** | `MAX_FILE_MB` | `100` MB | Maximum allowed upload size |
| **Video Duration** | `MAX_DURATION_SEC` | `120` seconds | Maximum allowed duration |
| **Active Tasks** | `MAX_ACTIVE_TASKS` | `5` | Active tasks per user *(queued + running)* |
| **Task History** | `MAX_HISTORY` | `20` | Max tasks stored per user *(older are pruned)* |
| **Global Queue** | `MAX_QUEUE` | `100` | Global queue capacity before rejecting |
| **Result Lifetime** | `RESULT_LIFETIME_HOURS` | `24` hours | Lifetime of processed files on disk |

---

## Technology Stack

* **Backend API:** Python 3.12, FastAPI, Uvicorn, Pydantic (pydantic-settings)
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

## Quickstart & Local Setup

### 1. Environment Configuration
Copy the sample environment file and adjust variables if needed:
```bash
cp .env.example .env
```

Ensure `.env` contains:
```env
DB_USER=cvp
DB_PASSWORD=my_secret_password
DB_NAME=cvp
DB_HOST=postgres
DB_PORT=5432

JWT_SECRET=super_secure_jwt_secret_key_12345
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=admin_password_123

MAX_FILE_MB=100
MAX_DURATION_SEC=120
MAX_ACTIVE_TASKS=5
MAX_HISTORY=20
MAX_QUEUE=100
RESULT_LIFETIME_HOURS=24
```

### 2. Build and Start Services
Start the entire stack using Docker Compose:
```bash
docker compose up --build
```

This starts:
- **`postgres`** on port `5432` with automatic health checks.
- **`redis`** on port `6379`.
- **`migrate`** runs `alembic upgrade head` to apply database schema migrations.
- **`api1`** FastAPI backend application on port `8000`.

### 3. Verify Health & Documentation
- **Health Check:** [http://localhost:8000/api/health](http://localhost:8000/api/health)
  ```json
  {
    "status": "ok",
    "server_id": "api1"
  }
  ```
- **Interactive API Docs (Swagger):** [http://localhost:8000/docs](http://localhost:8000/docs)
- **ReDoc:** [http://localhost:8000/redoc](http://localhost:8000/redoc)

### 4. Database Inspection (DBeaver / psql)
Connect using any PostgreSQL client (e.g., DBeaver, DataGrip, or `psql`):
- **Host:** `localhost`
- **Port:** `5432`
- **Database:** `cvp`
- **Username:** `cvp`
- **Password:** value of `DB_PASSWORD` in `.env` (default: `my_secret_password`)

Tables created by Alembic:
- `users`: stores user accounts and password hashes.
- `tasks`: stores video processing tasks and status (`task_status_enum`).
- `alembic_version`: tracks current database migration state.
