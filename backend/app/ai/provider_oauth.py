"""Sign-in through the provider's site (OAuth) for cloud provider nodes.

Some cloud providers let an account sign in on their site instead of pasting
an API key — usually to use a subscription (ChatGPT, SuperGrok, Kimi Code,
MiniMax) rather than a metered key. Five of the cloud kinds in the registry do:

    openrouter  PKCE redirect to openrouter.ai; the result is an ordinary,
                user-scoped API key (stored like a pasted one).
    minimax     device code (PKCE + user code) at account.minimax.io; the token
                calls MiniMax's Anthropic-compatible API with a Bearer header.
    moonshot    Kimi Code device code at auth.kimi.com; the token calls
                api.kimi.com/coding/v1 (OpenAI-compatible).
    openai      ChatGPT sign-in (the Codex device flow) at auth.openai.com; the
                token calls the Codex Responses API at chatgpt.com/backend-api.
    xai         device code at auth.x.ai (SuperGrok / X Premium+); the token
                calls the Responses API at api.x.ai.

Deliberately not offered: Anthropic (its terms forbid using a Claude.ai
subscription sign-in outside Anthropic's own apps), Google Gemini (accounts
signed in through the Gemini CLI client from third-party tools are being
suspended), Qwen/DashScope (the free Qwen OAuth tier closed on 2026-04-15) and
every other kind, which only offers API keys.

The client ids and endpoints are those of the providers' own CLIs, the same
ones the open-source agents (OpenClaw, Hermes Agent, Kimi CLI) use; OpenAI asks
third-party clients to identify themselves (``originator`` / User-Agent).

Token state lives Fernet-encrypted in ``provider_instances.oauth_encrypted``.
``runtime_credentials`` hands the router a fresh access token: it refreshes a
token close to expiry under a Redis lock — OpenAI and xAI rotate the refresh
token on every use, so two processes refreshing at once would lock the
account out — and writes the new state to Postgres before using it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from app.ai.secret_box import decrypt, encrypt

logger = structlog.get_logger()

FLOW_KEY = "provider_oauth:flow:{}"
REFRESH_LOCK_KEY = "provider_oauth:refresh_lock:{}"
TOKEN_CACHE_KEY = "provider_oauth:token:{}"
REFRESH_SKEW_S = 120
HTTP_TIMEOUT = 30.0
USER_AGENT = "ai-workspace/1.0"


class OAuthError(RuntimeError):
    """A sign-in or refresh failed; the message is meant for the operator."""


@dataclass(frozen=True)
class OAuthSpec:
    kind: str
    label: str  # what the operator signs in to
    flow: str  # "pkce_key" | "minimax" | "device" | "codex" | "xai"
    api: str  # protocol the token is used with: "openai" | "anthropic" | "responses" | "key"
    base_url: str = ""
    note: str = ""
    regions: tuple[str, ...] = ()
    static_models: tuple[str, ...] = ()
    headers: dict[str, str] = field(default_factory=dict)


SPECS: dict[str, OAuthSpec] = {
    "openrouter": OAuthSpec(
        kind="openrouter",
        label="аккаунт OpenRouter",
        flow="pkce_key",
        api="key",
        note=(
            "OpenRouter выдаст ключ, привязанный к вашему аккаунту; расход идёт с баланса "
            "OpenRouter. Возврат после входа работает только по https-адресу сайта."
        ),
    ),
    "minimax": OAuthSpec(
        kind="minimax",
        label="аккаунт MiniMax (Token Plan)",
        flow="minimax",
        api="anthropic",
        base_url="https://api.minimax.io/anthropic",
        regions=("global", "cn"),
        static_models=("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed"),
        note="Расход идёт по подписке MiniMax, а не по ключу API.",
    ),
    "moonshot": OAuthSpec(
        kind="moonshot",
        label="аккаунт Kimi (Kimi Code)",
        flow="device",
        api="openai",
        base_url="https://api.kimi.com/coding/v1",
        static_models=("kimi-for-coding",),
        note="Работает в рамках подписки Kimi Code; модели — из её каталога.",
    ),
    "openai": OAuthSpec(
        kind="openai",
        label="аккаунт ChatGPT (Plus / Pro / Business)",
        flow="codex",
        api="responses",
        base_url="https://chatgpt.com/backend-api/codex",
        note=(
            "Работает в рамках подписки ChatGPT через Codex: свои модели и лимиты, "
            "не те, что у ключа API OpenAI."
        ),
    ),
    "xai": OAuthSpec(
        kind="xai",
        label="аккаунт X / Grok (SuperGrok или X Premium+)",
        flow="xai",
        api="responses",
        base_url="https://api.x.ai/v1",
        note="Работает в рамках подписки SuperGrok / X Premium+.",
    ),
}

# ── Provider constants (from each provider's own CLI) ────────────────────────

MINIMAX = {
    "global": {"oauth": "https://account.minimax.io", "api": "https://api.minimax.io/anthropic"},
    "cn": {"oauth": "https://account.minimaxi.com", "api": "https://api.minimaxi.com/anthropic"},
}
MINIMAX_CLIENT_ID = "78257093-7e40-4613-99e0-527b14b39113"
MINIMAX_SCOPE = "group_id profile model.completion"
MINIMAX_USER_CODE_GRANT = "urn:ietf:params:oauth:grant-type:user_code"

KIMI_OAUTH = "https://auth.kimi.com"
KIMI_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

CODEX_ISSUER = "https://auth.openai.com"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_URL = f"{CODEX_ISSUER}/oauth/token"
CODEX_ORIGINATOR = "ai-workspace"

XAI_ISSUER = "https://auth.x.ai"
XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_SCOPE = "openid profile email offline_access grok-cli:access api:access"

OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
OPENROUTER_KEYS_URL = "https://openrouter.ai/api/v1/auth/keys"


def spec_for(kind: str) -> OAuthSpec | None:
    return SPECS.get(kind)


# ── PKCE / state helpers ─────────────────────────────────────────────────────


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge


def _redis():
    from app.utils.redis_client import get_sync_redis

    return get_sync_redis()


def _save_flow(flow_id: str, state: dict, ttl_s: int) -> None:
    _redis().set(FLOW_KEY.format(flow_id), encrypt(json.dumps(state)), ex=max(60, int(ttl_s)))


def load_flow(flow_id: str) -> dict | None:
    raw = _redis().get(FLOW_KEY.format(flow_id))
    if not raw:
        return None
    text = decrypt(raw.decode() if isinstance(raw, bytes) else raw)
    return json.loads(text) if text else None


def drop_flow(flow_id: str) -> None:
    _redis().delete(FLOW_KEY.format(flow_id))


def jwt_claims(token: str) -> dict:
    """Unverified JWT payload (for account id / email display only)."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return payload if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _expires_at(payload: dict, *, field_name: str = "expires_in") -> float | None:
    value = payload.get(field_name)
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num > 1e12:  # absolute epoch milliseconds (MiniMax)
        return num / 1000
    if num > 1e9:  # absolute epoch seconds
        return num
    return time.time() + num


# ── Starting a sign-in ───────────────────────────────────────────────────────


async def start(kind: str, instance_id: str, *, origin: str, region: str | None = None) -> dict:
    """Begin a sign-in; returns what the page shows (a code and a link, or a redirect)."""
    spec = spec_for(kind)
    if spec is None:
        raise OAuthError(f"{kind}: вход через сайт не поддерживается")
    flow_id = uuid.uuid4().hex
    base_state: dict[str, Any] = {"kind": kind, "instance_id": instance_id, "origin": origin}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as http:
        if spec.flow == "pkce_key":
            verifier, challenge = _pkce()
            callback = (
                f"{origin.rstrip('/')}/api/providers/oauth/openrouter/callback?state={flow_id}"
            )
            url = httpx.URL(
                OPENROUTER_AUTH_URL,
                params={
                    "callback_url": callback,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
            )
            _save_flow(flow_id, {**base_state, "verifier": verifier}, 900)
            return {"flow_id": flow_id, "type": "redirect", "authorize_url": str(url)}

        if spec.flow == "minimax":
            region = region if region in MINIMAX else "global"
            verifier, challenge = _pkce()
            state = secrets.token_urlsafe(16)
            r = await http.post(
                f"{MINIMAX[region]['oauth']}/oauth2/device/code",
                data={
                    "response_type": "code",
                    "client_id": MINIMAX_CLIENT_ID,
                    "scope": MINIMAX_SCOPE,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": state,
                },
                headers={"Accept": "application/json", "x-request-id": str(uuid.uuid4())},
            )
            body = _json_or_error(r, "MiniMax")
            if body.get("state") != state:
                raise OAuthError("MiniMax вернул чужой state — вход прерван")
            if not body.get("user_code") or not body.get("verification_uri"):
                raise OAuthError("MiniMax не выдал код подтверждения")
            expires = _expires_at(body, field_name="expired_in") or time.time() + 600
            _save_flow(
                flow_id,
                {
                    **base_state,
                    "region": region,
                    "verifier": verifier,
                    "user_code": body["user_code"],
                },
                int(expires - time.time()) + 60,
            )
            return _device_answer(
                flow_id, body["verification_uri"], body["user_code"], expires, body.get("interval")
            )

        if spec.flow == "device":  # Kimi Code, RFC 8628
            r = await http.post(
                f"{KIMI_OAUTH}/api/oauth/device_authorization",
                data={"client_id": KIMI_CLIENT_ID},
                headers=kimi_headers(),
            )
            body = _json_or_error(r, "Kimi")
            expires = time.time() + float(body.get("expires_in") or 600)
            _save_flow(
                flow_id,
                {**base_state, "device_code": body["device_code"]},
                int(expires - time.time()),
            )
            return _device_answer(
                flow_id,
                body.get("verification_uri_complete") or body.get("verification_uri"),
                body.get("user_code"),
                expires,
                body.get("interval"),
            )

        if spec.flow == "codex":
            r = await http.post(
                f"{CODEX_ISSUER}/api/accounts/deviceauth/usercode",
                json={"client_id": CODEX_CLIENT_ID},
            )
            body = _json_or_error(r, "OpenAI")
            if not body.get("user_code") or not body.get("device_auth_id"):
                raise OAuthError("OpenAI не выдал код подтверждения")
            expires = time.time() + 15 * 60
            _save_flow(
                flow_id,
                {
                    **base_state,
                    "device_auth_id": body["device_auth_id"],
                    "user_code": body["user_code"],
                },
                15 * 60,
            )
            return _device_answer(
                flow_id,
                f"{CODEX_ISSUER}/codex/device",
                body["user_code"],
                expires,
                max(3, int(body.get("interval") or 5)),
            )

        if spec.flow == "xai":
            disc = _json_or_error(
                await http.get(f"{XAI_ISSUER}/.well-known/openid-configuration"), "xAI"
            )
            device_ep = _trusted_xai(disc.get("device_authorization_endpoint"))
            token_ep = _trusted_xai(disc.get("token_endpoint"))
            r = await http.post(device_ep, data={"client_id": XAI_CLIENT_ID, "scope": XAI_SCOPE})
            body = _json_or_error(r, "xAI")
            expires = time.time() + float(body.get("expires_in") or 600)
            _save_flow(
                flow_id,
                {**base_state, "device_code": body["device_code"], "token_endpoint": token_ep},
                int(expires - time.time()),
            )
            return _device_answer(
                flow_id,
                body.get("verification_uri_complete") or body.get("verification_uri"),
                body.get("user_code"),
                expires,
                body.get("interval"),
            )
    raise OAuthError(f"{kind}: неизвестный способ входа {spec.flow}")


def _device_answer(flow_id, url, user_code, expires, interval) -> dict:
    try:
        interval_s = max(2, int(float(interval))) if interval else 5
    except (TypeError, ValueError):
        interval_s = 5
    if interval_s > 60:  # MiniMax reports milliseconds
        interval_s = max(2, interval_s // 1000)
    return {
        "flow_id": flow_id,
        "type": "device",
        "verification_uri": url,
        "user_code": user_code,
        "expires_at": expires,
        "interval": interval_s,
    }


def _trusted_xai(url: Any) -> str:
    text = str(url or "")
    host = httpx.URL(text).host if text.startswith("https://") else ""
    if not (host == "x.ai" or host.endswith(".x.ai")):
        raise OAuthError("xAI вернул адрес входа вне домена x.ai — вход прерван")
    return text


def _json_or_error(r: httpx.Response, who: str) -> dict:
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {}
    if r.status_code >= 400:
        detail = (
            body.get("error_description")
            or (body.get("base_resp") or {}).get("status_msg")
            or body.get("error")
            or r.text[:200]
        )
        raise OAuthError(f"{who}: HTTP {r.status_code} {detail}".strip())
    return body if isinstance(body, dict) else {}


# ── Polling a device sign-in ─────────────────────────────────────────────────


async def poll(flow_id: str) -> tuple[str, dict | None, str | None]:
    """One poll: ("pending"|"success"|"error", token state on success, message)."""
    flow = load_flow(flow_id)
    if flow is None:
        return "error", None, "Время на вход истекло — начните заново"
    kind = flow["kind"]
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as http:
        if kind == "minimax":
            region = flow["region"]
            r = await http.post(
                f"{MINIMAX[region]['oauth']}/oauth2/token",
                data={
                    "grant_type": MINIMAX_USER_CODE_GRANT,
                    "client_id": MINIMAX_CLIENT_ID,
                    "user_code": flow["user_code"],
                    "code_verifier": flow["verifier"],
                },
                headers={"Accept": "application/json"},
            )
            body = _safe_json(r)
            if r.status_code >= 400 or body.get("status") == "error":
                msg = (body.get("base_resp") or {}).get("status_msg") or r.text[:200]
                return "error", None, f"MiniMax: {msg}"
            if body.get("status") != "success":
                return "pending", None, None
            state = {
                "access": body["access_token"],
                "refresh": body.get("refresh_token"),
                "expires_at": _expires_at(body, field_name="expired_in"),
                "region": region,
                "token_endpoint": f"{MINIMAX[region]['oauth']}/oauth2/token",
                "base_url": body.get("resource_url") or MINIMAX[region]["api"],
            }
            return "success", state, body.get("notification_message")

        if kind in ("moonshot", "xai"):
            token_ep = (
                f"{KIMI_OAUTH}/api/oauth/token" if kind == "moonshot" else flow["token_endpoint"]
            )
            client_id = KIMI_CLIENT_ID if kind == "moonshot" else XAI_CLIENT_ID
            r = await http.post(
                token_ep,
                data={
                    "client_id": client_id,
                    "device_code": flow["device_code"],
                    "grant_type": DEVICE_CODE_GRANT,
                },
                headers=kimi_headers() if kind == "moonshot" else {},
            )
            body = _safe_json(r)
            err = body.get("error")
            if err in ("authorization_pending", "slow_down"):
                return "pending", None, None
            if r.status_code >= 400 or err:
                return (
                    "error",
                    None,
                    body.get("error_description") or err or f"HTTP {r.status_code}",
                )
            state = _token_state(body, token_endpoint=token_ep)
            return "success", state, None

        if kind == "openai":
            r = await http.post(
                f"{CODEX_ISSUER}/api/accounts/deviceauth/token",
                json={"device_auth_id": flow["device_auth_id"], "user_code": flow["user_code"]},
            )
            if r.status_code in (403, 404):  # not approved yet
                return "pending", None, None
            body = _safe_json(r)
            if r.status_code != 200:
                return "error", None, f"OpenAI: HTTP {r.status_code}"
            tr = await http.post(
                CODEX_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": body.get("authorization_code", ""),
                    "redirect_uri": f"{CODEX_ISSUER}/deviceauth/callback",
                    "client_id": CODEX_CLIENT_ID,
                    "code_verifier": body.get("code_verifier", ""),
                },
            )
            tokens = _safe_json(tr)
            if tr.status_code != 200 or not tokens.get("access_token"):
                return "error", None, f"OpenAI: обмен кода не удался (HTTP {tr.status_code})"
            return "success", _token_state(tokens, token_endpoint=CODEX_TOKEN_URL), None
    return "error", None, f"{kind}: вход не поддерживается"


async def exchange_openrouter_code(flow: dict, code: str) -> str:
    """OpenRouter PKCE: the code becomes a user-scoped API key."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
        r = await http.post(
            OPENROUTER_KEYS_URL,
            json={
                "code": code,
                "code_verifier": flow["verifier"],
                "code_challenge_method": "S256",
            },
        )
    body = _json_or_error(r, "OpenRouter")
    key = str(body.get("key") or "").strip()
    if not key:
        raise OAuthError("OpenRouter не выдал ключ")
    return key


def _safe_json(r: httpx.Response) -> dict:
    try:
        body = r.json()
        return body if isinstance(body, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _token_state(body: dict, *, token_endpoint: str) -> dict:
    claims = jwt_claims(body.get("id_token") or "") or jwt_claims(body.get("access_token") or "")
    return {
        "access": body["access_token"],
        "refresh": body.get("refresh_token"),
        "expires_at": _expires_at(body),
        "token_endpoint": token_endpoint,
        "account": claims.get("email") or claims.get("name") or claims.get("sub"),
    }


# ── Stored state and fresh credentials ───────────────────────────────────────


def encode_state(state: dict) -> str:
    return encrypt(json.dumps(state))


def decode_state(blob: str | None) -> dict | None:
    text = decrypt(blob)
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def public_info(kind: str, state: dict | None) -> dict:
    """What the UI may show about a signed-in node (never the tokens)."""
    if not state:
        return {}
    account = state.get("account")
    if not account and kind == "openai":
        auth = jwt_claims(state.get("access") or "").get("https://api.openai.com/profile", {})
        account = auth.get("email") if isinstance(auth, dict) else None
    return {
        "account": account,
        "expires_at": state.get("expires_at"),
        "region": state.get("region"),
        "refreshable": bool(state.get("refresh")),
    }


def kimi_headers() -> dict[str, str]:
    import platform

    from app.config import settings

    return {
        "X-Msh-Platform": "kimi_cli",
        "X-Msh-Version": "1.0.0",
        "X-Msh-Device-Name": "ai-workspace",
        "X-Msh-Device-Model": platform.machine() or "server",
        "X-Msh-Os-Version": platform.release() or "linux",
        "X-Msh-Device-Id": hashlib.sha256(settings.app_secret_key.encode()).hexdigest()[:32],
    }


def codex_headers(access_token: str) -> dict[str, str]:
    """Identity and workspace headers the Codex backend expects."""
    headers = {"originator": CODEX_ORIGINATOR, "User-Agent": f"{CODEX_ORIGINATOR}/1.0"}
    auth = jwt_claims(access_token).get("https://api.openai.com/auth", {})
    if isinstance(auth, dict):
        if isinstance(auth.get("chatgpt_account_id"), str):
            headers["ChatGPT-Account-ID"] = auth["chatgpt_account_id"]
        residency = auth.get("chatgpt_data_residency") or auth.get("chatgpt_compute_residency")
        if isinstance(residency, str) and residency.strip():
            headers["x-openai-internal-codex-residency"] = residency.strip()
    return headers


@dataclass
class Runtime:
    """How to call a signed-in node right now."""

    api: str  # "openai" | "anthropic" | "responses"
    base_url: str
    token: str
    headers: dict[str, str]


def runtime_for(kind: str, state: dict) -> Runtime:
    spec = SPECS[kind]
    token = state["access"]
    base = (state.get("base_url") or spec.base_url).rstrip("/")
    if spec.api == "anthropic" and base.endswith("/v1"):
        # MiniMax returns resource_url ".../anthropic/v1"; callers append "/v1/messages".
        base = base[: -len("/v1")]
    headers: dict[str, str] = {}
    if kind == "moonshot":
        headers = kimi_headers()
    elif kind == "openai":
        headers = codex_headers(token)
    return Runtime(api=spec.api, base_url=base, token=token, headers=headers)


def _needs_refresh(state: dict) -> bool:
    exp = state.get("expires_at")
    return bool(exp) and float(exp) - time.time() < REFRESH_SKEW_S


def runtime_credentials(instance_id: str, kind: str, blob: str | None) -> Runtime:
    """Fresh credentials for a signed-in node, refreshing an expiring token.

    Synchronous: the provider registry resolves nodes on the hot path. A
    refresh is rare (once per token lifetime) and serialised per node by a
    Redis lock; the new state goes to Postgres first, then to the cache.
    """
    state = _cached_state(instance_id) or decode_state(blob)
    if not state or not state.get("access"):
        raise OAuthError(f"{kind}: вход через сайт не выполнен — войдите заново")
    if _needs_refresh(state):
        state = _refresh_locked(instance_id, kind, state)
    return runtime_for(kind, state)


def _cached_state(instance_id: str) -> dict | None:
    try:
        raw = _redis().get(TOKEN_CACHE_KEY.format(instance_id))
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    return decode_state(raw.decode() if isinstance(raw, bytes) else raw)


def remember_state(instance_id: str, state: dict | None) -> None:
    key = TOKEN_CACHE_KEY.format(instance_id)
    try:
        if state:
            _redis().set(key, encode_state(state))
        else:
            _redis().delete(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("provider_oauth_cache_write_failed", error=str(exc)[:200])


def _refresh_locked(instance_id: str, kind: str, state: dict) -> dict:
    redis = _redis()
    lock = REFRESH_LOCK_KEY.format(instance_id)
    deadline = time.time() + 20
    while not redis.set(lock, "1", nx=True, ex=30):
        # Another process is refreshing: wait for its result instead of
        # spending the (rotating) refresh token a second time.
        time.sleep(0.25)
        fresh = _cached_state(instance_id)
        if fresh and not _needs_refresh(fresh):
            return fresh
        if time.time() > deadline:
            raise OAuthError(f"{kind}: обновление входа не завершилось — повторите позже")
    try:
        fresh = _cached_state(instance_id)
        if fresh and not _needs_refresh(fresh):
            return fresh
        new_state = refresh_state(kind, state)
        persist_state_sync(instance_id, new_state)
        remember_state(instance_id, new_state)
        logger.info("provider_oauth_refreshed", kind=kind, instance=instance_id)
        return new_state
    finally:
        redis.delete(lock)


def refresh_state(kind: str, state: dict) -> dict:
    refresh = state.get("refresh")
    if not refresh:
        raise OAuthError(f"{kind}: срок входа истёк, а обновить его нечем — войдите заново")
    endpoint = state.get("token_endpoint")
    if kind == "minimax":
        data = {
            "grant_type": "refresh_token",
            "client_id": MINIMAX_CLIENT_ID,
            "refresh_token": refresh,
        }
        headers = {"Accept": "application/json"}
    elif kind == "moonshot":
        data = {
            "grant_type": "refresh_token",
            "client_id": KIMI_CLIENT_ID,
            "refresh_token": refresh,
        }
        headers = kimi_headers()
    elif kind == "openai":
        endpoint = CODEX_TOKEN_URL
        data = {
            "grant_type": "refresh_token",
            "client_id": CODEX_CLIENT_ID,
            "refresh_token": refresh,
        }
        headers = {}
    elif kind == "xai":
        data = {"grant_type": "refresh_token", "client_id": XAI_CLIENT_ID, "refresh_token": refresh}
        headers = {}
    else:
        raise OAuthError(f"{kind}: обновление входа не поддерживается")
    with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}) as http:
        r = http.post(endpoint, data=data, headers=headers)
    body = _safe_json(r)
    if r.status_code >= 400 or (kind == "minimax" and body.get("status") not in (None, "success")):
        detail = body.get("error_description") or body.get("error") or f"HTTP {r.status_code}"
        raise OAuthError(f"{kind}: вход больше не действует ({detail}) — войдите заново")
    access = body.get("access_token")
    if not access:
        raise OAuthError(f"{kind}: при обновлении входа не пришёл токен — войдите заново")
    expires = _expires_at(body, field_name="expired_in" if kind == "minimax" else "expires_in")
    return {
        **state,
        "access": access,
        # A refresh response may omit refresh_token when it is not rotated.
        "refresh": body.get("refresh_token") or refresh,
        "expires_at": expires,
    }


def persist_state_sync(instance_id: str, state: dict) -> None:
    """Write the token state to Postgres from synchronous code (psycopg2)."""
    import psycopg2

    from app.config import settings

    conn = psycopg2.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password,
        dbname=settings.postgres_db,
        connect_timeout=10,
    )
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE provider_instances SET oauth_encrypted = %s, updated_at = now() WHERE id = %s",
                (encode_state(state), instance_id),
            )
    finally:
        conn.close()


async def list_models(kind: str, rt: Runtime) -> list[str]:
    """Models the signed-in account can use (also the connection check)."""
    spec = SPECS[kind]
    headers = {"Authorization": f"Bearer {rt.token}", **rt.headers}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
        if kind == "minimax":
            # No listing for the Token Plan: one 1-token call proves the token works.
            r = await http.post(
                f"{rt.base_url}/v1/messages",
                headers={**headers, "anthropic-version": "2023-06-01"},
                json={
                    "model": spec.static_models[1],
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
            _json_or_error(r, "MiniMax")
            return list(spec.static_models)
        if kind == "openai":
            # The Codex catalog hides models newer than the client_version it is told.
            r = await http.get(
                f"{rt.base_url}/models", params={"client_version": "99.0.0"}, headers=headers
            )
            body = _json_or_error(r, "OpenAI")
            return [
                str(m.get("slug"))
                for m in body.get("models") or []
                if m.get("slug") and m.get("visibility") not in ("hide", "hidden")
            ]
        r = await http.get(f"{rt.base_url}/models", headers=headers)
        body = _json_or_error(r, spec.label)
        ids = [str(m.get("id")) for m in body.get("data") or [] if m.get("id")]
        return ids or list(spec.static_models)
