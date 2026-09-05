"""Пригодность модели для слота считает сервер, а не интерфейс.

Правила жили только внутри _validate_assignment_draft, поэтому фронтенд держал
их вторую копию на TypeScript. Копии успели разойтись: список локальных
провайдеров и набор умеющих выключать рассуждение в page.tsx отличались от
серверных. Теперь источник один, и к каждой помехе прилагается подсказка,
каким действием она чинится.
"""

import pytest

from app.ai.schemas import Modality, ModelCapability, ModelStatus, ProviderKind
from app.api.providers_api import _model_eligibility


def _cap(**kw) -> ModelCapability:
    base = dict(
        name="k",
        provider=ProviderKind.OLLAMA,
        provider_model="some-model",
        status=ModelStatus.PRODUCTION,
        modalities={Modality.TEXT, Modality.VISION},
        local_only=True,
    )
    base.update(kw)
    return ModelCapability(**base)


def test_a_loaded_local_model_that_fits_is_simply_ok():
    verdict, reasons = _model_eligibility(
        "ocr_fast", _cap(), is_loaded=True, slot_local_only=True
    )
    assert verdict == "ok"
    assert reasons == []


def test_cloud_model_on_a_confidential_slot_is_forbidden_with_a_fix():
    verdict, reasons = _model_eligibility(
        "agent_email",
        _cap(provider=ProviderKind.ANTHROPIC, local_only=False),
        is_loaded=True,
        slot_local_only=True,
    )
    assert verdict == "forbidden"
    assert reasons[0].code == "cloud_for_confidential"
    # Подсказка ведёт туда, где это решается.
    assert reasons[0].fix_action == "open_cloud_provider"


def test_the_same_cloud_model_is_fine_once_the_slot_allows_cloud():
    verdict, _ = _model_eligibility(
        "agent_email",
        _cap(provider=ProviderKind.ANTHROPIC, local_only=False),
        is_loaded=True,
        slot_local_only=False,
    )
    assert verdict == "ok"


def test_a_model_absent_from_every_node_is_fixable_not_forbidden():
    """Модель можно скачать — это помеха, а не запрет."""
    verdict, reasons = _model_eligibility(
        "ocr_fast", _cap(), is_loaded=False, slot_local_only=True
    )
    assert verdict == "needs_action"
    assert reasons[0].code == "not_loaded"
    assert reasons[0].fix_action == "pull_model"


def test_missing_modality_is_unsuitable_but_still_selectable():
    """Слот просит vision, модель его не заявляет — предупреждение, не запрет:
    каталог может просто не знать о возможностях модели."""
    verdict, reasons = _model_eligibility(
        "ocr_fast",
        _cap(modalities={Modality.TEXT}),
        is_loaded=True,
        slot_local_only=True,
    )
    assert verdict == "unsuitable"
    assert reasons[0].code == "modality_mismatch"


def test_unverified_capabilities_say_so_instead_of_claiming_the_model_cannot():
    """«Не проверяли» и «не умеет» — разные вещи: первое чинится пробным
    запросом. Раньше обе ситуации давали одно и то же modality_mismatch."""
    verdict, reasons = _model_eligibility(
        "ocr_fast",
        _cap(
            provider=ProviderKind.DEEPSEEK,
            local_only=False,
            modalities=set(),
            capabilities_unknown=True,
        ),
        is_loaded=True,
        slot_local_only=False,
    )
    assert verdict == "needs_action"
    assert reasons[0].code == "capabilities_unknown"
    assert reasons[0].fix_action == "verify_model"


def test_a_forbidden_model_stays_forbidden_even_with_other_problems():
    """Запрет политики сильнее починимых помех: иначе слот показал бы
    «скачайте модель» там, где её вообще нельзя выбирать."""
    verdict, reasons = _model_eligibility(
        "agent_email",
        _cap(provider=ProviderKind.ANTHROPIC, local_only=False, modalities=set()),
        is_loaded=False,
        slot_local_only=True,
    )
    assert verdict == "forbidden"
    assert {r.code for r in reasons} >= {"cloud_for_confidential"}


@pytest.mark.asyncio
async def test_unknown_slot_is_404():
    from fastapi import HTTPException

    from app.api.providers_api import slot_candidates

    with pytest.raises(HTTPException) as exc:
        await slot_candidates("нет-такого-слота")
    assert exc.value.status_code == 404
