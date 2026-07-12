"""DXF → CAD IR import (the exact inverse of ``dxf_render``).

Coordinate mapping mirrors the export: DXF drawing units are mm and y-up,
IR pixel space is y-down at ``px_per_mm`` resolution. Arc angles invert the
export rule (IR ``[a0, a1]`` → DXF ``[-a1, -a0]``), so a DXF arc
``[sa, ea]`` becomes IR ``[-ea, -sa]``.

Supported entities: LINE, CIRCLE, ARC, LWPOLYLINE/POLYLINE (vertices only —
bulges arrive as straight chords for now), TEXT/MTEXT, and INSERT blocks via
``virtual_entities`` (recursively). Everything imported is human-authored
CAD (``origin="human"``); layers map back through ``LINE_CLASS_LAYERS``.
"""

from __future__ import annotations

import math

import structlog

from app.ai.cad_ir.schema import (
    LINE_CLASS_LAYERS,
    Arc,
    CadIR,
    Circle,
    Entity,
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


def dxf_to_ir(data: bytes, px_per_mm: float = _PX_PER_MM) -> CadIR:
    """Parse DXF bytes into a CAD IR sheet sized to the drawing's extents."""
    from ezdxf import bbox

    doc = _read_document(data)
    msp = doc.modelspace()
    unit_mm = _UNIT_TO_MM.get(int(doc.header.get("$INSUNITS", 4) or 4), 1.0)

    extents = bbox.extents(msp, fast=True)
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

    def _convert(e) -> None:
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
                    entities.append(TextEntity(
                        position=px(pos.x, pos.y),
                        text=text,
                        height=mm(height),
                        rotation=-rotation,
                        line_class="dim",
                        width_class="thin",
                        origin="human",
                        confidence=1.0,
                    ))
            elif kind == "INSERT":
                for child in e.virtual_entities():
                    _convert(child)
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
