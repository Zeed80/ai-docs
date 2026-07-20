"""DXF → CAD IR import (the exact inverse of ``dxf_render``).

Coordinate mapping mirrors the export: DXF drawing units are mm and y-up,
IR pixel space is y-down at ``px_per_mm`` resolution. Arc angles invert the
export rule (IR ``[a0, a1]`` → DXF ``[-a1, -a0]``), so a DXF arc
``[sa, ea]`` becomes IR ``[-ea, -sa]``.

Supported entities: LINE, CIRCLE, ARC, LWPOLYLINE/POLYLINE (vertices only —
bulges arrive as straight chords for now), TEXT/MTEXT, DIMENSION, HATCH and
INSERT blocks via ``virtual_entities`` (recursively). Common ЕСКД annotations
on DIM/DATUM/WELD layers are restored as structured entities. Everything
imported is human-authored CAD (``origin="human"``); layers map back through
``LINE_CLASS_LAYERS``.
"""

from __future__ import annotations

import math
import re

import structlog

from app.ai.cad_ir.schema import (
    LINE_CLASS_LAYERS,
    AnnotationEntity,
    Arc,
    CadIR,
    Circle,
    DimensionEntity,
    Entity,
    HatchRegion,
    Point,
    Polyline,
    Segment,
    SheetInfo,
    SourceInfo,
    TextEntity,
)

logger = structlog.get_logger()

_PX_PER_MM = 4.0
_MARGIN_MM = 10.0
# $INSUNITS → mm multiplier (0/unitless treated as mm, the DXF default here)
_UNIT_TO_MM = {0: 1.0, 1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0}

_LAYER_TO_LINE_CLASS = {v: k for k, v in LINE_CLASS_LAYERS.items()}
_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_THREAD = re.compile(r"^[MМ]\s*\d", re.IGNORECASE)
_ROUGHNESS = re.compile(r"^(RA|RZ)\s*(.+)$", re.IGNORECASE)
_TOLERANCE_GLYPHS = {
    "—": "straightness",
    "▱": "flatness",
    "○": "roundness",
    "⌭": "cylindricity",
    "⌒": "profile_line",
    "∥": "parallelism",
    "⊥": "perpendicularity",
    "∠": "angularity",
    "⊕": "position",
    "◎": "concentricity",
    "⌯": "symmetry",
    "↗": "runout",
}


class DxfImportError(ValueError):
    """The uploaded file is not a readable DXF."""


def _read_document(data: bytes):
    import io
    import tempfile

    import ezdxf
    from ezdxf import recover

    text = data.decode("utf-8", errors="replace")
    try:
        return ezdxf.read(io.StringIO(text))
    except Exception:  # noqa: BLE001 — retry via the tolerant recover loader
        pass
    try:
        with tempfile.NamedTemporaryFile(suffix=".dxf") as tmp:
            tmp.write(data)
            tmp.flush()
            doc, _auditor = recover.readfile(tmp.name)
            return doc
    except Exception as exc:  # noqa: BLE001
        raise DxfImportError(f"Не удалось прочитать DXF: {str(exc)[:200]}") from exc


def _line_class_for(dxf_entity) -> str:
    layer = str(getattr(dxf_entity.dxf, "layer", "") or "").upper()
    if layer in _LAYER_TO_LINE_CLASS:
        return _LAYER_TO_LINE_CLASS[layer]
    for marker, cls in (
        ("CENTER", "axis"), ("AXIS", "axis"), ("ОСЕВ", "axis"),
        ("HIDDEN", "hidden"), ("DASH", "hidden"),
        ("DIM", "dim"), ("РАЗМЕР", "dim"),
        ("HATCH", "hatch"), ("ШТРИХ", "hatch"),
        ("THIN", "thin"),
    ):
        if marker in layer:
            return cls
    return "contour"


def _width_class_for(dxf_entity, line_class: str) -> str:
    lw = int(getattr(dxf_entity.dxf, "lineweight", -1) or -1)
    if lw > 0:
        return "main" if lw >= 40 else "thin"
    return "main" if line_class == "contour" else "thin"


def _safe_extents(doc, msp):
    """Bounding box that survives broken anonymous block references."""
    from ezdxf import bbox
    from ezdxf.math import BoundingBox

    supported = {
        "LINE", "CIRCLE", "ARC", "LWPOLYLINE", "POLYLINE",
        "TEXT", "MTEXT", "HATCH",
    }
    result = BoundingBox()
    for entity in msp:
        kind = entity.dxftype()
        if kind == "DIMENSION":
            for name in ("defpoint", "defpoint2", "defpoint3", "defpoint4", "defpoint5"):
                point = getattr(entity.dxf, name, None)
                if point is not None and (
                    abs(float(point.x)) > 1e-12 or abs(float(point.y)) > 1e-12
                ):
                    result.extend([point])
            continue
        if kind == "INSERT" and entity.dxf.name not in doc.blocks:
            continue
        if kind not in supported and kind != "INSERT":
            continue
        try:
            entity_box = bbox.extents([entity], fast=True)
        except Exception:  # noqa: BLE001 — a corrupt entity must not hide valid geometry
            continue
        if entity_box.has_data:
            result.extend([entity_box.extmin, entity_box.extmax])
    return result


def _numeric_value(text: str, fallback: float | None = None) -> float | None:
    match = _NUMBER.search(text or "")
    if match:
        return float(match.group(0).replace(",", "."))
    return fallback


def _annotation_from_text(text: str, position: Point, height: float, layer: str):
    raw = text.strip()
    roughness = _ROUGHNESS.match(raw)
    common = {
        "position": position,
        "text": raw,
        "height": height,
        "origin": "human",
        "confidence": 1.0,
    }
    if roughness:
        return AnnotationEntity(
            kind="roughness",
            value=f"{roughness.group(1)} {roughness.group(2)}",
            **common,
        )
    if _THREAD.match(raw):
        return AnnotationEntity(kind="thread", value=raw, **common)
    if raw and raw[0] in _TOLERANCE_GLYPHS:
        parts = raw.split()
        return AnnotationEntity(
            kind="tolerance",
            symbol=_TOLERANCE_GLYPHS[parts[0]],
            value=parts[1] if len(parts) > 1 else None,
            datum_refs=parts[2:] if len(parts) > 2 else [],
            **common,
        )
    if ("DATUM" in layer or "БАЗ" in layer) and len(raw) == 1 and raw.isalpha():
        return AnnotationEntity(kind="datum", symbol=raw, **common)
    if "WELD" in layer or "СВАР" in layer:
        return AnnotationEntity(kind="weld", value=raw, **common)
    return None


def dxf_to_ir(data: bytes, px_per_mm: float = _PX_PER_MM) -> CadIR:
    """Parse DXF bytes into a CAD IR sheet sized to the drawing's extents."""
    doc = _read_document(data)
    msp = doc.modelspace()
    unit_mm = _UNIT_TO_MM.get(int(doc.header.get("$INSUNITS", 4) or 4), 1.0)

    # Broken DWG converters commonly leave INSERT/DIMENSION references to
    # anonymous blocks that do not exist. Unsupported/broken entities must
    # not prevent extents and all otherwise-valid CAD primitives from loading.
    extents = _safe_extents(doc, msp)
    if not extents.has_data:
        raise DxfImportError("DXF пуст: в modelspace нет поддерживаемых сущностей.")
    min_x, min_y = float(extents.extmin.x), float(extents.extmin.y)
    max_x, max_y = float(extents.extmax.x), float(extents.extmax.y)

    def px(x: float, y: float) -> Point:
        return Point(
            x=((x - min_x) * unit_mm + _MARGIN_MM) * px_per_mm,
            y=((max_y - y) * unit_mm + _MARGIN_MM) * px_per_mm,
        )

    def mm(value: float) -> float:
        return value * unit_mm * px_per_mm  # length in px

    entities: list[Entity] = []
    skipped: dict[str, int] = {}

    def _convert(e, depth: int = 0) -> None:
        kind = e.dxftype()
        line_class = _line_class_for(e)
        common = {
            "line_class": line_class,
            "width_class": _width_class_for(e, line_class),
            "origin": "human",
            "confidence": 1.0,
        }
        try:
            if kind == "LINE":
                entities.append(Segment(
                    p1=px(e.dxf.start.x, e.dxf.start.y),
                    p2=px(e.dxf.end.x, e.dxf.end.y),
                    **common,
                ))
            elif kind == "CIRCLE":
                entities.append(Circle(
                    center=px(e.dxf.center.x, e.dxf.center.y),
                    radius=mm(float(e.dxf.radius)),
                    **common,
                ))
            elif kind == "ARC":
                sa, ea = float(e.dxf.start_angle), float(e.dxf.end_angle)
                entities.append(Arc(
                    center=px(e.dxf.center.x, e.dxf.center.y),
                    radius=mm(float(e.dxf.radius)),
                    start_angle=-ea,
                    end_angle=-sa,
                    **common,
                ))
            elif kind in ("LWPOLYLINE", "POLYLINE"):
                if kind == "LWPOLYLINE":
                    pts = [px(p[0], p[1]) for p in e.get_points()]
                    closed = bool(e.closed)
                else:
                    pts = [px(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
                    closed = bool(e.is_closed)
                if len(pts) >= 2:
                    entities.append(Polyline(points=pts, closed=closed, **common))
            elif kind in ("TEXT", "MTEXT"):
                if kind == "TEXT":
                    pos = e.dxf.insert
                    text = str(e.dxf.text or "")
                    height = float(e.dxf.height or 3.5)
                    rotation = float(e.dxf.rotation or 0.0)
                else:
                    pos = e.dxf.insert
                    text = str(e.plain_text() or "")
                    height = float(e.dxf.char_height or 3.5)
                    rotation = float(e.dxf.rotation or 0.0)
                if text.strip():
                    position = px(pos.x, pos.y)
                    layer = str(getattr(e.dxf, "layer", "") or "").upper()
                    annotation = _annotation_from_text(
                        text, position, mm(height), layer
                    )
                    entities.append(
                        annotation
                        or TextEntity(
                            position=position,
                            text=text,
                            height=mm(height),
                            rotation=-rotation,
                            line_class="dim",
                            width_class="thin",
                            origin="human",
                            confidence=1.0,
                        )
                    )
            elif kind == "DIMENSION":
                dim_type = int(e.dxf.dimtype) & 0x0F
                label = str(e.dxf.text or "").strip()
                measured = float(e.get_measurement()) * unit_mm
                if label in ("", "<>"):
                    label = f"{measured:g}"
                if dim_type in (3, 4):
                    first, second = e.dxf.defpoint, e.dxf.defpoint4
                    dim_kind = "diameter" if dim_type == 3 else "radial"
                else:
                    first, second = e.dxf.defpoint2, e.dxf.defpoint3
                    dim_kind = "angular" if dim_type in (2, 5) else "linear"
                if first is not None and second is not None:
                    entities.append(DimensionEntity(
                        p1=px(first.x, first.y),
                        p2=px(second.x, second.y),
                        kind=dim_kind,
                        text=label,
                        value_mm=_numeric_value(label, measured),
                        line_class="dim",
                        width_class="thin",
                        origin="human",
                        confidence=1.0,
                    ))
            elif kind == "HATCH":
                from ezdxf import path as ezpath

                loops: list[tuple[bool, list[Point]]] = []
                for boundary in e.paths:
                    try:
                        flattened = list(
                            ezpath.from_hatch_boundary_path(
                                boundary,
                                ocs=e.ocs(),
                                elevation=float(e.dxf.elevation.z),
                            ).flattening(max(0.02 / unit_mm, 1e-5))
                        )
                    except Exception:  # noqa: BLE001
                        continue
                    points = [px(point.x, point.y) for point in flattened]
                    if len(points) >= 3:
                        external = bool(int(boundary.path_type_flags) & 1)
                        loops.append((external, points))
                outer = [points for external, points in loops if external]
                holes = [points for external, points in loops if not external]
                if not outer and loops:
                    outer = [loops[0][1]]
                    holes = [points for _, points in loops[1:]]
                for index, boundary in enumerate(outer):
                    entities.append(HatchRegion(
                        boundary=boundary,
                        holes=holes if index == 0 else [],
                        pattern=(
                            "solid"
                            if int(getattr(e.dxf, "solid_fill", 0) or 0)
                            else "ansi31"
                        ),
                        origin="human",
                        confidence=1.0,
                    ))
            elif kind == "INSERT":
                if depth >= 16:
                    raise ValueError("INSERT nesting exceeds 16 levels")
                for child in e.virtual_entities():
                    _convert(child, depth + 1)
            else:
                skipped[kind] = skipped.get(kind, 0) + 1
        except Exception as exc:  # noqa: BLE001 — one bad entity must not sink the import
            logger.warning("dxf_import_entity_failed", kind=kind, error=str(exc)[:120])
            skipped[kind] = skipped.get(kind, 0) + 1

    for e in msp:
        _convert(e)

    if not entities:
        raise DxfImportError(
            "DXF прочитан, но не содержит поддерживаемых сущностей "
            "(LINE/CIRCLE/ARC/POLYLINE/TEXT)."
        )

    w_mm = (max_x - min_x) * unit_mm + 2 * _MARGIN_MM
    h_mm = (max_y - min_y) * unit_mm + 2 * _MARGIN_MM
    ir = CadIR(
        source=SourceInfo(
            image_width=int(math.ceil(w_mm * px_per_mm)),
            image_height=int(math.ceil(h_mm * px_per_mm)),
            kind="import",
        ),
        scale=1.0 / px_per_mm,
        scale_source="manual",
        sheet=SheetInfo(width_mm=w_mm, height_mm=h_mm),
        entities=entities,
        recognizer_used="import",
    )
    if skipped:
        logger.info("dxf_import_skipped_kinds", skipped=skipped)
    return ir
