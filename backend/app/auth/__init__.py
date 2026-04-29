ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"*"},
    "manager": {
        "agent:read",
        "agent:run",
        "approval:decide",
        "document:read",
        "document:write",
        "invoice:approve",
        "invoice:export",
        "invoice:read",
    },
    "technologist": {
        "agent:read",
        "agent:run",
        "case:read",
        "case:write",
        "document:read",
        "document:write",
        "drawing:analyze",
        "email:draft",
    },
    "accountant": {
        "case:read",
        "document:read",
        "email:draft",
        "invoice:export",
        "invoice:read",
    },
    "buyer": {
        "document:read",
        "email:draft",
        "email:read",
        "supplier:read",
    },
    "viewer": {
        "document:read",
        "invoice:read",
        "supplier:read",
    },
}


def has_permission(roles: list[str], permission: str) -> bool:
    for role in roles:
        permissions = ROLE_PERMISSIONS.get(role, set())
        if "*" in permissions or permission in permissions:
            return True
    return False


def permissions_for_roles(roles: list[str]) -> list[str]:
    permissions: set[str] = set()
    for role in roles:
        role_permissions = ROLE_PERMISSIONS.get(role, set())
        if "*" in role_permissions:
            return ["*"]
        permissions.update(role_permissions)
    return sorted(permissions)
