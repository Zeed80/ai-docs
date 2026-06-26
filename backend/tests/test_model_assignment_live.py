"""Live production-stack checks for model assignment and reasoning controls.

Run from a backend environment that can reach the running stack:

    LIVE_STACK=1 PROD_SAFE_MUTATE=1 LIVE_ADMIN_API_KEY='...' \
      python3 -m pytest backend/tests/test_model_assignment_live.py -s --tb=short

The mutating test is fail-closed: it skips unless explicit prod-safe mutation and
admin auth are present, snapshots the current slot reasoning override, and
restores it in ``finally``.
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.live

_LIVE = os.environ.get("LIVE_STACK") == "1"
_SAFE_MUTATE = os.environ.get("PROD_SAFE_MUTATE") == "1"
_BACKEND = os.environ.get("BACKEND_URL", "http://localhost:8000").rstrip("/")


def _headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    bearer = os.environ.get("LIVE_ADMIN_BEARER", "").strip()
    api_key = os.environ.get("LIVE_ADMIN_API_KEY", "").strip()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _cookies() -> dict[str, str]:
    cookie = os.environ.get("LIVE_ADMIN_COOKIE", "").strip()
    if not cookie:
        return {}
    out: dict[str, str] = {}
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _mutation_headers(client: httpx.AsyncClient) -> dict[str, str]:
    headers = {"Content-Type": "application/json", **_headers()}
    csrf = client.cookies.get("csrf_token")
    if csrf and "Authorization" not in headers and "X-API-Key" not in headers:
        headers["X-CSRF-Token"] = csrf
    return headers


def _skip_if_not_live() -> None:
    if not _LIVE:
        pytest.skip("LIVE_STACK!=1")


def _skip_if_no_admin_auth() -> None:
    if not _headers() and not _cookies():
        pytest.skip("LIVE_ADMIN_API_KEY, LIVE_ADMIN_COOKIE or LIVE_ADMIN_BEARER is required")


@pytest.mark.asyncio
async def test_live_assignment_read_model_is_consistent() -> None:
    _skip_if_not_live()
    _skip_if_no_admin_auth()
    async with httpx.AsyncClient(
        base_url=_BACKEND,
        headers=_headers(),
        cookies=_cookies(),
        timeout=20.0,
    ) as client:
        draft = await client.get("/api/providers/assignment-draft")
        live_models = await client.get("/api/providers/live-models")
        local_status = await client.get("/api/local-models/status")

        assert draft.status_code == 200, draft.text[:300]
        assert live_models.status_code == 200, live_models.text[:300]
        assert local_status.status_code == 200, local_status.text[:300]

        slots = draft.json().get("slots") or []
        assert slots, "assignment-draft returned no slots"
        for slot in slots:
            assert slot["slot"]
            assert "current_model" in slot
            assert "required_modality" in slot
            assert "thinking_effective" in slot
            assert "thinking_supported_by_slot" in slot
        smoke_slot = next((slot for slot in slots if slot.get("model")), None)
        assert smoke_slot is not None, "no assigned slot available for smoke dry-run"
        smoke = await client.post(
            f"/api/providers/slots/{smoke_slot['slot']}/smoke",
            headers=_mutation_headers(client),
            json={"model": smoke_slot["model"], "dry_run": True},
        )
        assert smoke.status_code == 200, smoke.text[:300]
        assert smoke.json()["dry_run"] is True

        selectable = [
            model for model in live_models.json()
            if model.get("loaded") or model.get("status") != "disabled"
        ]
        assert selectable, "live-models returned no selectable models"


@pytest.mark.asyncio
async def test_live_slot_reasoning_override_roundtrip_safe_mutation() -> None:
    _skip_if_not_live()
    _skip_if_no_admin_auth()
    if not _SAFE_MUTATE:
        pytest.skip("PROD_SAFE_MUTATE!=1")

    async with httpx.AsyncClient(
        base_url=_BACKEND,
        headers=_headers(),
        cookies=_cookies(),
        timeout=30.0,
    ) as client:
        before_resp = await client.get("/api/providers/assignment-draft")
        assert before_resp.status_code == 200, before_resp.text[:300]
        slots = before_resp.json().get("slots") or []
        slot = next(
            (
                item for item in slots
                if item.get("thinking_supported_by_slot")
                and item.get("thinking_supported_by_model")
            ),
            None,
        )
        if slot is None:
            pytest.skip("no slot with a reasoning-capable assigned model")

        slot_name = slot["slot"]
        original = slot.get("thinking_override")
        target = False if original is not False else True

        try:
            patch = await client.patch(
                f"/api/providers/slots/{slot_name}/thinking",
                headers=_mutation_headers(client),
                json={"enabled": target},
            )
            assert patch.status_code == 200, patch.text[:300]

            after_resp = await client.get("/api/providers/assignment-draft")
            assert after_resp.status_code == 200, after_resp.text[:300]
            after_slot = next(
                item for item in after_resp.json().get("slots", [])
                if item.get("slot") == slot_name
            )
            assert after_slot["thinking_override"] is target
            assert after_slot["thinking_effective"] is target
        finally:
            restore = await client.patch(
                f"/api/providers/slots/{slot_name}/thinking",
                headers=_mutation_headers(client),
                json={"enabled": original},
            )
            assert restore.status_code == 200, restore.text[:300]
