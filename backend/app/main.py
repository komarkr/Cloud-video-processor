from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI

from app.config import settings
from app.db import SessionLocal
from app.models import User
from app.routers.auth import router as auth_router
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize admin
    init_admin()
    yield


app = FastAPI(
    title="Cloud Video Processor",
    description="Distributed Cloud Video Processing Service API",
    version="1.0.0",
    lifespan=lifespan,
)

# Register routers
app.include_router(auth_router)


@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "server_id": settings.server_id,
    }