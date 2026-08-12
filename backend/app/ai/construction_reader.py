"""Ф5.2: read a 2D architectural floor plan into a ConstructionModel.

Same strategy the mechanical reader already proved (`cad_recognize/spec_vectorize.py`):
a VLM reads the SEMANTIC numbers actually dimensioned on the sheet (wall
endpoints from the drawing's own coordinate system, thickness/height
callouts), and deterministic code here builds the geometry -- never the
other way around. A wall that is not axis-aligned, or whose height cannot be
resolved from anything stated on the sheet, is excluded individually and
reported; the rest of the floor plan still builds. This mirrors
`ifc_reader.ifc_to_construction_model`'s own fail-closed report shape
(``skipped``/``blocked``/``blocked_reason``) so a caller already handling
that reader's output handles this one unchanged.

Known, stated limitation (v1): only ORTHOGONAL walls (parallel to the
sheet's own X or Y axis) are modeled -- curved or angled walls are silently
excluded, never approximated. No real architectural drawing corpus exists
in this repository at the time this reader was written, so unlike the
mechanical reader it has not been live-verified against a real floor plan;
only unit-tested against hand-built spec fixtures and a synthetic smoke
test. Treat its VLM-reading half as unproven until run against a real
sheet -- the deterministic geometry half (`construction_read_as_model`) is
tested and needs no drawing to trust.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.ai.cad_recognize.spec_vectorize import SpecEvidence

_AXIS_TOLERANCE_MM = 1.0


class WallRead(BaseModel):
    """One wall segment as stated on the sheet -- endpoints in the drawing's
    own coordinate system (e.g. from the axis grid), not pixels."""

    id: str = Field(min_length=1, max_length=160)
    name: str | None = Field(default=None, max_length=300)
    start_x_mm: float
    start_y_mm: float
    end_x_mm: float
    end_y_mm: float
    thickness_mm: float = Field(gt=0)
    # Optional: a floor plan often states one storey height in a note rather
    # than per wall. None here falls back to StoreyRead.default_wall_height_mm.
    height_mm: float | None = Field(default=None, gt=0)
    load_bearing: bool = False
    material: str | None = Field(default=None, max_length=300)
    evidence: list[SpecEvidence] = Field(default_factory=list)


class OpeningRead(BaseModel):
    """A door/window, positioned along its host wall by a read offset --
    never by pixel position on the sheet."""

    id: str = Field(min_length=1, max_length=160)
    host_wall_id: str = Field(min_length=1, max_length=160)
    kind: Literal["door", "window"]
    # Distance from the host wall's OWN start point to the opening's near edge.
    offset_mm: float = Field(ge=0)
    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)
    sill_height_mm: float = Field(default=0.0, ge=0)
    evidence: list[SpecEvidence] = Field(default_factory=list)


class StoreyRead(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    elevation_mm: float = 0.0
    default_wall_height_mm: float | None = Field(default=None, gt=0)
    evidence: list[SpecEvidence] = Field(default_factory=list)


class ConstructionSheetRead(BaseModel):
    """Single-storey v1: one floor-plan sheet, one storey. A multi-sheet set
    (one drawing per floor) reads one ConstructionSheetRead per sheet --
    combining them into one building is a caller concern, not this reader's."""

    storey: StoreyRead
    walls: list[WallRead] = Field(default_factory=list, max_length=2000)
    openings: list[OpeningRead] = Field(default_factory=list, max_length=5000)


_CONSTRUCTION_PROMPT = """Ты читаешь архитектурный чертёж — план этажа (поэтажный план).

Верни СТРОГО JSON без markdown-блоков и без пояснений — ТОЛЬКО то, что реально
подписано на листе (координаты и размеры — из размерных цепочек и координатной
сетки осей, не оценка на глаз):

{
  "storey": {
    "name": "1 этаж",
    "elevation_mm": 0,
    "default_wall_height_mm": 3000
  },
  "walls": [
    {
      "id": "w1",
      "name": "стена по оси А",
      "start_x_mm": 0, "start_y_mm": 0,
      "end_x_mm": 6000, "end_y_mm": 0,
      "thickness_mm": 200,
      "height_mm": null,
      "load_bearing": true,
      "material": "кирпич"
    }
  ],
  "openings": [
    {
      "id": "o1",
      "host_wall_id": "w1",
      "kind": "door",
      "offset_mm": 1000,
      "width_mm": 900,
      "height_mm": 2100,
      "sill_height_mm": 0
    }
  ]
}

ВАЖНЫЕ ПРАВИЛА:
- x/y — миллиметры в системе координат самого листа (например, от пересечения
  крайних координационных осей здания), не пиксели изображения.
- Включай ТОЛЬКО прямые стены, идущие строго вдоль оси X или строго вдоль оси Y
  чертежа. Если стена идёт под углом — не включай её вовсе (не приближай к
  ближайшей прямой).
- thickness_mm — только если явно читается (по размеру или по числу осевых
  клеток известной ширины). default_wall_height_mm — только если высота этажа
  явно указана в примечании/разрезе на этом листе; если нигде не указана —
  верни null и оставь height_mm пустым у стен (пустая стена без известной
  высоты не должна получать выдуманное число).
- offset_mm проёма — расстояние от НАЧАЛА стены (start_x_mm/start_y_mm) до
  ближнего к началу края проёма, вдоль стены.
- kind — "door" или "window" (перегородку/нишу пропускай).
- Не добавляй ничего сверх подписанного текстом или размером на листе."""


def _wall_box(wall: WallRead, height_mm: float, elevation_mm: float) -> Any | None:
    from app.ai.construction_emg import ConstructionBox

    dx = wall.end_x_mm - wall.start_x_mm
    dy = wall.end_y_mm - wall.start_y_mm
    if abs(dx) <= _AXIS_TOLERANCE_MM and abs(dy) <= _AXIS_TOLERANCE_MM:
        return None  # zero-length
    if abs(dy) <= _AXIS_TOLERANCE_MM and abs(dx) > _AXIS_TOLERANCE_MM:
        x0, x1 = sorted((wall.start_x_mm, wall.end_x_mm))
        return ConstructionBox(
            x_mm=x0,
            y_mm=wall.start_y_mm - wall.thickness_mm / 2,
            z_mm=elevation_mm,
            width_mm=x1 - x0,
            depth_mm=wall.thickness_mm,
            height_mm=height_mm,
        )
    if abs(dx) <= _AXIS_TOLERANCE_MM and abs(dy) > _AXIS_TOLERANCE_MM:
        y0, y1 = sorted((wall.start_y_mm, wall.end_y_mm))
        return ConstructionBox(
            x_mm=wall.start_x_mm - wall.thickness_mm / 2,
            y_mm=y0,
            z_mm=elevation_mm,
            width_mm=wall.thickness_mm,
            depth_mm=y1 - y0,
            height_mm=height_mm,
        )
    return None  # not axis-aligned -- never approximated


def _opening_box(
    opening: OpeningRead, host: WallRead, host_box: Any, elevation_mm: float
) -> Any | None:
    from app.ai.construction_emg import ConstructionBox

    dx = host.end_x_mm - host.start_x_mm
    dy = host.end_y_mm - host.start_y_mm
    if abs(dy) <= _AXIS_TOLERANCE_MM and abs(dx) > _AXIS_TOLERANCE_MM:
        x0 = min(host.start_x_mm, host.end_x_mm) + opening.offset_mm
        return ConstructionBox(
            x_mm=x0,
            y_mm=host_box.y_mm,
            z_mm=elevation_mm + opening.sill_height_mm,
            width_mm=opening.width_mm,
            depth_mm=host_box.depth_mm,
            height_mm=opening.height_mm,
        )
    if abs(dx) <= _AXIS_TOLERANCE_MM and abs(dy) > _AXIS_TOLERANCE_MM:
        y0 = min(host.start_y_mm, host.end_y_mm) + opening.offset_mm
        return ConstructionBox(
            x_mm=host_box.x_mm,
            y_mm=y0,
            z_mm=elevation_mm + opening.sill_height_mm,
            width_mm=host_box.width_mm,
            depth_mm=opening.width_mm,
            height_mm=opening.height_mm,
        )
    return None


def construction_read_as_model(
    sheet: ConstructionSheetRead,
    *,
    site_name: str | None = None,
    building_name: str | None = None,
) -> tuple[Any | None, dict[str, Any]]:
    """Deterministic geometry from a read sheet. No VLM call here -- pure and
    unit-testable on its own, exactly like `_wall_box`/`_opening_box`."""
    from app.ai.construction_emg import (
        ConstructionModel,
        ConstructionStorey,
        OpeningElement,
        WallElement,
        _box_contains,
    )

    skipped: list[dict[str, Any]] = []
    storey_id = "storey-1"
    storey = ConstructionStorey(
        id=storey_id, name=sheet.storey.name, elevation_mm=sheet.storey.elevation_mm
    )

    walls_by_id: dict[str, WallRead] = {}
    wall_boxes: dict[str, Any] = {}
    wall_elements: list[WallElement] = []
    for wall in sheet.walls:
        height_mm = wall.height_mm or sheet.storey.default_wall_height_mm
        if height_mm is None:
            skipped.append({"id": wall.id, "kind": "wall", "reason": "no_height"})
            continue
        box = _wall_box(wall, height_mm, sheet.storey.elevation_mm)
        if box is None:
            skipped.append(
                {"id": wall.id, "kind": "wall", "reason": "not_orthogonal_or_zero_length"}
            )
            continue
        walls_by_id[wall.id] = wall
        wall_boxes[wall.id] = box
        wall_elements.append(
            WallElement(
                kind="wall",
                id=wall.id,
                name=wall.name or wall.id,
                storey_id=storey_id,
                box=box,
                material=wall.material,
                load_bearing=wall.load_bearing,
            )
        )

    opening_elements: list[OpeningElement] = []
    for opening in sheet.openings:
        host = walls_by_id.get(opening.host_wall_id)
        host_box = wall_boxes.get(opening.host_wall_id)
        if host is None or host_box is None:
            skipped.append(
                {"id": opening.id, "kind": "opening", "reason": "unknown_or_excluded_host"}
            )
            continue
        box = _opening_box(opening, host, host_box, sheet.storey.elevation_mm)
        if box is None or not _box_contains(host_box, box):
            skipped.append({"id": opening.id, "kind": "opening", "reason": "outside_host_bounds"})
            continue
        opening_elements.append(
            OpeningElement(
                kind="opening",
                id=opening.id,
                name=f"{opening.kind} {opening.id}",
                storey_id=storey_id,
                box=box,
                host_id=opening.host_wall_id,
            )
        )

    report: dict[str, Any] = {
        "walls_read": len(sheet.walls),
        "walls_built": len(wall_elements),
        "openings_read": len(sheet.openings),
        "openings_built": len(opening_elements),
        "skipped": skipped,
    }
    if not wall_elements:
        report["blocked"] = True
        report["blocked_reason"] = "no_orthogonal_walls_with_known_height"
        return None, report

    try:
        model = ConstructionModel(
            site_name=site_name or "Участок",
            building_name=building_name or sheet.storey.name,
            storeys=[storey],
            elements=[*wall_elements, *opening_elements],
        )
    except ValidationError as exc:
        report["blocked"] = True
        report["blocked_reason"] = "construction_model_validation_failed"
        report["validation_error"] = str(exc)
        return None, report
    return model, report


async def _read_sheet_via_vlm(
    image_bytes: bytes,
    *,
    router: Any | None,
    confidential: bool,
    allow_cloud: bool,
) -> ConstructionSheetRead | None:
    import base64

    from app.ai.cad_recognize.spec_vectorize import _parse_spec_json
    from app.ai.schemas import AIRequest, AITask, ChatMessage

    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        messages=[
            ChatMessage(role="system", content=_CONSTRUCTION_PROMPT),
            ChatMessage(role="user", content="Прочитай план этажа с этого листа."),
        ],
        images=[base64.b64encode(image_bytes).decode()],
        confidential=confidential,
        allow_cloud=allow_cloud,
    )
    try:
        response = await router.run(request)
    except Exception:  # noqa: BLE001 -- a router failure fails closed below
        return None
    parsed = _parse_spec_json(response.text or "")
    if not parsed:
        return None
    try:
        return ConstructionSheetRead.model_validate(parsed)
    except ValidationError:
        return None


async def read_construction_drawing(
    image_bytes: bytes,
    *,
    router: Any | None = None,
    confidential: bool = True,
    allow_cloud: bool = False,
    site_name: str | None = None,
    building_name: str | None = None,
) -> tuple[Any | None, dict[str, Any]]:
    """Ф5.2 public entry point: image bytes -> (ConstructionModel | None, report).

    ``report["read_failed"]`` is set (model always None) when the VLM call or
    JSON/schema validation itself failed -- distinct from a successful read
    that simply built nothing (``report["blocked"]``), which still carries
    the fuller `construction_read_as_model` report shape.
    """
    sheet = await _read_sheet_via_vlm(
        image_bytes, router=router, confidential=confidential, allow_cloud=allow_cloud
    )
    if sheet is None:
        return None, {"read_failed": True}
    return construction_read_as_model(sheet, site_name=site_name, building_name=building_name)
