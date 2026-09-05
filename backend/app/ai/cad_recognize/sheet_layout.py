"""Tell the drawing apart from the paperwork around it.

Every question the reader is asked is aimed at a crop, and every crop so far
was aimed by a rule of thumb: the stamp is "the bottom-right corner", the main
view is "the largest connected blob that is not the frame". Those rules are
wrong often enough to matter. On the spindle sheet the largest connected blob
is the longitudinal view welded to its own dimension lines and to the notes
column beside it, so a question meant for one view arrives carrying three
section views, a detail and a column of technical requirements.

This module measures the furniture instead of assuming it: the ГОСТ 2.301
frame, the ГОСТ 2.104 title block, the technical-requirements column, and
what is left over — the region where the views actually live, and the views
themselves.

Deliberately deterministic and model-free. A layout that depends on a VLM
would inherit exactly the uncertainty the crops exist to reduce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# The frame is a rectangle covering most of the sheet; below this it is some
# other box (a view border, a table) and must not be mistaken for the frame.
_FRAME_MIN_AREA_FRACTION = 0.35
# ГОСТ 2.104 form 1 is 185x55 mm. As a fraction of an A3 sheet that is roughly
# a quarter of the width and a tenth of the height; the search box is generous
# because the measured extent, not this box, is what gets reported.
_STAMP_SEARCH_W = 0.40
_STAMP_SEARCH_H = 0.28
_STAMP_MIN_INK = 0.02
# A view must occupy a real part of the sheet — smaller clusters are callouts,
# arrowheads and stray marks.
_VIEW_MIN_AREA_FRACTION = 0.004
_VIEW_MERGE_GAP_PX = 24


@dataclass
class Box:
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return max(0, self.x1 - self.x0)

    @property
    def height(self) -> int:
        return max(0, self.y1 - self.y0)

    @property
    def area(self) -> int:
        return self.width * self.height

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x0, self.y0, self.x1, self.y1)

    def to_dict(self) -> dict[str, int]:
        return {"x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1}


@dataclass
class SheetLayout:
    width: int
    height: int
    frame: Box | None = None
    title_block: Box | None = None
    notes_column: Box | None = None
    views: list[Box] = field(default_factory=list)
    # Where the drawing actually is, once the paperwork is taken out. This is
    # the part that crops need and the part that is reliable — splitting that
    # region into individual views is NOT solved (dimension lines physically
    # connect a view to its neighbours, so connected-ink clustering returns
    # the whole sheet as one blob on a dense drawing).
    views_region: Box | None = None

    @property
    def drawing_area(self) -> Box:
        """Where views may live: inside the frame, minus stamp and notes."""
        return self.views_region or self.frame or Box(0, 0, self.width, self.height)

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "frame": self.frame.to_dict() if self.frame else None,
            "title_block": self.title_block.to_dict() if self.title_block else None,
            "notes_column": self.notes_column.to_dict() if self.notes_column else None,
            "views_region": self.views_region.to_dict() if self.views_region else None,
            "views": [box.to_dict() for box in self.views],
        }


def _ink_mask(image) -> Any:
    import numpy as np

    grayscale = np.asarray(image.convert("L"))
    return (grayscale < 200).astype("uint8")


def _detect_frame(ink, width: int, height: int) -> Box | None:
    """The ГОСТ 2.301 frame: the dominant near-full-page rectangle."""
    import cv2

    contours, _hierarchy = cv2.findContours(ink, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best: Box | None = None
    best_area = -1.0
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < _FRAME_MIN_AREA_FRACTION * width * height:
            continue
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approx) != 4 or area <= best_area:
            continue
        x, y, box_w, box_h = cv2.boundingRect(approx)
        best_area = area
        best = Box(x, y, x + box_w, y + box_h)
    return best


def _detect_title_block(ink, frame: Box) -> Box | None:
    """The stamp, measured rather than assumed.

    The corner heuristic reports a fixed fraction of the sheet whatever the
    stamp's real size, which then either clips the stamp or drags a view into
    the crop. The stamp is a grid, so its own long rulings give its extent:
    inside the bottom-right search area, the topmost full-width horizontal
    ruling and the leftmost full-height vertical one are its edges.
    """
    import cv2
    import numpy as np

    x0 = int(frame.x1 - frame.width * _STAMP_SEARCH_W)
    y0 = int(frame.y1 - frame.height * _STAMP_SEARCH_H)
    region = ink[y0 : frame.y1, x0 : frame.x1]
    if region.size == 0 or float(region.mean()) < _STAMP_MIN_INK:
        return None

    region_h, region_w = region.shape[:2]
    # Rulings: runs of ink spanning most of the region in one direction.
    horizontal = cv2.morphologyEx(
        region,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(8, region_w // 3), 1)),
    )
    vertical = cv2.morphologyEx(
        region,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(8, region_h // 3))),
    )
    rows = np.nonzero(horizontal.any(axis=1))[0]
    cols = np.nonzero(vertical.any(axis=0))[0]
    if rows.size == 0 or cols.size == 0:
        return None
    top = int(rows.min())
    left = int(cols.min())
    box = Box(x0 + left, y0 + top, frame.x1, frame.y1)

    # Rulings are how a ГОСТ 2.104 stamp is drawn, but not every sheet that
    # reaches us is drawn that way: a rendered or exported sheet may carry the
    # stamp as a plain filled panel with no internal grid, and then the ruling
    # search returns a sliver at the far edge. Fall back to the extent of the
    # ink actually sitting in that corner.
    if box.width < frame.width * 0.05 or box.height < frame.height * 0.02:
        ys, xs = np.nonzero(region)
        if ys.size == 0:
            return None
        box = Box(x0 + int(xs.min()), y0 + int(ys.min()), frame.x1, frame.y1)

    # A stamp that would swallow half the sheet is a misdetection (a view's own
    # border can look like a ruling); report nothing rather than a wrong box.
    if box.width > frame.width * 0.6 or box.height > frame.height * 0.5:
        return None
    return box


def _text_row_blocks(image, region: Box) -> list[Box]:
    """Blocks of lettering inside ``region``, as merged glyph rows."""
    import cv2
    import numpy as np

    from app.ai.text_preserve import isolate_glyphs

    crop = image.crop(region.as_tuple())
    glyphs = np.asarray(isolate_glyphs(crop).convert("L")) < 128
    if not glyphs.any():
        return []
    # Merge glyphs into words, words into lines, lines into a block.
    merged = cv2.dilate(
        glyphs.astype("uint8"),
        cv2.getStructuringElement(cv2.MORPH_RECT, (25, 9)),
    )
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(merged, 8)
    blocks = []
    for index in range(1, count):
        x, y, box_w, box_h, area = stats[index]
        if area < 400:
            continue
        blocks.append(
            Box(region.x0 + x, region.y0 + y, region.x0 + x + box_w, region.y0 + y + box_h)
        )
    return blocks


def _stroke_density(ink, box: Box, min_run_px: int) -> float:
    """Fraction of ``box`` covered by strokes longer than ``min_run_px``.

    What separates the requirements column from a view is not where it sits —
    on the spindle sheet it is left of the stamp, not above it, and searching
    "above the stamp" landed the box in the middle of the drawing. It is that
    the column is lettering and NOTHING ELSE: no contour, no dimension line,
    no leader. So the test is for long strokes, not for position.
    """
    import cv2

    region = ink[box.y0 : box.y1, box.x0 : box.x1]
    if region.size == 0:
        return 0.0
    horizontal = cv2.morphologyEx(
        region,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (min_run_px, 1)),
    )
    vertical = cv2.morphologyEx(
        region,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, min_run_px)),
    )
    return float(((horizontal | vertical) > 0).mean())


def _detect_notes_column(image, ink, frame: Box, title_block: Box | None) -> Box | None:
    """The technical-requirements block (ГОСТ 2.316): lettering, no geometry."""
    search = Box(frame.x0, frame.y0, frame.x1, title_block.y0 if title_block else frame.y1)
    if search.height < frame.height * 0.1:
        return None
    min_run = max(40, int(min(frame.width, frame.height) * 0.06))
    candidates = []
    for block in _text_row_blocks(image, search):
        # Height is checked on the MERGED block below, never here: a single
        # line of the requirements is one glyph tall, and screening lines by
        # the height the whole column should have rejects every one of them.
        if block.width < frame.width * 0.1:
            continue
        if _stroke_density(ink, block, min_run) > 0.002:
            continue  # a view, or text sitting on top of one
        candidates.append(block)
    if not candidates:
        return None
    # The requirements are a numbered list, and its lines are separate blocks —
    # reporting only the biggest returns two lines out of five. Grow the best
    # block by absorbing the stroke-free blocks stacked over the same columns.
    best = max(candidates, key=lambda block: block.area)
    x0, y0, x1, y1 = best.x0, best.y0, best.x1, best.y1
    for block in candidates:
        overlap = min(x1, block.x1) - max(x0, block.x0)
        if overlap <= 0 or overlap < min(best.width, block.width) * 0.4:
            continue
        gap = max(y0 - block.y1, block.y0 - y1)
        if gap > best.height * 1.5:
            continue
        x0, y0 = min(x0, block.x0), min(y0, block.y0)
        x1, y1 = max(x1, block.x1), max(y1, block.y1)
    merged = Box(x0, y0, x1, y1)
    # One stray line of lettering is a label, not the requirements column.
    if merged.height < frame.height * 0.04:
        return None
    return merged


def _detect_views(ink, frame: Box, exclude: list[Box]) -> list[Box]:
    """Ink clusters left in the drawing area once the furniture is removed."""
    import cv2

    work = ink.copy()
    work[: frame.y0, :] = 0
    work[frame.y1 :, :] = 0
    work[:, : frame.x0] = 0
    work[:, frame.x1 :] = 0
    # The frame's own rulings connect everything to everything.
    border = max(2, min(frame.width, frame.height) // 200)
    work[frame.y0 : frame.y0 + border, :] = 0
    work[frame.y1 - border : frame.y1, :] = 0
    work[:, frame.x0 : frame.x0 + border] = 0
    work[:, frame.x1 - border : frame.x1] = 0
    for box in exclude:
        if box is not None:
            work[box.y0 : box.y1, box.x0 : box.x1] = 0

    grouped = cv2.dilate(
        work,
        cv2.getStructuringElement(cv2.MORPH_RECT, (_VIEW_MERGE_GAP_PX, _VIEW_MERGE_GAP_PX)),
    )
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(grouped, 8)
    sheet_area = float(frame.area) or 1.0
    views = []
    for index in range(1, count):
        x, y, box_w, box_h, _area = stats[index]
        if (box_w * box_h) / sheet_area < _VIEW_MIN_AREA_FRACTION:
            continue
        views.append(Box(int(x), int(y), int(x + box_w), int(y + box_h)))
    views.sort(key=lambda box: -box.area)
    return views


def detect_sheet_layout(image) -> SheetLayout:
    """Measure a sheet's furniture and the regions its views occupy."""
    width, height = image.size
    ink = _ink_mask(image)
    frame = _detect_frame(ink, width, height) or Box(0, 0, width, height)
    title_block = _detect_title_block(ink, frame)
    notes_column = _detect_notes_column(image, ink, frame, title_block)
    views = _detect_views(ink, frame, [title_block, notes_column])
    views_region = None
    if views:
        views_region = Box(
            min(box.x0 for box in views),
            min(box.y0 for box in views),
            max(box.x1 for box in views),
            max(box.y1 for box in views),
        )
    layout = SheetLayout(
        width=width,
        height=height,
        frame=frame,
        title_block=title_block,
        notes_column=notes_column,
        views=views,
        views_region=views_region,
    )
    logger.info(
        "cad_sheet_layout",
        frame=frame.to_dict() if frame else None,
        title_block=bool(title_block),
        notes_column=bool(notes_column),
        views=len(views),
    )
    return layout
