Cloud Video Processor

A distributed cloud video processing service demonstrating scalable architecture. Built with FastAPI, Nginx for load balancing, Celery, Redis, and PostgreSQL. Uses FFmpeg for async media operations like conversion and scaling, plus SSE for real-time task progress tracking.

Features

Format Conversion: Convert videos from various allowed formats to MP4 (H.264 + AAC).

Resolution Scaling: Compress or scale videos down to 1080p, 720p, 480p, or 360p.

Watermarking: Add a custom PNG watermark (up to 2 MB) with adjustable position and transparency.

Audio Processing: Extract audio to MP3 or remove the audio track completely.

Face Blurring (OpenCV): Frame-by-frame face detection and blurring using a custom algorithm.

Real-time Progress: Live progress tracking via Server-Sent Events (SSE).

Task Management: Users can view task history, download processed files, or cancel running tasks.

System Limits

Maximum File Size: 100 MB

Maximum Duration: 120 seconds

Max Active Tasks: 5 per user (queued + running)

Max Task History: 20 per user (older tasks are automatically deleted)

Global Queue Limit: 100 tasks (rejects new tasks if overloaded)

Result Lifetime: 24 hours

Technology Stack

Backend API: Python 3.12, FastAPI, Uvicorn, Pydantic

Video Processing: FFmpeg, ffprobe, OpenCV

Task Queue & Workers: Celery, Redis

Database: PostgreSQL 16, SQLAlchemy 2, Alembic

Infrastructure & Load Balancing: Nginx (least_conn), Docker, Docker Compose

Architecture Overview

Load Balancing: Nginx distributes incoming HTTP requests across multiple FastAPI backend instances.

Task Creation: The API validates the uploaded file using ffprobe. If valid, it saves the file, creates a record in PostgreSQL, and pushes the task to the Redis queue.

Asynchronous Processing: Celery Workers pick up tasks from Redis, execute the corresponding FFmpeg or OpenCV operations, and write progress updates back to Redis.

Live Updates: The frontend receives real-time progress updates through SSE.

Autoscaling: A background controller monitors the Redis queue length and dynamically starts or stops worker containers based on the load.

Local Setup

Clone the repository:

git clone https://github.com/komarkr/cloud-video-processor.git
cd cloud-video-processor


Create the environment file based on the example:

cp .env.example .env


Start the infrastructure using Docker Compose:

docker compose up --build -d
