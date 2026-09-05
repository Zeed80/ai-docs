"""Черновик назначения вмещает не только модель.

Раньше `AssignmentDraftIn.slots` был `dict[str, str | None]` — только имя
модели. Поэтому рассуждение, узел и разрешение облака применялись немедленно,
отдельными запросами, и в одной карточке получалось два разных поведения:
модель ждала кнопки «Применить», а соседний переключатель срабатывал сразу.

Отдельно проверяется согласованность: валидация отвергала облачную модель по
БАЗОВОМУ признаку слота, игнорируя выданное разрешение, — то есть была строже
применения, которое разрешение учитывает.
"""

import pytest

from app.api.providers_api import (
    AssignmentDraftIn,
    SlotDraft,
    _as_slot_draft,
    _cloud_overrides,
)

# ── Совместимость со старой формой ───────────────────────────────────────────


def test_a_bare_string_still_means_just_the_model():
    payload = AssignmentDraftIn(slots={"agent_email": "some_model_key"})
    assert payload.model_slots == {"agent_email": "some_model_key"}
    assert payload.drafts["agent_email"].model == "some_model_key"
    assert payload.drafts["agent_email"].thinking is None


def test_none_still_means_clear_the_slot():
    payload = AssignmentDraftIn(slots={"agent_email": None})
    assert payload.model_slots == {"agent_email": None}


def test_both_forms_can_be_mixed_in_one_request():
    payload = AssignmentDraftIn(
        slots={
            "agent_email": "model_a",
            "ocr_fast": {"model": "model_b", "thinking": True},
        }
    )
    assert payload.model_slots == {"agent_email": "model_a", "ocr_fast": "model_b"}
    assert payload.drafts["ocr_fast"].thinking is True


@pytest.mark.parametrize(
    "value, expected_model",
    [("key", "key"), (None, None), (SlotDraft(model="key"), "key")],
)
def test_normalisation_covers_every_accepted_shape(value, expected_model):
    assert _as_slot_draft(value).model == expected_model


# ── Новые поля ───────────────────────────────────────────────────────────────


def test_draft_carries_thinking_node_and_cloud():
    payload = AssignmentDraftIn(
        slots={
            "agent_email": {
                "model": "some_key",
                "thinking": False,
                "thinking_level": "high",
                "allow_cloud": True,
                "preferred_instance": "gpu-node",
            }
        }
    )
    d = payload.drafts["agent_email"]
    assert (d.thinking, d.thinking_level) == (False, "high")
    assert d.allow_cloud is True
    assert d.preferred_instance == "gpu-node"


def test_only_explicit_cloud_decisions_are_collected():
    """None означает «не трогать», и такой слот не должен попадать в
    переопределения — иначе черновик молча закрывал бы уже открытое облако."""
    payload = AssignmentDraftIn(
        slots={
            "agent_email": {"allow_cloud": True},
            "ocr_fast": {"allow_cloud": False},
            "agent_large": {"model": "x"},
        }
    )
    assert _cloud_overrides(payload) == {"agent_email": True, "ocr_fast": False}


def test_an_invalid_level_is_rejected_at_the_schema():
    with pytest.raises(Exception):
        AssignmentDraftIn(slots={"agent_email": {"thinking_level": "очень высокое"}})
