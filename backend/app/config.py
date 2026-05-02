from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    app_env: str = "development"
    app_debug: bool = False
    app_secret_key: str = "dev-secret-key"
    app_log_level: str = "INFO"

    # Postgres
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "aiworkspace"
    postgres_password: str = "changeme"
    postgres_db: str = "aiworkspace"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url_sync(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # MinIO
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "changeme"
    minio_bucket: str = "documents"
    minio_secure: bool = False

    # Qdrant
    qdrant_url: str = "http://localhost:6333"

    # Ollama
    ollama_url: str = "http://localhost:11434"
    ollama_model_ocr: str = "gemma4:e4b"
    ollama_model_reasoning: str = "gemma4:26b"

    # AI reasoning backend
    ai_reasoning_backend: str = "ollama"  # ollama | anthropic | openrouter
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    deepseek_api_key: str = ""

    # CORS — wildcard for development; override via CORS_ORIGINS env var in production
    cors_origins: str = "*"

    # Auth (Authentik SSO)
    auth_enabled: bool = False          # set True in production
    authentik_url: str = "http://authentik:9000"
    authentik_slug: str = "ai-workspace"
    oauth_client_id: str = ""
    oauth_client_secret: str = ""

    # SMTP
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@company.com"

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = Settings()
