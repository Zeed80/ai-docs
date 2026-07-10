"""Tokenized sequence form of the CAD IR geometry (Drawing2CAD/DeepCAD style).

This is the I/O contract of the neural vectorizer: the model consumes a
raster and emits command rows; training pairs are produced by encoding IR
built from synthetic/ground-truth drawings. The mapping is deterministic in
both directions so the dataset generator, the inference container and the
backend never disagree on the vocabulary.

Row layout (``N_PARAMS`` float slots after the command index; unused = -1):

    [CMD, a, b, c, d, e, line_class_idx, width_idx]

Coordinates are normalized to [0, 1] by the source image width/height,
radii by max(w, h), angles by 360°. Text content and dimension labels are
out-of-band (OCR/VLM path) — only their anchors would be sequence-encodable
and are intentionally excluded; the sequence covers pure geometry:
segments, arcs, circles, polylines and hatch boundaries.
"""

from __future__ import annotations

from app.ai.cad_ir.schema import (
    Arc,
    CadIR,
    Circle,
    Entity,
    HatchRegion,
    Point,
    Polyline,
    Segment,
    SourceInfo,
)

COMMANDS = ("EOS", "SEG", "ARC", "CIR", "PLN", "PT", "HAT")
_CMD_IDX = {name: i for i, name in enumerate(COMMANDS)}
N_PARAMS = 7
_UNUSED = -1.0

_LINE_CLASSES = ("contour", "axis", "dim", "hatch", "hidden", "thin")
_WIDTH_CLASSES = ("main", "thin")


def _row(cmd: str, params: list[float], line_class: str, width_class: str) -> list[float]:
    padded = params + [_UNUSED] * (N_PARAMS - 2 - len(params))
    return [
        float(_CMD_IDX[cmd]),
        *padded,
        float(_LINE_CLASSES.index(line_class)),
        float(_WIDTH_CLASSES.index(width_class)),
    ]


def encode(ir: CadIR) -> list[list[float]]:
    """IR geometry → command rows (terminated by EOS)."""
    w = float(ir.source.image_width)
    h = float(ir.source.image_height)
    r_norm = max(w, h)
    rows: list[list[float]] = []
    for entity in ir.entities:
        if isinstance(entity, Segment):
            rows.append(
                _row(
                    "SEG",
                    [entity.p1.x / w, entity.p1.y / h, entity.p2.x / w, entity.p2.y / h],
                    entity.line_class,
                    entity.width_class,
                )
            )
        elif isinstance(entity, Arc):
            rows.append(
                _row(
                    "ARC",
                    [
                        entity.center.x / w,
                        entity.center.y / h,
                        entity.radius / r_norm,
                        entity.start_angle / 360.0,
                        entity.end_angle / 360.0,
                    ],
                    entity.line_class,
                    entity.width_class,
                )
            )
        elif isinstance(entity, Circle):
            rows.append(
                _row(
                    "CIR",
                    [entity.center.x / w, entity.center.y / h, entity.radius / r_norm],
                    entity.line_class,
                    entity.width_class,
                )
            )
        elif isinstance(entity, Polyline):
            rows.append(
                _row("PLN", [1.0 if entity.closed else 0.0], entity.line_class, entity.width_class)
            )
            for pt in entity.points:
                rows.append(_row("PT", [pt.x / w, pt.y / h], entity.line_class, entity.width_class))
        elif isinstance(entity, HatchRegion):
            rows.append(_row("HAT", [], entity.line_class, entity.width_class))
            for pt in entity.boundary:
                rows.append(_row("PT", [pt.x / w, pt.y / h], entity.line_class, entity.width_class))
        # TextEntity / DimensionEntity: out-of-band (see module docstring)
    rows.append(_row("EOS", [], "contour", "main"))
    return rows


def decode(rows: list[list[float]], source: SourceInfo, origin: str = "neural") -> list[Entity]:
    """Command rows → IR entities. Malformed trailing rows are dropped."""
    w = float(source.image_width)
    h = float(source.image_height)
    r_norm = max(w, h)
    entities: list[Entity] = []
    open_points: list[Point] | None = None
    open_meta: tuple[str, str, str, bool] | None = None  # kind, line_class, width, closed

    def _flush() -> None:
        nonlocal open_points, open_meta
        if open_points is None or open_meta is None:
            return
        kind, line_class, width_class, closed = open_meta
        if kind == "PLN" and len(open_points) >= 2:
            entities.append(
                Polyline(
                    points=open_points,
                    closed=closed,
                    line_class=line_class,
                    width_class=width_class,
                    origin=origin,
                )
            )
        elif kind == "HAT" and len(open_points) >= 3:
            entities.append(
                HatchRegion(
                    boundary=open_points,
                    line_class=line_class,
                    width_class=width_class,
                    origin=origin,
                )
            )
        open_points = None
        open_meta = None

    for row in rows:
        if len(row) != N_PARAMS + 1:
            continue
        cmd_idx = int(round(row[0]))
        if not 0 <= cmd_idx < len(COMMANDS):
            continue
        cmd = COMMANDS[cmd_idx]
        params = row[1 : N_PARAMS - 1]
        lc_idx = int(round(row[N_PARAMS - 1]))
        wc_idx = int(round(row[N_PARAMS]))
        line_class = _LINE_CLASSES[lc_idx] if 0 <= lc_idx < len(_LINE_CLASSES) else "contour"
        width_class = _WIDTH_CLASSES[wc_idx] if 0 <= wc_idx < len(_WIDTH_CLASSES) else "main"
        common = {"line_class": line_class, "width_class": width_class, "origin": origin}

        if cmd == "EOS":
            break
        if cmd == "PT":
            if open_points is not None:
                open_points.append(Point(x=params[0] * w, y=params[1] * h))
            continue
        _flush()
        if cmd == "SEG":
            entities.append(
                Segment(
                    p1=Point(x=params[0] * w, y=params[1] * h),
                    p2=Point(x=params[2] * w, y=params[3] * h),
                    **common,
                )
            )
        elif cmd == "ARC":
            entities.append(
                Arc(
                    center=Point(x=params[0] * w, y=params[1] * h),
                    radius=max(params[2] * r_norm, 1e-6),
                    start_angle=params[3] * 360.0,
                    end_angle=params[4] * 360.0,
                    **common,
                )
            )
        elif cmd == "CIR":
            entities.append(
                Circle(
                    center=Point(x=params[0] * w, y=params[1] * h),
                    radius=max(params[2] * r_norm, 1e-6),
                    **common,
                )
            )
        elif cmd in ("PLN", "HAT"):
            open_points = []
            open_meta = (cmd, line_class, width_class, cmd == "PLN" and params[0] >= 0.5)
    _flush()
    return entities
