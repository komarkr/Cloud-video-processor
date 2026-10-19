from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    server_id: str = "api_default"

    db_user: str
    db_password: str
    db_name: str
    db_host: str
    db_port: int

    secret_key: str

    max_file_mb: int = 100
    max_duration_sec: int = 120
    max_active_tasks: int = 5
    max_queue_length: int = 100
    result_lifetime_hours: int = 24

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def database_url(self) -> str:
        return f"postgresql+asyncpg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"


settings = Settings()