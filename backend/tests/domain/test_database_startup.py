from __future__ import annotations

import pytest

from backend.app.main import create_app


def test_production_startup_rejects_silent_create_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTO_CREATE_SCHEMA", "true")

    with pytest.raises(RuntimeError, match="AUTO_CREATE_SCHEMA=true is not allowed"):
        create_app()
