from celery import Celery
from app.config import settings

celery_app = Celery(
    "worker",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["worker.tasks", "worker.heartbeat"],
)

# Critical configurations for fair load balancing and fault tolerance
celery_app.conf.update(
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_soft_time_limit=600,
    task_time_limit=660,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

app = celery_app  # Alias for `celery -A worker.app worker`
