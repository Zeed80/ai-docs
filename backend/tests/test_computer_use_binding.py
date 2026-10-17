"""computer_use through the gateway: bound to the real order, action passed on (E50).

Live: the model invented its work order ("wo-eval-stal-email") and the shared
/execute endpoint never learned which action it was asked for, so every
grant-bound browser call failed with 404 or 422.
"""

from __future__ import annotations

import uuid

from starlette.requests import Request

from app.api.capability_router import _bind_computer_use
from app.auth.effect_fence import EFFECT_FENCE_HEADER, new_effect_fence


def _request(headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


def test_order_and_step_come_from_the_signed_fence_not_the_model():
    order, step = uuid.uuid4(), uuid.uuid4()
    token = new_effect_fence(
        work_order_id=order,
        step_id=step,
        attempt_id=uuid.uuid4(),
        plan_id=uuid.uuid4(),
        plan_revision=1,
        operation_key="k",
    )
    shaped = _bind_computer_use(
        "browser_fetch",
        "/api/computer-use/execute",
        {
            "work_order_id": "wo-invented",
            "step_id": "step-1",
            "target": "http://x/",
            "max_chars": 500,
        },
        _request({EFFECT_FENCE_HEADER: token}),
    )
    assert shaped["work_order_id"] == str(order)
    assert shaped["step_id"] == str(step)
    assert shaped["action"] == "browser_fetch"
    assert shaped["body"] == {"max_chars": 500}


def test_click_fields_reach_the_broker_body():
    shaped = _bind_computer_use(
        "desktop_click",
        "/api/computer-use/execute",
        {
            "work_order_id": str(uuid.uuid4()),
            "target": "sess",
            "ref": "e7",
            "revision": 3,
            "arguments": {"card": "c"},
        },
        _request({}),
    )
    assert shaped["body"] == {"ref": "e7", "revision": 3, "card": "c"}


def test_a_forged_fence_does_not_bind():
    shaped = _bind_computer_use(
        "browser_fetch",
        "/api/computer-use/execute",
        {"work_order_id": "model-value", "target": "http://x/"},
        _request({EFFECT_FENCE_HEADER: "forged.token"}),
    )
    assert shaped["work_order_id"] == "model-value"


def test_the_session_named_as_session_id_becomes_the_target():
    """E52: observe went out with the page URL as target and session_id aside."""
    shaped = _bind_computer_use(
        "desktop_observe",
        "/api/computer-use/execute",
        {
            "work_order_id": str(uuid.uuid4()),
            "target": "http://eval-sites/nikel/price.html",
            "arguments": {"session_id": "19094c69-2555-4bc5-8a7b-af5c0fdf2e37"},
        },
        _request({}),
    )
    assert shaped["target"] == "19094c69-2555-4bc5-8a7b-af5c0fdf2e37"
    assert shaped["body"] == {}


def test_desktop_start_keeps_its_url():
    shaped = _bind_computer_use(
        "desktop_start",
        "/api/computer-use/execute",
        {"work_order_id": str(uuid.uuid4()), "target": "http://x/", "session_id": "s"},
        _request({}),
    )
    assert shaped["target"] == "http://x/"
