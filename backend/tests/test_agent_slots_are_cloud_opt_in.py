"""Через агентские слоты проходит содержимое писем, счетов и чертежей.

Облако для них не запрещено — CLAUDE.md прямо разрешает cloud-модели для
planner/auditor и генерации писем. Но раньше эти слоты не были помечены
local-only вовсе, то есть облачная модель назначалась на «Письма» обычным
выбором из списка: тела деловых писем уходили наружу без единого вопроса.

Теперь они cloud-opt-in: по умолчанию локальные, облако включается отдельным
защищённым действием, а не побочным эффектом выбора модели.
"""

import pytest

from app.api.providers_api import (
    _slot_base_local_only,
    _slot_effective_local_only,
)

# Слоты, через которые проходит пользовательский контент.
CONTENT_BEARING_SLOTS = [
    "agent_orchestrator",
    "agent_fast",
    "agent_email",
    "agent_large",
    "agent_compression",
    "agent_auditor",
    "ocr_fast",
    "structured_extraction",
    "ocr_large",
    "embedding",
    "rerank",
    "cad_spec_read",
    "cad_text_ocr",
    "cad_spec_draft",
]


@pytest.mark.parametrize("slot", CONTENT_BEARING_SLOTS)
def test_content_bearing_slot_is_local_by_default(slot):
    assert _slot_base_local_only(slot), (
        f"слот {slot} видит пользовательский контент, но не помечен local-only — "
        "облачная модель назначится на него без подтверждения"
    )


@pytest.mark.parametrize("slot", ["agent_email", "agent_orchestrator"])
def test_cloud_stays_possible_after_an_explicit_opt_in(slot):
    """Запрет не жёсткий: заявленная возможность облачного планировщика и
    генерации писем сохраняется, но требует явного разрешения."""
    assert _slot_effective_local_only(slot, cloud_slots=set()) is True
    assert _slot_effective_local_only(slot, cloud_slots={slot}) is False
