import os
import sys
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Ensure backend directory is in sys.path
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import User
from app.services.security import hash_password

TEST_DB_NAME = "cvp_test"
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

if not TEST_DATABASE_URL:
    pg_host = os.getenv("DB_HOST", "localhost")
    pg_port = os.getenv("DB_PORT", "5432")
    pg_user = os.getenv("DB_USER", "cvp")
    pg_password = os.getenv("DB_PASSWORD", "my_secret_password")
    pg_url = f"postgresql+psycopg://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{TEST_DB_NAME}"

    # Try connecting to PostgreSQL and creating cvp_test database if needed
    try:
        import psycopg
        with psycopg.connect(
            host=pg_host,
            port=int(pg_port),
            user=pg_user,
            password=pg_password,
            dbname="postgres",
            connect_timeout=2,
        ) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(f"SELECT 1 FROM pg_database WHERE datname='{TEST_DB_NAME}'")
                if not cur.fetchone():
                    cur.execute(f"CREATE DATABASE {TEST_DB_NAME}")
        TEST_DATABASE_URL = pg_url
    except Exception:
        # Fallback to isolated test database file
        TEST_DATABASE_URL = "sqlite:///./cvp_test.db"

connect_args = {"check_same_thread": False} if "sqlite" in TEST_DATABASE_URL else {}
test_engine = create_engine(TEST_DATABASE_URL, connect_args=connect_args)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    """Create all schema tables in test database."""
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)
    if "sqlite" in TEST_DATABASE_URL and os.path.exists("./cvp_test.db"):
        try:
            os.remove("./cvp_test.db")
        except OSError:
            pass


@pytest.fixture
def db_session():
    """Provide isolated transactional session for each test."""
    connection = test_engine.connect()
    transaction = connection.begin()
    session = TestingSessionLocal(bind=connection)

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def client(db_session):
    """Provide FastAPI test client with database dependency override and base_url=https."""
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db

    # Pre-seed admin user into test database session
    admin = db_session.query(User).filter(User.email == settings.admin_email).first()
    if not admin:
        admin = User(
            email=settings.admin_email,
            hashed_password=hash_password(settings.admin_password),
            is_admin=True,
        )
        db_session.add(admin)
        db_session.commit()

    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client

    app.dependency_overrides.clear()
