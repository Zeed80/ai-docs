"""Mailbox OAuth2 consent flow — Gmail / Microsoft 365.

Runs as a browser popup: /start returns a Google/Microsoft consent URL, the
popup navigates through it, the provider redirects back to /callback/{provider}
on this backend, which finishes the token exchange and hands a tiny result
back to the opener window via postMessage before closing itself. See
app/domain/oauth_mail.py for the token-exchange mechanics.
"""
from __future__ import annotations

import json
import secrets
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.acting import get_effective_user
from app.auth.models import UserInfo
from app.db.models import MailboxConfig, OAuthAppConfig
from app.db.session import get_db
from app.domain import oauth_mail
from app.utils.crypto import encrypt_password

router = APIRouter()
logger = structlog.get_logger()

_START_KEY = "oauth_mail_start:{state}"
_RESULT_KEY = "oauth_mail_result:{state}"
_TTL = 600  # 10 minutes — long enough for a consent screen, short enough to not linger


class OAuthStartRequest(BaseModel):
    provider: str
    mailbox_id: uuid.UUID | None = None  # set only when (re)connecting an existing mailbox


class OAuthStartResponse(BaseModel):
    authorize_url: str
    state: str


class OAuthPendingOut(BaseModel):
    provider: str
    email: str | None


async def _app_config(db: AsyncSession, provider: str) -> OAuthAppConfig:
    if provider not in oauth_mail.PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Неизвестный OAuth-провайдер: {provider}")
    cfg = (
        await db.execute(select(OAuthAppConfig).where(OAuthAppConfig.provider == provider))
    ).scalar_one_or_none()
    if not cfg or not cfg.client_id or not cfg.client_secret_encrypted or not cfg.redirect_uri:
        raise HTTPException(
            status_code=409,
            detail=(
                f"OAuth-приложение «{provider}» не настроено администратором — "
                f"заполните Client ID/Secret и redirect URI в /admin/integrations"
            ),
        )
    return cfg


@router.post("/start", response_model=OAuthStartResponse)
async def start_oauth(
    payload: OAuthStartRequest,
    _user: UserInfo = Depends(get_effective_user),
    db: AsyncSession = Depends(get_db),
) -> OAuthStartResponse:
    """Skill: mailbox.oauth_start — Begin a Gmail/Microsoft 365 consent flow."""
    from app.utils.redis_client import get_async_redis

    app_cfg = await _app_config(db, payload.provider)

    if payload.mailbox_id is not None:
        mb = await db.get(MailboxConfig, payload.mailbox_id)
        if not mb:
            raise HTTPException(status_code=404, detail="Mailbox not found")

    state = secrets.token_urlsafe(24)
    r = get_async_redis()
    await r.setex(
        _START_KEY.format(state=state),
        _TTL,
        json.dumps({"provider": payload.provider, "mailbox_id": str(payload.mailbox_id) if payload.mailbox_id else None}),
    )

    url = oauth_mail.build_authorize_url(
        payload.provider, app_cfg.client_id, app_cfg.redirect_uri, state
    )
    return OAuthStartResponse(authorize_url=url, state=state)


def _popup_page(payload: dict) -> HTMLResponse:
    """A self-closing page that hands `payload` to window.opener and exits.

    postMessage's target origin is "*": the payload never carries a secret
    (just a session id, provider name, and — for the connect-existing-mailbox
    case — a mailbox id the opener already knows), only a one-time pointer
    the opener uses to fetch/attach the real result server-side.
    """
    data = json.dumps(payload)
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Подключение почты</title></head>
<body style="font-family:system-ui,sans-serif;padding:2rem;color:#334">
<p>Готово, можно закрыть эту вкладку.</p>
<script>
  try {{ window.opener && window.opener.postMessage({data}, "*"); }} catch (e) {{}}
  window.close();
</script>
</body></html>"""
    return HTMLResponse(content=html)


@router.get("/callback/{provider}")
async def oauth_callback(
    provider: str,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Provider redirects the browser here after the user grants/denies consent."""
    from app.utils.redis_client import get_async_redis

    if error:
        logger.info("oauth_mail_denied", provider=provider, error=error)
        return _popup_page({"type": "oauth_error", "detail": f"Доступ не предоставлен ({error})"})
    if not code or not state:
        return _popup_page({"type": "oauth_error", "detail": "Отсутствует code/state в ответе провайдера"})

    r = get_async_redis()
    key = _START_KEY.format(state=state)
    raw = await r.get(key)
    await r.delete(key)
    if not raw:
        return _popup_page({"type": "oauth_error", "detail": "Сессия подключения истекла, попробуйте снова"})

    started = json.loads(raw)
    if started["provider"] != provider:
        return _popup_page({"type": "oauth_error", "detail": "Провайдер не совпадает с началом сессии"})

    try:
        app_cfg = await _app_config(db, provider)
        from app.ai.secret_box import decrypt as decrypt_client_secret

        result = await oauth_mail.exchange_code(
            provider, app_cfg.client_id,
            decrypt_client_secret(app_cfg.client_secret_encrypted or ""),
            app_cfg.redirect_uri, code,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to the popup, not raised to the browser as a 500
        logger.error("oauth_mail_exchange_failed", provider=provider, error=str(exc))
        return _popup_page({"type": "oauth_error", "detail": f"Не удалось обменять код на токен: {exc}"})

    mailbox_id = started.get("mailbox_id")
    if mailbox_id:
        mb = await db.get(MailboxConfig, uuid.UUID(mailbox_id))
        if not mb:
            return _popup_page({"type": "oauth_error", "detail": "Почтовый ящик уже удалён"})
        if not result.refresh_token and not mb.oauth_refresh_token_encrypted:
            return _popup_page({
                "type": "oauth_error",
                "detail": "Провайдер не выдал refresh-токен. Отзовите доступ приложению в настройках аккаунта и подключите заново.",
            })
        mb.auth_method = "oauth2"
        mb.oauth_provider = provider
        if result.refresh_token:
            mb.oauth_refresh_token_encrypted = encrypt_password(result.refresh_token)
        mb.oauth_access_token_encrypted = encrypt_password(result.access_token)
        mb.oauth_token_expires_at = result.expires_at
        mb.oauth_scope = result.scope
        mb.oauth_email = result.email
        if result.email and not mb.imap_user:
            mb.imap_user = result.email
        if result.email and not mb.smtp_user:
            mb.smtp_user = result.email
            mb.smtp_from_address = mb.smtp_from_address or result.email
        await db.commit()
        logger.info("oauth_mail_connected", provider=provider, mailbox=mb.name)
        return _popup_page({"type": "oauth_complete", "mailbox_id": mailbox_id})

    if not result.refresh_token:
        return _popup_page({
            "type": "oauth_error",
            "detail": "Провайдер не выдал refresh-токен, попробуйте подключить ещё раз",
        })

    await r.setex(
        _RESULT_KEY.format(state=state),
        _TTL,
        json.dumps({
            "provider": provider,
            "email": result.email,
            "refresh_token_encrypted": encrypt_password(result.refresh_token),
            "access_token_encrypted": encrypt_password(result.access_token),
            "expires_at": result.expires_at.isoformat(),
            "scope": result.scope,
        }),
    )
    logger.info("oauth_mail_pending", provider=provider, session=state[:8])
    return _popup_page({"type": "oauth_complete", "session": state})


@router.get("/pending/{session}", response_model=OAuthPendingOut)
async def get_pending_result(
    session: str,
    _user: UserInfo = Depends(get_effective_user),
) -> OAuthPendingOut:
    """Skill: mailbox.oauth_pending — Look up a just-finished consent result
    (by its one-time session id) before the mailbox row exists yet."""
    from app.utils.redis_client import get_async_redis

    r = get_async_redis()
    raw = await r.get(_RESULT_KEY.format(state=session))
    if not raw:
        raise HTTPException(status_code=404, detail="Сессия не найдена или истекла")
    data = json.loads(raw)
    return OAuthPendingOut(provider=data["provider"], email=data.get("email"))


async def consume_pending_result(db: AsyncSession, session: str) -> dict | None:
    """Pop and decrypt-ready a pending OAuth result for attaching to a new
    mailbox at creation time. Returns raw (still-encrypted) fields to copy
    directly onto the new MailboxConfig row — see app/api/mailbox.py."""
    from app.utils.redis_client import get_async_redis

    r = get_async_redis()
    key = _RESULT_KEY.format(state=session)
    raw = await r.get(key)
    if not raw:
        return None
    await r.delete(key)
    return json.loads(raw)
