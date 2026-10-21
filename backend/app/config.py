from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    server_id: str = "api_default"

    # Database
    db_user: str = "cvp"
    db_password: str = "my_secret_password"
    db_name: str = "cvp"
    db_host: str = "postgres"
    db_port: int = 5432

    # Security & Admin
    jwt_secret: str = "super_secure_jwt_secret_key_12345"
    admin_email: str = "admin@example.com"
    admin_password: str = "admin_password_123"

    # System Limits
    max_file_mb: int = 100
    max_duration_sec: int = 120
    max_active_tasks: int = 5
    max_history: int = 20
    max_queue: int = 100
    result_lifetime_hours: int = 24

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @property
    def database_url(self) -> str:
        return f"postgresql+psycopg://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"


settings = Settings()