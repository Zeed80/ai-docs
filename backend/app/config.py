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

    # LoRA training (studio tab): shared volume between worker and trainer
    lora_data_dir: str = "/lora-data"
    # HuggingFace token forwarded to the trainer container (FLUX.2-dev is a
    # gated repo; klein/Qwen download without it).
    hf_token: str | None = None

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

    # Proactive: hour (0–23, server tz) for the secretary morning briefing
    morning_briefing_hour: int = 8
    morning_briefing_enabled: bool = True

    # Security
    csrf_secret: str = "dev-csrf-secret"   # set to secrets.token_hex(32) in production
    rate_limit_login_per_minute: int = 30  # 30/min = 1 request per 2s; plenty for dev, still safe
    rate_limit_api_per_minute: int = 600   # 10 req/s per IP — generous for rich SPA polling
    csp_enabled: bool = False              # enable in production
    # Set to true only when the backend sits behind a trusted reverse proxy (Traefik/nginx)
    # that sets X-Forwarded-For. False by default to prevent IP spoofing via that header.
    trusted_proxy: bool = False
    # Qdrant vector store API key — required when QDRANT__SERVICE__API_KEY is set on the container
    qdrant_api_key: str = ""

    # Admin bootstrap — email of the first admin user (auto-promoted on first login if no admin exists)
    initial_admin_email: str = ""
    # One-time setup token for bootstrapping admin via API (invalidated after first use)
    setup_token: str = ""
    # Minimum number of active admins (prevents demoting the last admin)
    min_admin_count: int = 1

    # Signing key for backend-minted session tokens (QR-login). Falls back to
    # app_secret_key when empty; set a dedicated value in production to isolate the
    # session-signing secret from app_secret_key (Fernet) and to allow rotation.
    session_signing_key: str = ""
    # Lifetime of a QR-login session (minutes). Long by design: scanning a login
    # QR signs the phone in durably ("stays logged in"). Revoke anytime via
    # /api/admin/users/{sub}/revoke-sessions (bumps the per-user session epoch).
    # Default ≈ 10 years.
    qr_login_session_ttl_minutes: int = 5_256_000

    # Internal service-to-service key used by the AI orchestrator and capability proxy.
    # Must be set to the same value in all services that need to call the backend API.
    agent_service_key: str = ""

    # Isolated executor for agent-generated skills (infra/skill-runner).
    # Generated code never runs inside the backend process.
    skill_runner_url: str = "http://skill-runner:8077"

    # Auth (Authentik SSO)
    auth_enabled: bool = False          # set True in production
    authentik_url: str = "http://authentik-server:9000"   # internal Docker URL for JWKS/token
    # External URL for browser login redirects (same as authentik_url when not split-brain)
    authentik_external_url: str = ""
    authentik_slug: str = "ai-workspace"
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    # Service-account token for Authentik REST API (user provisioning)
    authentik_api_token: str = ""

    # SMTP
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@company.com"

    # Push notifications (self-hosted ntfy — no Google services).
    # Internal URL the backend POSTs to; external URL is what the mobile app subscribes to.
    ntfy_enabled: bool = False
    ntfy_url: str = "http://ntfy:80"          # internal publish endpoint
    ntfy_external_url: str = ""               # e.g. https://push.example.com (for the mobile app)
    ntfy_token: str = ""                      # optional bearer token for ntfy auth

    # Mobile app APK distribution — directory served (without auth) at /download.
    releases_dir: str = "/releases"

    # Telegram
    telegram_bot_token: str = ""
    telegram_allowed_users: str = ""  # comma-separated int user IDs; empty = no whitelist
    telegram_notifications_chat_id: str = ""  # default chat for push notifications
    telegram_notifications_enabled: bool = False

    # llama.cpp server (optional embedded backend)
    # Defaults match docker-compose.yml values; env vars always win via Pydantic BaseSettings.
    llamacpp_url: str = "http://localhost:11436"   # override: LLAMACPP_URL=http://llama-server:8080
    llamacpp_model: str = "/models/model.gguf"
    llamacpp_ctx_size: int = 16384   # 16 384 / parallel(2) = 8 192 tokens per slot
    llamacpp_kv_cache_type: str = "q8_0"

    # ComfyUI (image generation / editing — drawings studio). On-prem only.
    # Default targets the optional compose service; override: COMFYUI_URL=http://host:8188
    comfyui_url: str = "http://comfyui:8188"
    # Neural CAD vectorizer (Ф3, infra/cad-vectorizer) — on-prem only, same
    # confidentiality posture as ComfyUI. Optional: cad_recognize/neural.py
    # degrades to CV-only when unreachable (NEURAL_UNAVAILABLE).
    # NOTE: superseded in arbitration by technical_vectorizer_url below (see
    # cad_recognize/technical_vectorizer.py) — the from-scratch model here
    # never beat CV on real photos (recall ~0, trained on synthetic data
    # only). Kept running/configurable for now, not removed from the code.
    cad_vectorizer_url: str = "http://cad-vectorizer:8090"
    # Technical-drawing line vectorizer (infra/technical-vectorizer) —
    # vendored, openly-licensed (MPL-2.0), pretrained Deep Vectorization of
    # Technical Drawings model. Validated live (2026-07-11): zero-shot
    # recall +3.8..+23.5 points over CV baseline on real test photos. This
    # is the primary neural recognizer in arbitrate_recognition now.
    technical_vectorizer_url: str = "http://technical-vectorizer:8091"
    llamacpp_n_gpu_layers: int = -1   # -1 = all layers on GPU
    llamacpp_parallel: int = 2        # 2 slots × 8 192 tokens; was 4 (too many, caused OOM)
    llamacpp_flash_attn: bool = True

    # OCR (local VLM fallback for scanned PDFs / images)
    ocr_max_pages: int = 15             # max PDF pages rendered for VLM OCR
    ocr_render_scale: float = 2.5       # PyMuPDF render matrix scale for OCR pages

    # Upload limits
    max_upload_size_mb: int = 100       # max single file size in MB
    max_batch_size: int = 50            # max files per batch upload

    # Celery beat schedules (intervals in seconds or minutes)
    imap_poll_interval_minutes: int = 5
    approval_escalation_interval_seconds: int = 900  # 15 minutes

    model_config = {"env_prefix": "", "case_sensitive": False}

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    def model_post_init(self, __context) -> None:
        """Fail-closed in production: refuse to start with dev defaults / weak secrets.

        Catches the most dangerous misconfigurations (auth disabled, default
        passwords, empty OAuth secret) at process start rather than silently
        running an insecure stack. No-op outside production.
        """
        if not self.is_production:
            return

        # (field value, dev-default/blank marker that must NOT survive into prod)
        _weak = {
            "APP_SECRET_KEY": (self.app_secret_key, {"", "dev-secret-key", "dev-secret-key-2026"}),
            "CSRF_SECRET": (self.csrf_secret, {"", "dev-csrf-secret"}),
            "POSTGRES_PASSWORD": (self.postgres_password, {"", "changeme"}),
            "MINIO_SECRET_KEY": (self.minio_secret_key, {"", "changeme"}),
            "AGENT_SERVICE_KEY": (self.agent_service_key, {"", "agent-internal-key-2026"}),
        }
        problems: list[str] = [
            f"{name} is unset or uses an insecure dev default"
            for name, (value, bad) in _weak.items()
            if value in bad
        ]

        if not self.auth_enabled:
            problems.append("AUTH_ENABLED must be true in production (no anonymous admin access)")
        else:
            if not self.oauth_client_secret:
                problems.append("OAUTH_CLIENT_SECRET must be set when AUTH_ENABLED=true")
            if not self.oauth_client_id:
                problems.append("OAUTH_CLIENT_ID must be set when AUTH_ENABLED=true")

        if problems:
            raise RuntimeError(
                "Insecure production configuration — refusing to start:\n  - "
                + "\n  - ".join(problems)
                + "\nGenerate secrets with infra/scripts/gen-secrets.sh and set APP_ENV/AUTH_ENABLED."
            )


settings = Settings()
