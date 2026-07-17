"""Auth domain — user identity and roles."""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel


class UserRole(str, Enum):
    admin = "admin"
    manager = "manager"           # руководитель: approves invoices/releases, sees everything
    accountant = "accountant"     # works with invoices and documents
    buyer = "buyer"               # works with suppliers and orders
    engineer = "engineer"         # конструктор: drawings, specs, engineering revisions
    technologist = "technologist" # технолог: manufacturing process plans
    normcontroller = "normcontroller"  # G2: нормоконтролёр — validates and signs releases
    calculator = "calculator"     # G2: расчётчик — runs analysis cases
    viewer = "viewer"             # read-only


class UserInfo(BaseModel):
    sub: str                   # Authentik user ID
    email: str
    name: str
    preferred_username: str
    roles: list[UserRole] = []
    groups: list[str] = []


# Role → allowed actions map (used by permission checks)
ROLE_PERMISSIONS: dict[UserRole, set[str]] = {
    UserRole.admin: {"*"},
    UserRole.manager: {
        "invoice.approve", "invoice.reject", "anomaly.resolve",
        "approval.decide", "document.read", "document.approve",
        "compare.decide",
        # G2: руководитель утверждает выпуск и решает по изменениям
        "engineering.read", "engineering.revision_approve",
        "engineering.change_sign", "engineering.change_apply",
    },
    UserRole.accountant: {
        "invoice.read", "invoice.export", "document.read",
        "document.extract", "table.export", "table.import",
        "norm.read",
    },
    UserRole.buyer: {
        "supplier.read", "supplier.merge", "compare.read",
        "document.read", "email.read", "email.send",
    },
    UserRole.engineer: {
        "document.read", "document.extract", "collection.read",
        "drawing.read", "drawing.analyze",
        # G2: конструктор создаёт проекты/ревизии/изменения, но НЕ утверждает
        "engineering.read", "engineering.project_create",
        "engineering.revision_create", "engineering.change_create",
        "engineering.change_sign", "engineering.change_apply",
        "engineering.analysis_run",
    },
    UserRole.technologist: {
        "document.read", "drawing.read", "drawing.analyze",
        "technology.read", "technology.create", "technology.edit",
        "technology.normcontrol", "technology.export",
        "collection.read",
        "engineering.read",
    },
    # G2: нормоконтролёр проверяет и подписывает выпуск, но сам не чертит
    UserRole.normcontroller: {
        "document.read", "drawing.read",
        "engineering.read", "engineering.revision_approve",
        "engineering.change_sign",
    },
    # G2: расчётчик гоняет расчётные кейсы, геометрию не меняет
    UserRole.calculator: {
        "document.read", "drawing.read",
        "engineering.read", "engineering.analysis_run",
    },
    UserRole.viewer: {
        "document.read", "invoice.read", "supplier.read",
    },
}


def user_has_permission(user: "UserInfo", action: str) -> bool:
    for role in user.roles:
        permissions = ROLE_PERMISSIONS.get(role, set())
        if "*" in permissions or action in permissions:
            return True
    return False


def require_permission(user: "UserInfo", action: str) -> None:
    """G2: raise 403 unless one of the user's roles grants ``action``.
    Admin's ``*`` passes everything; with AUTH_ENABLED=false every request is
    the dev admin, so development flows are unaffected."""
    from fastapi import HTTPException

    if not user_has_permission(user, action):
        raise HTTPException(
            403,
            f"Недостаточно прав: требуется {action} (роли: {', '.join(r.value for r in user.roles) or 'нет'})",
        )
