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
    ollama_model_vlm: str = "gemma4:e4b"  # Vision LM for drawing analysis

    # AI reasoning backend
    ai_reasoning_backend: str = "ollama"  # ollama | anthropic | openrouter
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    deepseek_api_key: str = ""

    # CORS — restrict to frontend domain in production via CORS_ORIGINS env var
    cors_origins: str = "http://localhost:3000"
    frontend_url: str = "http://localhost:3000"

    # Security
    csrf_secret: str = "dev-csrf-secret"   # set to secrets.token_hex(32) in production
    rate_limit_login_per_minute: int = 5
    rate_limit_api_per_minute: int = 200
    csp_enabled: bool = False              # enable in production

    # Admin bootstrap — email of the first admin user (auto-promoted on first login if no admin exists)
    initial_admin_email: str = ""
    # One-time setup token for bootstrapping admin via API (invalidated after first use)
    setup_token: str = ""
    # Minimum number of active admins (prevents demoting the last admin)
    min_admin_count: int = 1

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

    # Telegram
    telegram_bot_token: str = ""
    telegram_allowed_users: str = ""  # comma-separated int user IDs; empty = no whitelist
    telegram_notifications_chat_id: str = ""  # default chat for push notifications
    telegram_notifications_enabled: bool = False

    # Celery beat schedules (intervals in seconds or minutes)
    imap_poll_interval_minutes: int = 5
    approval_escalation_interval_seconds: int = 900  # 15 minutes

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = Settings()
