"""Agent-driven tech process (ТП) generator.

Orchestrates full pipeline: drawing feature extraction → surface analysis →
blank recommendation → operation drafting → equipment matching →
cutting parameters calculation → time norms.
"""

from __future__ import annotations

import math
import uuid
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.db.models import (
    BlankSpec,
    Drawing,
    DrawingFeature,
    DrawingTPLink,
    ManufacturingOperation,
    ManufacturingProcessPlan,
    ManufacturingResource,
    SurfaceMachiningSpec,
)

logger = structlog.get_logger()

# ── Cutting parameter tables ──────────────────────────────────────────────────

# {material_group: {operation_type: {tool_material: (Vc_m_min, fz_mm)}}}
_CUTTING_PARAMS: dict[str, dict[str, dict[str, tuple[float, float]]]] = {
    "steel_carbon": {
        "turning":   {"carbide": (220.0, 0.20), "hss": (60.0, 0.15)},
        "milling":   {"carbide": (180.0, 0.10), "hss": (40.0, 0.08)},
        "drilling":  {"carbide": (80.0,  0.18), "hss": (25.0, 0.12)},
        "grinding":  {"abrasive": (25.0, 0.005)},
        "reaming":   {"carbide": (12.0, 0.30),  "hss": (6.0, 0.20)},
        "boring":    {"carbide": (200.0, 0.10)},
        "default":   {"carbide": (150.0, 0.10), "hss": (35.0, 0.08)},
    },
    "steel_alloy": {
        "turning":   {"carbide": (160.0, 0.15), "hss": (40.0, 0.10)},
        "milling":   {"carbide": (120.0, 0.08), "hss": (28.0, 0.06)},
        "drilling":  {"carbide": (60.0,  0.15), "hss": (18.0, 0.10)},
        "grinding":  {"abrasive": (20.0, 0.004)},
        "reaming":   {"carbide": (10.0, 0.25),  "hss": (5.0, 0.15)},
        "boring":    {"carbide": (150.0, 0.08)},
        "default":   {"carbide": (110.0, 0.08), "hss": (25.0, 0.06)},
    },
    "cast_iron": {
        "turning":   {"carbide": (180.0, 0.25)},
        "milling":   {"carbide": (150.0, 0.12)},
        "drilling":  {"carbide": (70.0,  0.20)},
        "grinding":  {"abrasive": (22.0, 0.006)},
        "boring":    {"carbide": (160.0, 0.12)},
        "default":   {"carbide": (120.0, 0.12)},
    },
    "aluminum": {
        "turning":   {"carbide": (600.0, 0.25), "hss": (200.0, 0.20)},
        "milling":   {"carbide": (500.0, 0.15), "hss": (150.0, 0.12)},
        "drilling":  {"carbide": (200.0, 0.25), "hss": (80.0,  0.18)},
        "boring":    {"carbide": (500.0, 0.15)},
        "default":   {"carbide": (400.0, 0.15)},
    },
    "stainless": {
        "turning":   {"carbide": (140.0, 0.12)},
        "milling":   {"carbide": (100.0, 0.07)},
        "drilling":  {"carbide": (50.0,  0.12)},
        "grinding":  {"abrasive": (18.0, 0.003)},
        "boring":    {"carbide": (120.0, 0.08)},
        "default":   {"carbide": (90.0, 0.08)},
    },
}

# {machining_method: machine resource_type keywords}
_METHOD_MACHINE_TYPE: dict[str, list[str]] = {
    "turning":   ["токарный", "lathe", "turning"],
    "milling":   ["фрезерный", "milling", "machining center"],
    "drilling":  ["сверлильный", "drilling", "radial drill"],
    "grinding":  ["шлифовальный", "grinding", "круглошлифовальный"],
    "boring":    ["расточной", "boring", "jig boring"],
    "reaming":   ["сверлильный", "drilling"],
    "honing":    ["хонинговальный", "honing"],
    "broaching": ["протяжной", "broaching"],
}

# Roughness Ra → machining stage mapping
_RA_TO_STAGE: list[tuple[float, str]] = [
    (0.2,  "finish"),
    (0.8,  "finish"),
    (1.6,  "finish"),
    (3.2,  "semi-finish"),
    (6.3,  "semi-finish"),
    (12.5, "rough"),
]

# Operation type → ГОСТ operation code (three-digit ГОСТ classifier)
_GOST_OP_CODES: dict[str, str] = {
    "turning":           "4110",
    "milling":           "4130",
    "drilling":          "4140",
    "grinding":          "4150",
    "boring":            "4110",
    "reaming":           "4140",
    "honing":            "4150",
    "broaching":         "4160",
    "blank_preparation": "0210",
    "heat_treatment":    "0500",
    "quality_control":   "0900",
    "assembly":          "5100",
    "welding":           "1000",
    "other":             "0000",
}

# Auxiliary time reference (minutes) by operation type and machine class
_TV_TABLE: dict[str, float] = {
    "turning": 1.5, "milling": 2.0, "drilling": 1.2,
    "grinding": 2.5, "boring": 2.0, "reaming": 1.0,
    "honing": 3.0, "broaching": 1.5, "blank_preparation": 3.0,
    "quality_control": 2.0, "assembly": 5.0, "default": 2.0,
}

# Service + rest fraction (from machining time)
_K_OB = 0.06   # time for service (6% of To)
_K_OTD = 0.04  # time for rest (4% of To)

# Prep-finish time (Tpz) by operation type, minutes
_TPZ_TABLE: dict[str, float] = {
    "turning": 15.0, "milling": 20.0, "drilling": 10.0,
    "grinding": 25.0, "boring": 20.0, "default": 12.0,
}


# ── Material group detector ───────────────────────────────────────────────────

def _material_group(material: str) -> str:
    m = material.lower()
    if any(k in m for k in ["12х18", "нержав", "stainless", "321", "316", "304", "aisi"]):
        return "stainless"
    if any(k in m for k in ["алюм", "ад3", "ад0", "ад1", "амг", "амц", "дур", "ав", "al ", "al-", "aluminum", "aluminium", "д16", "а5", "а6", "а7"]):
        return "aluminum"
    if any(k in m for k in ["чугун", "сч", "кч", "вч", "cast iron", "grey iron"]):
        return "cast_iron"
    if any(k in m for k in ["легир", "хвг", "хвф", "40х", "30хгса", "18хгт", "alloy", "chrome"]):
        return "steel_alloy"
    return "steel_carbon"


# ── Surface type → machining method ──────────────────────────────────────────

def _surface_to_method(feature_type: str, nominal_mm: float | None, roughness_ra: float | None) -> str:
    if feature_type in ("hole", "pocket"):
        if nominal_mm and nominal_mm < 3:
            return "drilling"
        if roughness_ra and roughness_ra <= 1.6:
            return "boring"
        return "drilling"
    if feature_type == "thread":
        return "turning"
    if feature_type == "groove":
        return "turning"
    if feature_type == "contour":
        return "milling"
    if feature_type in ("surface", "flat"):
        if roughness_ra and roughness_ra <= 0.8:
            return "grinding"
        return "milling"
    if feature_type in ("boss", "external_cylindrical"):
        if roughness_ra and roughness_ra <= 0.8:
            return "grinding"
        return "turning"
    return "turning"


def _roughness_stage(ra: float | None) -> str:
    if ra is None:
        return "finish"
    for threshold, stage in _RA_TO_STAGE:
        if ra <= threshold:
            return stage
    return "rough"


# ── Drawing feature → SurfaceMachiningSpec candidates ────────────────────────

def extract_tp_features_from_drawing(
    drawing: Drawing,
    plan_id: uuid.UUID,
    db: Session,
) -> list[dict[str, Any]]:
    """Build SurfaceMachiningSpec candidates from DrawingFeature records."""
    specs: list[dict[str, Any]] = []

    for feature in drawing.features:
        nominal_mm: float | None = None
        upper_tol: float | None = None
        lower_tol: float | None = None
        fit_system: str | None = None
        roughness_ra: float | None = None

        for dim in feature.dimensions:
            if dim.dim_type.value in ("diameter", "linear", "radius"):
                nominal_mm = dim.nominal
                upper_tol = dim.upper_tol
                lower_tol = dim.lower_tol
                fit_system = dim.fit_system
                break

        for surf in feature.surfaces:
            if surf.roughness_type.value == "Ra":
                roughness_ra = surf.value
                break

        method = _surface_to_method(feature.feature_type.value, nominal_mm, roughness_ra)
        stage = _roughness_stage(roughness_ra)

        specs.append({
            "process_plan_id": str(plan_id),
            "drawing_feature_id": str(feature.id),
            "surface_type": feature.feature_type.value,
            "nominal_mm": nominal_mm,
            "upper_tol": upper_tol,
            "lower_tol": lower_tol,
            "roughness_ra": roughness_ra,
            "fit_system": fit_system,
            "machining_method": method,
            "machining_stage": stage,
            "confidence": feature.confidence,
        })

    return specs


def save_surface_specs(specs: list[dict[str, Any]], db: Session) -> list[SurfaceMachiningSpec]:
    rows = []
    for s in specs:
        row = SurfaceMachiningSpec(**{k: v for k, v in s.items()})
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


# ── Blank recommendation ──────────────────────────────────────────────────────

def recommend_blank(
    material: str,
    dims: dict[str, float],
    mass_part_kg: float | None,
    annual_volume: int = 1,
) -> dict[str, Any]:
    """Recommend blank type based on КИМ analysis."""
    d = dims.get("d_mm") or dims.get("a_mm") or 50.0
    l = dims.get("l_mm") or dims.get("h_mm") or 100.0

    # Estimate blank mass for rod stock
    material_lower = material.lower()
    if any(k in material_lower for k in ["алюм", "ад3", "ад0", "амг", "дур", "aluminum", "aluminium"]):
        density = 2.7e-6   # kg/mm³
    elif "латун" in material_lower or "бронз" in material_lower:
        density = 8.5e-6
    else:
        density = 7.85e-6  # steel default

    mass_blank = density * math.pi * (d / 2) ** 2 * l
    kim = (mass_part_kg / mass_blank) if mass_part_kg and mass_blank > 0 else 0.5

    if kim >= 0.7:
        blank_type = "прокат"
        std = "ГОСТ 2590-2006" if d <= 250 else "ГОСТ 7502-98"
        reasoning = f"КИМ={kim:.2f} ≥ 0.70 → прокат эффективен"
    elif kim >= 0.5:
        if annual_volume >= 100:
            blank_type = "штамповка"
            std = "ГОСТ 7505-89"
            reasoning = f"КИМ={kim:.2f} 0.50-0.70, серийное производство → штамповка"
        else:
            blank_type = "поковка"
            std = "ГОСТ 8479-70"
            reasoning = f"КИМ={kim:.2f} 0.50-0.70, единичное/мелкосерийное → поковка"
    else:
        if annual_volume >= 500:
            blank_type = "штамповка"
            std = "ГОСТ 7505-89"
            reasoning = f"КИМ={kim:.2f} < 0.50, крупная серия → штамповка выгодна"
        else:
            blank_type = "поковка"
            std = "ГОСТ 8479-70"
            reasoning = f"КИМ={kim:.2f} < 0.50 → поковка для сокращения припусков"

    dim_d = round(d * 1.1 + 5)  # blank diameter with allowance
    dim_l = round(l + 10)

    return {
        "blank_type": blank_type,
        "material_grade": material,
        "standard_gost": std,
        "dimensions": {"d_mm": dim_d, "l_mm": dim_l},
        "mass_blank_kg": round(mass_blank, 3),
        "mass_part_kg": mass_part_kg,
        "utilization_factor": round(kim, 3),
        "confidence": 0.75,
        "reasoning": reasoning,
    }


# ── Equipment selection ───────────────────────────────────────────────────────

def select_equipment(
    operation_type: str,
    workpiece_d_mm: float | None,
    tolerance_grade: int | None,
    db: Session,
    limit: int = 3,
) -> list[ManufacturingResource]:
    """Select candidate machines from resource catalog by operation type and tolerance."""
    keywords = _METHOD_MACHINE_TYPE.get(operation_type, ["machine"])

    query = db.query(ManufacturingResource).filter(
        ManufacturingResource.resource_type == "machine",
        ManufacturingResource.status == "active",
    )

    candidates = query.all()
    scored: list[tuple[int, ManufacturingResource]] = []
    for res in candidates:
        score = 0
        name_lower = res.name.lower()
        for kw in keywords:
            if kw.lower() in name_lower:
                score += 10
        if res.capabilities:
            caps = res.capabilities
            if workpiece_d_mm and "max_diameter_mm" in caps:
                if caps["max_diameter_mm"] >= workpiece_d_mm:
                    score += 5
            if tolerance_grade and "min_tolerance_grade" in caps:
                if caps["min_tolerance_grade"] <= tolerance_grade:
                    score += 5
        scored.append((score, res))

    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


# ── Cutting parameters ────────────────────────────────────────────────────────

def calculate_cutting_parameters(
    operation_type: str,
    material: str,
    nominal_mm: float | None,
    roughness_ra: float | None,
    tool_material: str = "carbide",
) -> dict[str, Any]:
    """Calculate Vc, n, feed, ap/ae and To estimate."""
    mat_group = _material_group(material)
    op_key = operation_type if operation_type in ("turning", "milling", "drilling", "grinding",
                                                   "boring", "reaming", "honing", "broaching") else "default"

    params = _CUTTING_PARAMS.get(mat_group, _CUTTING_PARAMS["steel_carbon"])
    op_params = params.get(op_key, params.get("default", {}))
    tool_key = tool_material if tool_material in op_params else next(iter(op_params), "carbide")
    vc, fz = op_params.get(tool_key, (120.0, 0.10))

    d = nominal_mm or 50.0

    # n = Vc * 1000 / (pi * d)
    n_rpm = round(vc * 1000 / (math.pi * d), 0)

    # ap: depth of cut based on machining stage
    if roughness_ra and roughness_ra <= 1.6:
        ap_mm = 0.3  # finish
    elif roughness_ra and roughness_ra <= 3.2:
        ap_mm = 0.8  # semi-finish
    else:
        ap_mm = 2.5  # rough

    feed_mm_min = round(fz * n_rpm, 1)

    # To = L / feed, estimate L = 1.5 * d
    l_mm = 1.5 * d
    to_min = round(l_mm / feed_mm_min, 3) if feed_mm_min > 0 else 0.5

    return {
        "vc_m_min": round(vc, 1),
        "n_rpm": int(n_rpm),
        "fz_mm": round(fz, 3),
        "feed_mm_min": feed_mm_min,
        "ap_mm": ap_mm,
        "to_min": to_min,
        "material_group": mat_group,
        "tool_material": tool_key,
    }


# ── Time norms ────────────────────────────────────────────────────────────────

def calculate_time_norms(
    operation_type: str,
    to_min: float,
    batch_size: int = 1,
) -> dict[str, float]:
    """Calculate full time norm set: To, Tv, Tob, Totd, Tsht, Tsht-k, Tpz."""
    tv = _TV_TABLE.get(operation_type, _TV_TABLE["default"])
    tob = round(to_min * _K_OB, 3)
    totd = round(to_min * _K_OTD, 3)
    tsht = round(to_min + tv + tob + totd, 3)
    tpz = _TPZ_TABLE.get(operation_type, _TPZ_TABLE["default"])
    tsht_k = round(tsht + tpz / max(batch_size, 1), 3)

    return {
        "to_minutes": round(to_min, 3),
        "tv_minutes": round(tv, 3),
        "tob_minutes": tob,
        "totd_minutes": totd,
        "tsht_minutes": tsht,
        "tsht_k_minutes": tsht_k,
        "tpz_minutes": round(tpz, 3),
        "machine_minutes": round(to_min, 3),
        "labor_minutes": round(tsht, 3),
        "setup_minutes": round(tpz, 3),
    }


# ── Operation drafting ────────────────────────────────────────────────────────

def _build_route_summary(ops: list[dict[str, Any]]) -> str:
    names = [f"{o['sequence_no']:03d} {o['name']}" for o in ops]
    return " → ".join(names)


def draft_operations_from_surfaces(
    surface_specs: list[dict[str, Any]],
    material: str,
    batch_size: int,
    plan_id: uuid.UUID,
    db: Session,
) -> list[ManufacturingOperation]:
    """Group surface specs by machining method and create operation rows."""
    # Group surfaces by machining method
    method_groups: dict[str, list[dict]] = {}
    for spec in surface_specs:
        m = spec["machining_method"]
        method_groups.setdefault(m, []).append(spec)

    operations = []
    sequence = 5

    # Always start with blank preparation
    blank_op = ManufacturingOperation(
        process_plan_id=plan_id,
        sequence_no=sequence,
        operation_code="0210",
        gost_operation_code="0210",
        name="Заготовительная",
        operation_type="blank_preparation",
        setup_description="Получить заготовку согласно спецификации материалов.",
        transition_text="1. Проверить размеры и внешний вид заготовки.",
        control_requirements="Контроль размеров заготовки по чертежу. Внешний осмотр.",
        safety_requirements="Работать в защитных перчатках при работе с металлическими заготовками.",
    )
    db.add(blank_op)
    operations.append(blank_op)
    sequence += 5

    # Machining operations (ordered: rough→semi-finish→finish)
    stage_order = {"rough": 0, "semi-finish": 1, "finish": 2}
    method_order = [
        "turning", "milling", "drilling", "boring", "reaming",
        "grinding", "honing", "broaching", "other",
    ]

    for method in method_order:
        if method not in method_groups:
            continue
        specs = sorted(method_groups[method], key=lambda s: stage_order.get(s.get("machining_stage", "finish"), 2))

        name_ru = {
            "turning": "Токарная", "milling": "Фрезерная", "drilling": "Сверлильная",
            "boring": "Расточная", "reaming": "Развёрточная", "grinding": "Шлифовальная",
            "honing": "Хонинговальная", "broaching": "Протяжная",
        }.get(method, method.capitalize())

        # Calculate nominal cutting params from first surface
        first = specs[0]
        cp = calculate_cutting_parameters(
            method, material,
            first.get("nominal_mm"),
            first.get("roughness_ra"),
        )
        norms = calculate_time_norms(method, cp["to_min"], batch_size)

        op = ManufacturingOperation(
            process_plan_id=plan_id,
            sequence_no=sequence,
            operation_code=_GOST_OP_CODES.get(method, "0000"),
            gost_operation_code=_GOST_OP_CODES.get(method, "0000"),
            name=name_ru,
            operation_type=method,
            setup_description=(
                f"Установить и закрепить деталь в приспособлении. "
                f"Обработать {len(specs)} поверхн."
            ),
            transition_text="\n".join(
                "{n}. Обработать поверхность {st}{dia}{ra}.".format(
                    n=i + 1,
                    st=s.get("surface_type", ""),
                    dia=f" Ø{s['nominal_mm']:.1f}" if s.get("nominal_mm") else "",
                    ra=f" Ra{s['roughness_ra']}" if s.get("roughness_ra") else "",
                )
                for i, s in enumerate(specs[:8])
            ),
            cutting_parameters=cp,
            control_requirements=(
                "Контроль размеров штангенциркулем, микрометром. "
                "Параметры шероховатости — профилометром."
            ),
            safety_requirements="СОЖ по норме. Защитные очки обязательны.",
            **{k: v for k, v in norms.items()},
        )
        db.add(op)
        operations.append(op)
        sequence += 5

    # Quality control at the end
    qc_op = ManufacturingOperation(
        process_plan_id=plan_id,
        sequence_no=sequence,
        operation_code="0900",
        gost_operation_code="0900",
        name="Технический контроль",
        operation_type="quality_control",
        setup_description="Предъявить деталь ОТК.",
        transition_text=(
            "1. Проверить все линейные размеры.\n"
            "2. Проверить параметры шероховатости.\n"
            "3. Проверить геометрические допуски.\n"
            "4. Оформить сопроводительную документацию."
        ),
        control_requirements="100% контроль размеров согласно чертежу. Запись в журнал ОТК.",
        safety_requirements="Работа с измерительным инструментом по инструкции.",
        to_minutes=0.0,
        tv_minutes=5.0,
        tsht_minutes=5.0,
        tsht_k_minutes=5.0,
    )
    db.add(qc_op)
    operations.append(qc_op)

    db.flush()
    return operations


# ── Link surfaces to operations ───────────────────────────────────────────────

def link_surfaces_to_operations(
    surface_rows: list[SurfaceMachiningSpec],
    operations: list[ManufacturingOperation],
    db: Session,
) -> None:
    """Assign operation_id to each surface spec based on machining method match."""
    op_by_method: dict[str, uuid.UUID] = {}
    for op in operations:
        if op.operation_type and op.operation_type not in ("blank_preparation", "quality_control"):
            op_by_method[op.operation_type] = op.id

    for spec in surface_rows:
        op_id = op_by_method.get(spec.machining_method)
        if op_id:
            spec.operation_id = op_id

    db.flush()


# ── Full generation pipeline ──────────────────────────────────────────────────

def generate_process_plan_from_drawing(
    drawing_id: uuid.UUID,
    plan_id: uuid.UUID,
    batch_size: int,
    db: Session,
) -> dict[str, Any]:
    """Full pipeline: drawing features → surfaces → blank → operations → equipment → norms."""
    drawing = db.query(Drawing).filter(Drawing.id == drawing_id).first()
    if not drawing:
        raise ValueError(f"Drawing {drawing_id} not found")

    plan = db.query(ManufacturingProcessPlan).filter(ManufacturingProcessPlan.id == plan_id).first()
    if not plan:
        raise ValueError(f"ProcessPlan {plan_id} not found")

    material = plan.material or (drawing.title_block or {}).get("material", "Сталь 45") or "Сталь 45"

    # 1. Extract surface specs from drawing features
    surface_dicts = extract_tp_features_from_drawing(drawing, plan_id, db)
    surface_rows = save_surface_specs(surface_dicts, db)

    # 2. Blank recommendation
    dims = {"d_mm": 50.0, "l_mm": 100.0}
    if drawing.title_block:
        mass = drawing.title_block.get("mass_kg")
    else:
        mass = None

    blank_data = recommend_blank(material, dims, mass, annual_volume=batch_size)
    blank_spec = BlankSpec(process_plan_id=plan_id, **blank_data)
    db.add(blank_spec)
    db.flush()

    # Update plan's blank_type from recommendation
    plan.blank_type = blank_data["blank_type"]

    # 3. Draft operations
    operations = draft_operations_from_surfaces(
        surface_dicts, material, batch_size, plan_id, db
    )

    # 4. Link surfaces to operations
    link_surfaces_to_operations(surface_rows, operations, db)

    # 5. Equipment matching — assign machines
    for op in operations:
        if op.operation_type in ("blank_preparation", "quality_control"):
            continue
        candidates = select_equipment(op.operation_type, None, None, db, limit=1)
        if candidates:
            op.machine_resource_id = candidates[0].id
    db.flush()

    # 6. Build DrawingTPLink
    link = DrawingTPLink(
        drawing_id=drawing_id,
        process_plan_id=plan_id,
        link_type="derived_from",
        surface_mapping={str(s.drawing_feature_id): str(s.operation_id) for s in surface_rows if s.operation_id},
    )
    db.add(link)

    # 7. Update plan totals
    plan.drawing_id = drawing_id
    machining_ops = [o for o in operations if o.tsht_k_minutes]
    plan.total_norm_minutes = round(sum(o.tsht_k_minutes for o in machining_ops if o.tsht_k_minutes), 2)
    plan.route_summary = _build_route_summary([
        {"sequence_no": o.sequence_no, "name": o.name} for o in operations
    ])

    db.commit()

    logger.info(
        "tp_generated",
        plan_id=str(plan_id),
        drawing_id=str(drawing_id),
        surfaces=len(surface_rows),
        operations=len(operations),
    )

    return {
        "plan_id": str(plan_id),
        "surfaces_count": len(surface_rows),
        "operations_count": len(operations),
        "blank_spec": blank_data,
        "total_norm_minutes": plan.total_norm_minutes,
    }
