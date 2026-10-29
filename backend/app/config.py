import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    server_id: str = "api_default"

    db_user: str = "cvp"
    db_password: str = "my_secret_password"
    db_name: str = "cvp"
    db_host: str = "postgres"
    db_port: int = 5432

    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db: int = 0

    jwt_secret: str = "super_secure_jwt_secret_key_12345"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    cookie_secure: bool = True
    cookie_samesite: str = "lax"

    admin_email: str = "admin@example.com"
    admin_password: str = "admin_password_123"

    max_file_mb: int = 100
    max_duration_sec: int = 120
    max_active_tasks: int = 5
    max_history: int = 20
    max_queue: int = 100
    result_lifetime_hours: int = 24

    upload_dir: str = "/data/uploads" if os.name != "nt" else os.path.abspath("data/uploads")
    output_dir: str = "/data/outputs" if os.name != "nt" else os.path.abspath("data/outputs")
    log_dir: str = "/data/logs" if os.name != "nt" else os.path.abspath("data/logs")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @property
    def database_url(self) -> str:
        return f"postgresql+psycopg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def redis_url(self) -> str:
        return os.getenv("REDIS_URL", f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}")


settings = Settings()