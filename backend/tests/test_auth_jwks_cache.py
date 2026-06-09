"""Tests for JWKS caching in auth/jwt.py.

Verifies that:
- Multiple rapid calls to _get_jwks() hit Authentik only once (cache hit)
- After TTL expiry the cache is refreshed (second HTTP call)
- Concurrent calls don't cause a thundering herd (only 1 fetch while lock is held)
"""

import asyncio
import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def reset_jwks_cache():
    """Reset module-level JWKS cache state before each test."""
    import app.auth.jwt as jwt_module
    original_cache = jwt_module._jwks_cache
    original_fetched_at = jwt_module._jwks_fetched_at
    jwt_module._jwks_cache = None
    jwt_module._jwks_fetched_at = 0.0
    yield
    jwt_module._jwks_cache = original_cache
    jwt_module._jwks_fetched_at = original_fetched_at


@pytest.mark.asyncio
async def test_jwks_fetched_only_once_for_multiple_calls():
    """10 rapid calls to _get_jwks() should result in exactly 1 HTTP request."""
    fake_jwks = {"keys": [{"kid": "test-key", "kty": "RSA"}]}

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value=fake_jwks)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("httpx.AsyncClient", return_value=mock_client):
        from app.auth.jwt import _get_jwks
        results = await asyncio.gather(*[_get_jwks() for _ in range(10)])

    assert all(r == fake_jwks for r in results), "All calls should return the same JWKS"
    assert mock_client.get.call_count == 1, (
        f"Expected 1 HTTP call, got {mock_client.get.call_count}"
    )


@pytest.mark.asyncio
async def test_jwks_refreshed_after_ttl_expiry():
    """After TTL expires, _get_jwks() should make a second HTTP request."""
    import app.auth.jwt as jwt_module

    fake_jwks_v1 = {"keys": [{"kid": "key-v1"}]}
    fake_jwks_v2 = {"keys": [{"kid": "key-v2"}]}

    call_count = 0

    async def mock_get_jwks_side_effect():
        nonlocal call_count
        call_count += 1
        return fake_jwks_v1 if call_count == 1 else fake_jwks_v2

    # First call — cold cache
    mock_response_1 = MagicMock()
    mock_response_1.raise_for_status = MagicMock()
    mock_response_1.json = MagicMock(return_value=fake_jwks_v1)

    mock_response_2 = MagicMock()
    mock_response_2.raise_for_status = MagicMock()
    mock_response_2.json = MagicMock(return_value=fake_jwks_v2)

    responses = [mock_response_1, mock_response_2]
    response_iter = iter(responses)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=lambda *a, **kw: next(response_iter))

    with patch("httpx.AsyncClient", return_value=mock_client):
        from app.auth.jwt import _get_jwks

        # First call — should fetch
        result1 = await _get_jwks()
        assert result1 == fake_jwks_v1

        # Simulate TTL expiry by backdating the cache timestamp
        jwt_module._jwks_fetched_at = time.monotonic() - jwt_module._JWKS_TTL - 1.0

        # Second call — cache expired, should fetch again
        result2 = await _get_jwks()
        assert result2 == fake_jwks_v2

    assert mock_client.get.call_count == 2, (
        f"Expected 2 HTTP calls (initial + after TTL), got {mock_client.get.call_count}"
    )


@pytest.mark.asyncio
async def test_jwks_warm_cache_no_additional_fetch():
    """Calling _get_jwks() within TTL window uses cache without HTTP call."""
    import app.auth.jwt as jwt_module

    fake_jwks = {"keys": [{"kid": "warm-key"}]}
    jwt_module._jwks_cache = fake_jwks
    jwt_module._jwks_fetched_at = time.monotonic()  # just fetched

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_client):
        from app.auth.jwt import _get_jwks
        result = await _get_jwks()

    assert result == fake_jwks
    mock_client.get.assert_not_called()
