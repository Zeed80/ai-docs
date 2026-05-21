"""Normcontrol agent: ГОСТ ЕСТД compliance checker for manufacturing tech processes.

Checks:
  ГОСТ 3.1102 — general ЕСТД requirements (document designation, standard system)
  ГОСТ 3.1118 — route card (МК) field requirements
  ГОСТ 3.1404 — operational card (ОК) for machining operations
  ГОСТ 3.1107 — surface roughness designation
  ГОСТ 3.1127 — time norms completeness
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.db.models import (
    ManufacturingOperation,
    ManufacturingProcessPlan,
    NormControlCheck,
    SurfaceMachiningSpec,
)

logger = structlog.get_logger()

# ── Check-code registry ───────────────────────────────────────────────────────

# Format: (check_code, gost_code, clause, severity, auto_fixable)
_CHECKS_META: dict[str, tuple[str, str, str, bool]] = {
    "ESTD_GEN_001": ("ГОСТ 3.1102", "п.1.1", "error",   False),
    "ESTD_GEN_002": ("ГОСТ 3.1102", "п.1.2", "error",   False),
    "ESTD_GEN_003": ("ГОСТ 3.1102", "п.2.1", "warning", True),
    "ESTD_MK_001":  ("ГОСТ 3.1118", "п.3.1", "error",   False),
    "ESTD_MK_002":  ("ГОСТ 3.1118", "п.3.2", "error",   False),
    "ESTD_MK_003":  ("ГОСТ 3.1118", "п.3.3", "warning", True),
    "ESTD_MK_004":  ("ГОСТ 3.1118", "п.3.4", "warning", False),
    "ESTD_MK_005":  ("ГОСТ 3.1118", "п.3.5", "error",   False),
    "ESTD_OK_001":  ("ГОСТ 3.1404", "п.2.1", "error",   False),
    "ESTD_OK_002":  ("ГОСТ 3.1404", "п.2.2", "error",   False),
    "ESTD_OK_003":  ("ГОСТ 3.1404", "п.2.3", "warning", True),
    "ESTD_OK_004":  ("ГОСТ 3.1404", "п.2.4", "warning", False),
    "ESTD_OK_005":  ("ГОСТ 3.1404", "п.3.1", "error",   False),
    "ESTD_RA_001":  ("ГОСТ 3.1107", "п.4.1", "warning", False),
    "ESTD_RA_002":  ("ГОСТ 3.1107", "п.4.2", "info",    False),
    "ESTD_NC_001":  ("ГОСТ 3.1127", "п.2.1", "error",   False),
    "ESTD_NC_002":  ("ГОСТ 3.1127", "п.2.2", "warning", True),
    "ESTD_NC_003":  ("ГОСТ 3.1127", "п.2.3", "error",   False),
}

_MACHINING_OPS = {"turning", "milling", "drilling", "grinding", "boring", "reaming", "honing", "broaching"}
_VALID_RA_VALUES = {0.025, 0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 6.3, 12.5, 25.0, 50.0, 100.0}


def _make_check(
    code: str,
    message: str,
    recommendation: str,
    plan_id: uuid.UUID,
    operation_id: uuid.UUID | None = None,
    form_type: str | None = None,
    evidence: dict | None = None,
) -> NormControlCheck:
    meta = _CHECKS_META.get(code, ("ГОСТ ЕСТД", None, "warning", False))
    return NormControlCheck(
        process_plan_id=plan_id,
        operation_id=operation_id,
        form_type=form_type,
        gost_code=meta[0],
        clause=meta[1],
        check_code=code,
        severity=meta[2],
        status="open",
        message=message,
        recommendation=recommendation,
        auto_fixable=meta[3],
        evidence=evidence or {},
        created_by="normcontrol_agent",
    )


# ── ГОСТ 3.1102 — General ЕСТД requirements ───────────────────────────────────

def _check_gost_3_1102(
    plan: ManufacturingProcessPlan,
    plan_id: uuid.UUID,
) -> list[NormControlCheck]:
    issues: list[NormControlCheck] = []

    if not plan.product_code or len(plan.product_code.strip()) < 3:
        issues.append(_make_check(
            "ESTD_GEN_001",
            f"Отсутствует или некорректное обозначение изделия (product_code='{plan.product_code}').",
            "Заполните поле обозначения согласно системе конструкторских документов предприятия (минимум 3 символа).",
            plan_id,
            form_type="МК",
            evidence={"product_code": plan.product_code},
        ))

    if plan.standard_system != "ЕСТД":
        issues.append(_make_check(
            "ESTD_GEN_002",
            f"Стандартная система документации указана как '{plan.standard_system}', ожидается 'ЕСТД'.",
            "Установите standard_system = 'ЕСТД' согласно ГОСТ 3.1102.",
            plan_id,
            form_type="МК",
            evidence={"standard_system": plan.standard_system},
        ))

    if not plan.version or plan.version.strip() == "":
        issues.append(_make_check(
            "ESTD_GEN_003",
            "Не указана литера технологического документа (version/литера).",
            "Заполните поле version литерой документа (О, О1, О2 для опытного производства; А, Б — для серийного).",
            plan_id,
            form_type="МК",
        ))

    return issues


# ── ГОСТ 3.1118 — Route card (МК) ─────────────────────────────────────────────

def _check_gost_3_1118(
    plan: ManufacturingProcessPlan,
    operations: list[ManufacturingOperation],
    plan_id: uuid.UUID,
) -> list[NormControlCheck]:
    issues: list[NormControlCheck] = []

    if not plan.material or len(plan.material.strip()) < 3:
        issues.append(_make_check(
            "ESTD_MK_001",
            f"В маршрутной карте не заполнено поле 'Материал' (material='{plan.material}').",
            "Укажите марку материала по ГОСТ (например: 'Сталь 45 ГОСТ 1050-2013').",
            plan_id,
            form_type="МК",
            evidence={"material": plan.material},
        ))

    if not plan.blank_type:
        issues.append(_make_check(
            "ESTD_MK_002",
            "Не указан вид заготовки (blank_type) — обязательное поле маршрутной карты.",
            "Укажите тип заготовки: прокат, поковка, штамповка, литьё или сварная конструкция.",
            plan_id,
            form_type="МК",
        ))

    machining_ops = [o for o in operations if o.operation_type in _MACHINING_OPS]
    if len(operations) == 0:
        issues.append(_make_check(
            "ESTD_MK_005",
            "Маршрутная карта не содержит ни одной операции.",
            "Добавьте хотя бы одну технологическую операцию.",
            plan_id,
            form_type="МК",
        ))
    elif len(machining_ops) == 0:
        issues.append(_make_check(
            "ESTD_MK_003",
            "В маршрутной карте нет ни одной операции механической обработки.",
            "Убедитесь, что маршрут включает необходимые операции обработки (токарная, фрезерная и т.д.).",
            plan_id,
            form_type="МК",
        ))

    has_qc = any(o.operation_type == "quality_control" for o in operations)
    if not has_qc and len(operations) > 1:
        issues.append(_make_check(
            "ESTD_MK_004",
            "Маршрутная карта не содержит операции технического контроля.",
            "Добавьте операцию 'Технический контроль' (код 0900) в конце маршрута.",
            plan_id,
            form_type="МК",
        ))

    for op in operations:
        if op.sequence_no <= 0 or op.sequence_no % 5 != 0:
            issues.append(_make_check(
                "ESTD_MK_005",
                f"Операция '{op.name}' имеет некратный-5 порядковый номер ({op.sequence_no}).",
                "По ГОСТ 3.1118 номера операций должны быть кратны 5 (005, 010, 015…).",
                plan_id,
                operation_id=op.id,
                form_type="МК",
                evidence={"sequence_no": op.sequence_no, "operation_name": op.name},
            ))

    return issues


# ── ГОСТ 3.1404 — Operational card (ОК) ──────────────────────────────────────

def _check_gost_3_1404(
    operations: list[ManufacturingOperation],
    plan_id: uuid.UUID,
) -> list[NormControlCheck]:
    issues: list[NormControlCheck] = []

    for op in operations:
        if op.operation_type not in _MACHINING_OPS:
            continue

        if not op.machine_resource_id:
            issues.append(_make_check(
                "ESTD_OK_001",
                f"Операция '{op.name}' (сл. {op.sequence_no}): не назначено оборудование (machine_resource_id).",
                "Укажите станок из справочника оборудования для данной операции.",
                plan_id,
                operation_id=op.id,
                form_type="ОК",
                evidence={"operation_name": op.name, "sequence_no": op.sequence_no},
            ))

        if not op.transition_text or len(op.transition_text.strip()) < 10:
            issues.append(_make_check(
                "ESTD_OK_002",
                f"Операция '{op.name}' (сл. {op.sequence_no}): переходы не описаны (transition_text пустой).",
                "Заполните переходы операции в формате: '1. Установить и закрепить деталь. 2. Точить поверхность…'",
                plan_id,
                operation_id=op.id,
                form_type="ОК",
            ))

        if not op.cutting_parameters:
            issues.append(_make_check(
                "ESTD_OK_003",
                f"Операция '{op.name}' (сл. {op.sequence_no}): не указаны режимы резания (cutting_parameters).",
                "Заполните режимы резания: скорость резания (Vc), подача (fz), глубина (ap).",
                plan_id,
                operation_id=op.id,
                form_type="ОК",
            ))

        if not op.operation_code or not op.operation_code.isdigit() or len(op.operation_code) != 4:
            issues.append(_make_check(
                "ESTD_OK_005",
                f"Операция '{op.name}' (сл. {op.sequence_no}): код операции '{op.operation_code}' не соответствует "
                "четырёхзначному классификатору ГОСТ.",
                "Укажите корректный четырёхзначный код операции согласно классификатору ЕСКД/ГОСТ 3.1404 "
                "(например, 4110 — токарная, 4130 — фрезерная).",
                plan_id,
                operation_id=op.id,
                form_type="ОК",
                evidence={"operation_code": op.operation_code},
            ))

        if not op.control_requirements:
            issues.append(_make_check(
                "ESTD_OK_004",
                f"Операция '{op.name}' (сл. {op.sequence_no}): не указаны требования к контролю.",
                "Добавьте требования к контролю (измерительный инструмент, параметры контроля).",
                plan_id,
                operation_id=op.id,
                form_type="ОК",
            ))

    return issues


# ── ГОСТ 3.1107 — Surface roughness designation ────────────────────────────────

def _check_gost_3_1107(
    surface_specs: list[SurfaceMachiningSpec],
    plan_id: uuid.UUID,
) -> list[NormControlCheck]:
    issues: list[NormControlCheck] = []

    for spec in surface_specs:
        if spec.roughness_ra is not None:
            # Check it's a valid standard Ra value
            closest = min(_VALID_RA_VALUES, key=lambda v: abs(v - spec.roughness_ra))
            if abs(closest - spec.roughness_ra) > 0.01:
                issues.append(_make_check(
                    "ESTD_RA_001",
                    f"Поверхность (тип: {spec.surface_type}): Ra={spec.roughness_ra} не является "
                    f"стандартным значением по ГОСТ 2789. Ближайшее стандартное: Ra={closest}.",
                    f"Используйте стандартное значение Ra={closest} согласно ряду Ra по ГОСТ 2789-73.",
                    plan_id,
                    evidence={"ra_value": spec.roughness_ra, "nearest_standard": closest},
                ))

        if spec.fit_system and spec.machining_stage == "rough":
            issues.append(_make_check(
                "ESTD_RA_002",
                f"Поверхность с посадкой '{spec.fit_system}' назначена на черновую обработку. "
                "Посадки обычно достигаются на чистовом или получистовом переходе.",
                "Проверьте стадию обработки: посадочные поверхности требуют финишной (чистовой) обработки.",
                plan_id,
                evidence={"fit_system": spec.fit_system, "machining_stage": spec.machining_stage},
            ))

    return issues


# ── ГОСТ 3.1127 — Time norms completeness ─────────────────────────────────────

def _check_gost_3_1127(
    operations: list[ManufacturingOperation],
    plan_id: uuid.UUID,
) -> list[NormControlCheck]:
    issues: list[NormControlCheck] = []

    for op in operations:
        if op.operation_type in ("blank_preparation", "quality_control"):
            if op.tsht_minutes is None:
                issues.append(_make_check(
                    "ESTD_NC_002",
                    f"Операция '{op.name}' (сл. {op.sequence_no}): не заполнено Тшт.",
                    "Заполните норму штучного времени (Тшт) даже для вспомогательных операций.",
                    plan_id,
                    operation_id=op.id,
                    form_type="МК",
                ))
            continue

        if op.operation_type not in _MACHINING_OPS:
            continue

        if op.tsht_k_minutes is None:
            issues.append(_make_check(
                "ESTD_NC_001",
                f"Операция '{op.name}' (сл. {op.sequence_no}): не заполнено Тшт-к (штучно-калькуляционное время).",
                "Рассчитайте Тшт-к = Тшт + Тпз/n, где n — размер партии.",
                plan_id,
                operation_id=op.id,
                form_type="МК",
                evidence={"operation_name": op.name, "tsht_k_minutes": None},
            ))

        if op.to_minutes is None:
            issues.append(_make_check(
                "ESTD_NC_003",
                f"Операция '{op.name}' (сл. {op.sequence_no}): не заполнено То (основное машинное время).",
                "Рассчитайте То по формуле: То = L / (n × Sо), где L — расчётная длина пути, n — частота вращения.",
                plan_id,
                operation_id=op.id,
                form_type="МК",
                evidence={"operation_name": op.name},
            ))

    return issues


# ── Main normcontrol runner ───────────────────────────────────────────────────

def run_normcontrol(plan_id: uuid.UUID, db: Session) -> dict[str, Any]:
    """Run all ГОСТ ЕСТД checks for a process plan. Saves NormControlCheck rows."""
    plan = db.query(ManufacturingProcessPlan).filter(
        ManufacturingProcessPlan.id == plan_id
    ).first()
    if not plan:
        raise ValueError(f"ProcessPlan {plan_id} not found")

    operations: list[ManufacturingOperation] = list(plan.operations)
    surface_specs: list[SurfaceMachiningSpec] = (
        db.query(SurfaceMachiningSpec)
        .filter(SurfaceMachiningSpec.process_plan_id == plan_id)
        .all()
    )

    # Remove previous checks from this run
    db.query(NormControlCheck).filter(
        NormControlCheck.process_plan_id == plan_id,
        NormControlCheck.created_by == "normcontrol_agent",
        NormControlCheck.status == "open",
    ).delete(synchronize_session=False)

    all_checks: list[NormControlCheck] = []

    all_checks.extend(_check_gost_3_1102(plan, plan_id))
    all_checks.extend(_check_gost_3_1118(plan, operations, plan_id))
    all_checks.extend(_check_gost_3_1404(operations, plan_id))
    all_checks.extend(_check_gost_3_1107(surface_specs, plan_id))
    all_checks.extend(_check_gost_3_1127(operations, plan_id))

    for check in all_checks:
        db.add(check)

    errors = [c for c in all_checks if c.severity == "error"]
    warnings = [c for c in all_checks if c.severity == "warning"]

    normcontrol_status = "passed" if len(errors) == 0 else "failed"
    plan.normcontrol_status = normcontrol_status
    plan.normcontrol_checked_at = datetime.now(timezone.utc)
    plan.normcontrol_checked_by = "normcontrol_agent"

    db.commit()

    logger.info(
        "normcontrol_completed",
        plan_id=str(plan_id),
        status=normcontrol_status,
        errors=len(errors),
        warnings=len(warnings),
        total=len(all_checks),
    )

    return {
        "status": normcontrol_status,
        "checks": [
            {
                "id": str(c.id),
                "check_code": c.check_code,
                "gost_code": c.gost_code,
                "clause": c.clause,
                "severity": c.severity,
                "message": c.message,
                "recommendation": c.recommendation,
                "auto_fixable": c.auto_fixable,
                "operation_id": str(c.operation_id) if c.operation_id else None,
                "form_type": c.form_type,
            }
            for c in all_checks
        ],
        "errors_count": len(errors),
        "warnings_count": len(warnings),
        "total_count": len(all_checks),
    }
