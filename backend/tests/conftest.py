import os
import sys
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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
        TEST_DATABASE_URL = "sqlite:///./cvp_test.db"

connect_args = {"check_same_thread": False} if "sqlite" in TEST_DATABASE_URL else {}
test_engine = create_engine(TEST_DATABASE_URL, connect_args=connect_args)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


class FakeRedis:
    """In-memory Redis implementation for unit testing."""
    def __init__(self, *args, **kwargs):
        self._data = {}

    def set(self, key, value, ex=None):
        self._data[str(key)] = str(value)
        return True

    def get(self, key):
        return self._data.get(str(key))

    def exists(self, key):
        return 1 if str(key) in self._data else 0

    def hset(self, name, key=None, value=None, mapping=None):
        name_str = str(name)
        if name_str not in self._data or not isinstance(self._data[name_str], dict):
            self._data[name_str] = {}
        if mapping:
            for k, v in mapping.items():
                self._data[name_str][str(k)] = str(v)
        if key is not None and value is not None:
            self._data[name_str][str(key)] = str(value)
        return True

    def hget(self, name, key):
        return self._data.get(str(name), {}).get(str(key))

    def hgetall(self, name):
        return self._data.get(str(name), {})

    def keys(self, pattern="*"):
        import fnmatch
        return [k for k in self._data.keys() if fnmatch.fnmatch(k, pattern)]

    def delete(self, *keys):
        count = 0
        for k in keys:
            if str(k) in self._data:
                del self._data[str(k)]
                count += 1
        return count

    def expire(self, key, seconds):
        return True

    def rpush(self, name, *values):
        name_str = str(name)
        if name_str not in self._data or not isinstance(self._data[name_str], list):
            self._data[name_str] = []
        for v in values:
            self._data[name_str].append(str(v))
        return len(self._data[name_str])

    def lrange(self, name, start, end):
        name_str = str(name)
        lst = self._data.get(name_str, [])
        if not isinstance(lst, list):
            return []
        if end == -1:
            return lst[start:]
        return lst[start : end + 1]

    def ltrim(self, name, start, end):
        name_str = str(name)
        lst = self._data.get(name_str, [])
        if isinstance(lst, list):
            if end == -1:
                self._data[name_str] = lst[start:]
            else:
                self._data[name_str] = lst[start : end + 1]
        return True

    def flushall(self):
        self._data.clear()


class FakeAsyncRedis:
    """Async wrapper around FakeRedis for testing SSE and async endpoints."""
    def __init__(self, sync_redis: FakeRedis):
        self._sync = sync_redis

    async def hgetall(self, name):
        return self._sync.hgetall(name)

    async def hset(self, name, key=None, value=None, mapping=None):
        return self._sync.hset(name, key=key, value=value, mapping=mapping)

    async def set(self, key, value, ex=None):
        return self._sync.set(key, value, ex=ex)

    async def get(self, key):
        return self._sync.get(key)

    async def exists(self, key):
        return self._sync.exists(key)

    async def delete(self, *keys):
        return self._sync.delete(*keys)

    async def aclose(self):
        pass


_global_fake_redis = FakeRedis()


@pytest.fixture(autouse=True)
def setup_mock_redis(monkeypatch):
    """Automatically provide FakeRedis if real Redis is unreachable."""
    try:
        import redis
        real_r = redis.Redis.from_url(settings.redis_url, socket_timeout=0.5)
        real_r.ping()
    except Exception:
        import redis
        import redis.asyncio as aioredis
        monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: _global_fake_redis)
        monkeypatch.setattr(aioredis, "from_url", lambda *args, **kwargs: FakeAsyncRedis(_global_fake_redis))

    import worker.tasks
    monkeypatch.setattr(worker.tasks, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(worker.tasks.process_task, "delay", lambda *args, **kwargs: None)


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
