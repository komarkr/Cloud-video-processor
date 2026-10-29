import asyncio
from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI, Request
import redis

from app.config import settings
from app.db import SessionLocal
from app.models import User
from app.routers.auth import router as auth_router
from app.routers.tasks import router as tasks_router
from app.routers.autoscaler import router as autoscaler_router
from app.routers.admin import router as admin_router
from app.services.cleanup import cleanup_expired_tasks
from app.services.security import hash_password

logger = logging.getLogger(__name__)


def init_admin():
    """Create admin user at startup using ADMIN_EMAIL and ADMIN_PASSWORD."""
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.email == settings.admin_email).first()
        if not admin:
            admin = User(
                email=settings.admin_email,
                hashed_password=hash_password(settings.admin_password),
                is_admin=True,
            )
            db.add(admin)
            db.commit()
            logger.info("Admin user '%s' created successfully.", settings.admin_email)
        else:
            logger.info("Admin user '%s' already exists.", settings.admin_email)
    except Exception as e:
        db.rollback()
        logger.warning("Could not initialize admin user on startup: %s", e)
    finally:
        db.close()


def run_cleanup_job():
    """Execute cleanup of expired tasks in a background thread."""
    db = SessionLocal()
    r = None
    try:
        r = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception:
        pass
    try:
        purged = cleanup_expired_tasks(db, r)
        if purged > 0:
            logger.info("[cleanup] Purged %d expired tasks on schedule.", purged)
    except Exception as e:
        logger.warning("Error during periodic expired task cleanup: %s", e)
    finally:
        db.close()


async def periodic_cleanup_loop():
    """Background task running every 10 minutes (600s) to delete expired tasks and files."""
    while True:
        try:
            await asyncio.sleep(600)
            await asyncio.to_thread(run_cleanup_job)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Unexpected error in periodic cleanup loop: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_admin()
    cleanup_task = asyncio.create_task(periodic_cleanup_loop())
    yield
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="Cloud Video Processor",
    description="Distributed Cloud Video Processing Service API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def add_x_served_by_header(request: Request, call_next):
    """Add X-Served-By header indicating which backend instance served the request."""
    response = await call_next(request)
    response.headers["X-Served-By"] = settings.server_id
    return response


app.include_router(auth_router)
app.include_router(tasks_router)
app.include_router(autoscaler_router)
app.include_router(admin_router)


@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "server_id": settings.server_id,
    }