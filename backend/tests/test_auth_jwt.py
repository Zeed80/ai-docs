"""Tests for app/auth/jwt.py — dev mode, role mapping, require_role."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock, MagicMock, patch

from app.auth.models import UserInfo, UserRole, ROLE_PERMISSIONS
from app.auth.jwt import (
    _groups_to_roles,
    require_role,
    get_current_user,
    _DEV_USER,
    _assert_user_active,
)


# ── _groups_to_roles ──────────────────────────────────────────────────────────

def test_admins_group_maps_to_admin():
    assert UserRole.admin in _groups_to_roles(["admins"])


def test_managers_group_maps_to_manager():
    assert UserRole.manager in _groups_to_roles(["managers"])


def test_accountants_group_maps_to_accountant():
    assert UserRole.accountant in _groups_to_roles(["accountants"])


def test_buyers_group_maps_to_buyer():
    assert UserRole.buyer in _groups_to_roles(["buyers"])


def test_engineers_group_maps_to_engineer():
    assert UserRole.engineer in _groups_to_roles(["engineers"])


def test_technologists_group_maps_to_technologist():
    assert UserRole.technologist in _groups_to_roles(["technologists"])


def test_unknown_group_falls_back_to_viewer():
    roles = _groups_to_roles(["unknown-department"])
    assert roles == [UserRole.viewer]


def test_empty_groups_falls_back_to_viewer():
    assert _groups_to_roles([]) == [UserRole.viewer]


def test_multiple_groups_accumulates_roles():
    roles = _groups_to_roles(["engineers", "technologists"])
    assert UserRole.engineer in roles
    assert UserRole.technologist in roles


def test_group_name_is_case_insensitive():
    assert UserRole.admin in _groups_to_roles(["Admins"])
    assert UserRole.manager in _groups_to_roles(["MANAGERS"])


# ── UserRole enum completeness ─────────────────────────────────────────────────

def test_technologist_in_user_role_enum():
    assert UserRole.technologist.value == "technologist"


def test_all_roles_have_permissions():
    for role in UserRole:
        if role != UserRole.viewer:
            assert role in ROLE_PERMISSIONS, f"Role {role} has no permissions"


def test_admin_has_wildcard():
    assert "*" in ROLE_PERMISSIONS[UserRole.admin]


def test_technologist_has_technology_permissions():
    perms = ROLE_PERMISSIONS[UserRole.technologist]
    assert "technology.read" in perms
    assert "technology.create" in perms
    assert "technology.normcontrol" in perms


# ── Dev mode bypass ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dev_mode_returns_dev_user():
    """When auth_enabled=False, get_current_user returns _DEV_USER."""
    mock_request = MagicMock()
    mock_request.headers.get.return_value = None

    with patch("app.auth.jwt.settings") as mock_settings:
        mock_settings.auth_enabled = False
        user = await get_current_user(mock_request, None)

    assert user is _DEV_USER
    assert UserRole.admin in user.roles


@pytest.mark.asyncio
async def test_auth_enabled_no_token_raises_401():
    """When auth_enabled=True and no token provided, raises 401."""
    mock_request = MagicMock()
    mock_request.headers.get.return_value = None

    with patch("app.auth.jwt.settings") as mock_settings:
        mock_settings.auth_enabled = True
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(mock_request, None)

    assert exc_info.value.status_code == 401


# ── _assert_user_active (token revocation) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_active_cache_hit_allows():
    """Cached '1' (active) returns without touching the DB."""
    redis = MagicMock()
    redis.get = AsyncMock(return_value="1")
    with patch("app.utils.redis_client.get_async_redis", return_value=redis):
        await _assert_user_active("sub-active")  # no exception


@pytest.mark.asyncio
async def test_active_cache_inactive_raises_403():
    """Cached '0' (deactivated) rejects with 403."""
    redis = MagicMock()
    redis.get = AsyncMock(return_value="0")
    with patch("app.utils.redis_client.get_async_redis", return_value=redis):
        with pytest.raises(HTTPException) as exc_info:
            await _assert_user_active("sub-inactive")
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_active_check_fails_open_on_infra_error():
    """If Redis and DB both fail, access is allowed (no global lockout)."""
    with patch("app.utils.redis_client.get_async_redis", side_effect=RuntimeError("down")), \
         patch("app.db.session._get_session_factory", side_effect=RuntimeError("db down")):
        await _assert_user_active("sub-any")  # no exception


# ── require_role ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_require_role_passes_for_matching_role():
    user = UserInfo(
        sub="u1", email="e@t.com", name="T", preferred_username="t",
        roles=[UserRole.technologist],
    )
    checker = require_role(UserRole.technologist)
    # Patch get_current_user to return our user
    with patch("app.auth.jwt.get_current_user", return_value=user):
        result = await checker(user)
    assert result is user


@pytest.mark.asyncio
async def test_require_role_passes_for_admin():
    """Admin always passes any role check."""
    admin = UserInfo(
        sub="a1", email="a@t.com", name="A", preferred_username="a",
        roles=[UserRole.admin],
    )
    checker = require_role(UserRole.accountant)
    with patch("app.auth.jwt.get_current_user", return_value=admin):
        result = await checker(admin)
    assert result is admin


@pytest.mark.asyncio
async def test_require_role_raises_403_for_wrong_role():
    viewer = UserInfo(
        sub="v1", email="v@t.com", name="V", preferred_username="v",
        roles=[UserRole.viewer],
    )
    checker = require_role(UserRole.manager)
    with patch("app.auth.jwt.get_current_user", return_value=viewer):
        with pytest.raises(HTTPException) as exc_info:
            await checker(viewer)
    assert exc_info.value.status_code == 403
