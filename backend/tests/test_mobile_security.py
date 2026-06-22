"""Security contract checks for Android/mobile integration."""

from app.middleware.rate_limit import _LOGIN_PATHS


def test_qr_login_routes_are_login_rate_limited() -> None:
    assert "/api/auth/qr-login/create" in _LOGIN_PATHS
    assert "/api/auth/qr-login/redeem" in _LOGIN_PATHS

