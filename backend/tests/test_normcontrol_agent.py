"""Tests for normcontrol_agent.py — one test per check_code ESTD_*."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.normcontrol_agent import (
    _check_gost_3_1102,
    _check_gost_3_1107,
    _check_gost_3_1118,
    _check_gost_3_1127,
    _check_gost_3_1404,
)


def _plan(**kwargs):
    defaults = dict(
        id=uuid.uuid4(),
        product_name="Вал ступенчатый",
        product_code="75.ХХХХ.001",
        version="1.0",
        standard_system="ЕСТД",
        material="Ст.45",
        blank_type="прокат",
        tp_type="единичный",
        normcontrol_status="not_checked",
    )
    defaults.update(kwargs)
    m = MagicMock(**defaults)
    # Make attribute access work directly
    for k, v in defaults.items():
        setattr(m, k, v)
    return m


def _operation(**kwargs):
    defaults = dict(
        id=uuid.uuid4(),
        sequence_no=5,
        name="Токарная черновая",
        operation_type="turning",
        operation_code="4110",
        gost_operation_code="4110",
        machine_resource_id=str(uuid.uuid4()),
        transition_text="1. Установить деталь. 2. Точить Ø50.",
        cutting_parameters={"vc_m_min": 120, "n_rpm": 764, "ap_mm": 2.0},
        control_requirements="Проверить Ø50 h8",
        tsht_k_minutes=5.5,
        to_minutes=3.0,
    )
    defaults.update(kwargs)
    m = MagicMock(**defaults)
    for k, v in defaults.items():
        setattr(m, k, v)
    return m


def _surface(**kwargs):
    defaults = dict(
        id=uuid.uuid4(),
        roughness_ra=1.6,
        fit_system="H7",
        machining_stage="finish",
    )
    defaults.update(kwargs)
    m = MagicMock(**defaults)
    for k, v in defaults.items():
        setattr(m, k, v)
    return m


# ── ГОСТ 3.1102 ────────────────────────────────────────────────────────────────

def _codes(checks) -> list[str]:
    return [c.check_code for c in checks]


def test_estd_gen_001_passes_when_code_valid():
    plan = _plan(product_code="75.ХХХХ.001")
    checks = _check_gost_3_1102(plan, plan.id)
    assert "ESTD_GEN_001" not in _codes(checks)


def test_estd_gen_001_fails_when_code_short():
    plan = _plan(product_code="ДТ")
    checks = _check_gost_3_1102(plan, plan.id)
    assert "ESTD_GEN_001" in _codes(checks)


def test_estd_gen_002_fails_when_not_estd():
    plan = _plan(standard_system="ISO")
    checks = _check_gost_3_1102(plan, plan.id)
    assert "ESTD_GEN_002" in _codes(checks)


def test_estd_gen_003_fails_when_version_empty():
    plan = _plan(version="")
    checks = _check_gost_3_1102(plan, plan.id)
    assert "ESTD_GEN_003" in _codes(checks)


# ── ГОСТ 3.1118 ────────────────────────────────────────────────────────────────

def test_estd_mk_001_fails_when_no_material():
    plan = _plan(material=None)
    ops = [_operation(operation_type="turning")]
    checks = _check_gost_3_1118(plan, ops, plan.id)
    assert "ESTD_MK_001" in _codes(checks)


def test_estd_mk_002_fails_when_no_blank():
    plan = _plan(blank_type=None)
    ops = [_operation(operation_type="turning")]
    checks = _check_gost_3_1118(plan, ops, plan.id)
    assert "ESTD_MK_002" in _codes(checks)


def test_estd_mk_003_fails_when_no_machining_ops():
    plan = _plan()
    ops = [_operation(operation_type="quality_control", sequence_no=5)]
    checks = _check_gost_3_1118(plan, ops, plan.id)
    assert "ESTD_MK_003" in _codes(checks)


def test_estd_mk_004_fails_when_no_qc_op():
    plan = _plan()
    # Two ops needed: single op plan doesn't trigger MK_004
    ops = [
        _operation(operation_type="turning", sequence_no=5),
        _operation(operation_type="milling", sequence_no=10),
    ]
    checks = _check_gost_3_1118(plan, ops, plan.id)
    assert "ESTD_MK_004" in _codes(checks)


def test_estd_mk_005_warns_when_sequence_not_divisible_by_5():
    plan = _plan()
    ops = [
        _operation(operation_type="turning", sequence_no=3),
        _operation(operation_type="quality_control", sequence_no=7),
    ]
    checks = _check_gost_3_1118(plan, ops, plan.id)
    assert "ESTD_MK_005" in _codes(checks)


def test_mk_passes_on_valid_plan():
    plan = _plan()
    ops = [
        _operation(operation_type="turning", sequence_no=5),
        _operation(operation_type="quality_control", sequence_no=10),
    ]
    checks = _check_gost_3_1118(plan, ops, plan.id)
    errors = [c for c in checks if c.severity == "error"]
    assert len(errors) == 0


# ── ГОСТ 3.1404 ────────────────────────────────────────────────────────────────

def test_estd_ok_001_fails_when_no_machine():
    ops = [_operation(operation_type="turning", machine_resource_id=None)]
    checks = _check_gost_3_1404(ops, uuid.uuid4())
    assert "ESTD_OK_001" in _codes(checks)


def test_estd_ok_002_fails_when_no_transition():
    ops = [_operation(operation_type="turning", transition_text=None)]
    checks = _check_gost_3_1404(ops, uuid.uuid4())
    assert "ESTD_OK_002" in _codes(checks)


def test_estd_ok_003_fails_when_no_cutting_params():
    ops = [_operation(operation_type="milling", cutting_parameters=None)]
    checks = _check_gost_3_1404(ops, uuid.uuid4())
    assert "ESTD_OK_003" in _codes(checks)


def test_estd_ok_005_fails_when_op_code_not_4digit():
    ops = [_operation(operation_type="turning", operation_code="41")]
    checks = _check_gost_3_1404(ops, uuid.uuid4())
    assert "ESTD_OK_005" in _codes(checks)


def test_estd_ok_004_fails_when_no_control_requirements():
    ops = [_operation(operation_type="turning", control_requirements=None)]
    checks = _check_gost_3_1404(ops, uuid.uuid4())
    assert "ESTD_OK_004" in _codes(checks)


def test_ok_passes_on_valid_op():
    ops = [_operation(operation_type="quality_control")]  # non-machining skipped
    checks = _check_gost_3_1404(ops, uuid.uuid4())
    errors = [c for c in checks if c.severity == "error"]
    assert len(errors) == 0


# ── ГОСТ 3.1107 ────────────────────────────────────────────────────────────────

def test_estd_ra_001_fails_on_nonstandard_ra():
    surfaces = [_surface(roughness_ra=0.7)]  # Not in standard Ra set
    checks = _check_gost_3_1107(surfaces, uuid.uuid4())
    assert "ESTD_RA_001" in _codes(checks)


def test_estd_ra_001_passes_on_standard_ra():
    surfaces = [_surface(roughness_ra=0.8)]
    checks = _check_gost_3_1107(surfaces, uuid.uuid4())
    assert "ESTD_RA_001" not in _codes(checks)


def test_estd_ra_002_warns_finish_fit_with_rough_stage():
    surfaces = [_surface(roughness_ra=1.6, fit_system="H7", machining_stage="rough")]
    checks = _check_gost_3_1107(surfaces, uuid.uuid4())
    assert "ESTD_RA_002" in _codes(checks)


# ── ГОСТ 3.1127 ────────────────────────────────────────────────────────────────

def test_estd_nc_001_fails_when_tsht_k_missing():
    ops = [_operation(operation_type="turning", tsht_k_minutes=None)]
    checks = _check_gost_3_1127(ops, uuid.uuid4())
    assert "ESTD_NC_001" in _codes(checks)


def test_estd_nc_003_fails_when_to_missing():
    # to_minutes=None for machining op → ESTD_NC_003
    ops = [_operation(operation_type="turning", to_minutes=None)]
    checks = _check_gost_3_1127(ops, uuid.uuid4())
    assert "ESTD_NC_003" in _codes(checks)


def test_nc_passes_on_normed_op():
    ops = [
        _operation(operation_type="turning", tsht_k_minutes=5.5, to_minutes=3.0),
        _operation(operation_type="quality_control", tsht_k_minutes=None, to_minutes=None),
    ]
    checks = _check_gost_3_1127(ops, uuid.uuid4())
    errors = [c for c in checks if c.severity == "error"]
    assert len(errors) == 0


# ── Integration: all checks pass on valid plan ─────────────────────────────────

def test_all_checks_pass_on_valid_plan():
    plan = _plan()
    ops = [
        _operation(operation_type="turning", sequence_no=5),
        _operation(operation_type="milling", sequence_no=10),
        _operation(operation_type="quality_control", sequence_no=15),
    ]
    surfaces = [
        _surface(roughness_ra=1.6, fit_system="H7", machining_stage="finish"),
        _surface(roughness_ra=3.2, fit_system=None, machining_stage="rough"),
    ]
    all_checks = (
        _check_gost_3_1102(plan, plan.id)
        + _check_gost_3_1118(plan, ops, plan.id)
        + _check_gost_3_1404(ops, plan.id)
        + _check_gost_3_1107(surfaces, plan.id)
        + _check_gost_3_1127(ops, plan.id)
    )
    errors = [c for c in all_checks if c.severity == "error"]
    assert errors == [], f"Unexpected errors: {[c.message for c in errors]}"
