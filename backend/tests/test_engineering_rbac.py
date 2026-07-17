"""G2: engineering access policy — конструктор/нормоконтролёр/технолог/
расчётчик/руководитель and what each may do."""

import pytest
from fastapi import HTTPException

from app.auth.models import UserInfo, UserRole, require_permission, user_has_permission


def _user(*roles: UserRole) -> UserInfo:
    return UserInfo(sub="u1", email="u@x", name="U", preferred_username="u", roles=list(roles))


@pytest.mark.parametrize(
    ("role", "allowed", "denied"),
    [
        (
            UserRole.engineer,  # конструктор: создаёт, но не утверждает
            ["engineering.project_create", "engineering.revision_create",
             "engineering.change_create", "engineering.analysis_run"],
            ["engineering.revision_approve"],
        ),
        (
            UserRole.normcontroller,  # нормоконтролёр: подписывает, не чертит
            ["engineering.revision_approve", "engineering.change_sign"],
            ["engineering.revision_create", "engineering.project_create"],
        ),
        (
            UserRole.calculator,  # расчётчик: только расчёты
            ["engineering.analysis_run", "engineering.read"],
            ["engineering.revision_create", "engineering.revision_approve",
             "engineering.change_sign"],
        ),
        (
            UserRole.manager,  # руководитель: утверждает и решает по изменениям
            ["engineering.revision_approve", "engineering.change_apply"],
            ["engineering.revision_create"],
        ),
        (
            UserRole.technologist,  # технолог: читает инженерку, правит технологию
            ["engineering.read"],
            ["engineering.revision_create", "engineering.revision_approve"],
        ),
        (
            UserRole.viewer,
            [],
            ["engineering.read", "engineering.revision_approve"],
        ),
    ],
)
def test_role_matrix(role, allowed, denied):
    user = _user(role)
    for action in allowed:
        assert user_has_permission(user, action), f"{role} должен иметь {action}"
    for action in denied:
        assert not user_has_permission(user, action), f"{role} НЕ должен иметь {action}"


def test_admin_passes_everything():
    require_permission(_user(UserRole.admin), "engineering.revision_approve")


def test_require_permission_raises_403():
    with pytest.raises(HTTPException) as exc:
        require_permission(_user(UserRole.viewer), "engineering.revision_approve")
    assert exc.value.status_code == 403


def test_multiple_roles_union():
    # расчётчик + нормоконтролёр = обе способности
    user = _user(UserRole.calculator, UserRole.normcontroller)
    assert user_has_permission(user, "engineering.analysis_run")
    assert user_has_permission(user, "engineering.revision_approve")
