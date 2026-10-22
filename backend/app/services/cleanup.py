from datetime import datetime, timezone
import logging
import os
from typing import Optional

import redis
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Task

logger = logging.getLogger(__name__)


def cleanup_expired_tasks(db: Session, r: Optional[redis.Redis] = None) -> int:
    """
    Find all tasks whose expires_at is in the past, remove their associated
    media and log files from disk and Redis, and delete the database records.
    Returns the count of purged tasks.
    """
    now = datetime.now(timezone.utc)
    expired_tasks = (
        db.query(Task)
        .filter(Task.expires_at.isnot(None), Task.expires_at < now)
        .all()
    )

    if not expired_tasks:
        return 0

    count = 0
    for task in expired_tasks:
        task_id = str(task.id)
        logger.info(
            "[cleanup] [task_id=%s] Purging expired task (expired at %s)",
            task_id,
            task.expires_at,
        )

        # 1. Delete input file
        if task.input_file and os.path.exists(task.input_file):
            try:
                os.remove(task.input_file)
            except OSError as e:
                logger.warning(
                    "[cleanup] [task_id=%s] Could not remove input file %s: %s",
                    task_id,
                    task.input_file,
                    e,
                )

        # 2. Delete output file
        if task.output_file and os.path.exists(task.output_file):
            try:
                os.remove(task.output_file)
            except OSError as e:
                logger.warning(
                    "[cleanup] [task_id=%s] Could not remove output file %s: %s",
                    task_id,
                    task.output_file,
                    e,
                )

        # 3. Delete logo file if watermark task
        if task.params and isinstance(task.params, dict):
            logo_path = task.params.get("logo_path")
            if logo_path and os.path.exists(logo_path):
                try:
                    os.remove(logo_path)
                except OSError as e:
                    logger.warning(
                        "[cleanup] [task_id=%s] Could not remove logo file %s: %s",
                        task_id,
                        logo_path,
                        e,
                    )

        # 4. Delete log file
        log_file = os.path.join(settings.log_dir, f"{task_id}.log")
        if os.path.exists(log_file):
            try:
                os.remove(log_file)
            except OSError as e:
                logger.warning(
                    "[cleanup] [task_id=%s] Could not remove log file %s: %s",
                    task_id,
                    log_file,
                    e,
                )

        # 5. Delete Redis cache keys
        if r:
            try:
                r.delete(f"task:{task_id}", f"cancel:{task_id}")
            except Exception as e:
                logger.warning(
                    "[cleanup] [task_id=%s] Could not delete Redis keys: %s",
                    task_id,
                    e,
                )

        # 6. Delete task from database
        db.delete(task)
        count += 1

    db.commit()
    logger.info("[cleanup] Successfully purged %d expired tasks and their files.", count)
    return count
