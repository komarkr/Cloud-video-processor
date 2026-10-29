from typing import Any, Dict, List
from fastapi import APIRouter, Depends
import redis
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.routers.deps import get_current_user
from app.models import User
from autoscaler.scaler import Autoscaler

router = APIRouter(prefix="/api/autoscaler", tags=["autoscaler"])


@router.get(
    "/status",
    summary="Get autoscaler metrics, worker heartbeats, and scaling event logs",
)
def get_autoscaler_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    scaler = Autoscaler(
        min_workers=2,
        max_workers=4,
        per_worker=2,
        idle_for=60,
    )
    queued = scaler.count_queued(db)
    heartbeats = scaler.get_worker_heartbeats()
    events = scaler.get_events(limit=20)

    return {
        "status": "ok",
        "queued_tasks": queued,
        "active_workers_count": max(len(heartbeats), 1),
        "workers": heartbeats,
        "min_workers": 2,
        "max_workers": 4,
        "per_worker": 2,
        "events": events,
    }
