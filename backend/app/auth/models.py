"""Auth domain — user identity and roles."""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel


class UserRole(str, Enum):
    admin = "admin"
    manager = "manager"       # approves invoices, sees everything
    accountant = "accountant"  # works with invoices and documents
    buyer = "buyer"            # works with suppliers and orders
    engineer = "engineer"      # works with drawings and specs
    viewer = "viewer"          # read-only


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
    },
    UserRole.viewer: {
        "document.read", "invoice.read", "supplier.read",
    },
}
