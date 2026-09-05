"""Score how completely a sheet description reproduces the sheet it came from.

Every accuracy metric this project has used so far needed ground truth — a
human writing down what the drawing says — and that is exactly what there is
almost none of. This one needs none. The claim under test is "the description
is complete enough to redraw the original", so the test is: redraw it and
compare with the original ink.

Two numbers, both necessary and neither sufficient alone:

  recall     how much of the sheet's ink the description accounts for
             (1.0 - recall is what a reconstruction would silently LOSE)
  precision  how much of what it draws is really on the sheet
             (1.0 - precision is what a reconstruction would INVENT)

An aggregate is nearly useless for fixing anything, so the loss is also
localized: the sheet is tiled and each tile scored, and a diff image is
written with missed ink in red and invented ink in blue. That picture is the
working instrument — it says WHERE the description fails, which no single
number can.

    python backend/scripts/eval_reconstruction.py \
        --image example-drawings/detal_126.png --out test-results/recon
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

TILES_X = 8
TILES_Y = 6


def _diff_image(ink_bool, drawn, covered_grown, ink_grown):
    """Grey original, red where ink is unaccounted for, blue where ink was invented."""
    import numpy as np

    h, w = ink_bool.shape[:2]
    canvas = np.full((h, w, 3), 255, dtype=np.uint8)
    canvas[ink_bool] = (185, 185, 185)
    missed = ink_bool & ~covered_grown
    invented = drawn & ~ink_grown
    canvas[missed] = (0, 0, 220)  # BGR red
    canvas[invented] = (220, 60, 0)  # BGR blue
    return canvas, missed, invented


def _tile_report(ink_bool, missed, invented):
    """Per-tile recall, so the loss has an address instead of an average."""

    h, w = ink_bool.shape[:2]
    rows = []
    for ty in range(TILES_Y):
        y0, y1 = ty * h // TILES_Y, (ty + 1) * h // TILES_Y
        row = []
        for tx in range(TILES_X):
            x0, x1 = tx * w // TILES_X, (tx + 1) * w // TILES_X
            total = int(ink_bool[y0:y1, x0:x1].sum())
            if total < 50:
                row.append(None)
                continue
            lost = int(missed[y0:y1, x0:x1].sum())
            row.append(round(1.0 - lost / total, 3))
        rows.append(row)
    return rows


def _print_tiles(rows) -> None:
    print("\nпокрытие по клеткам листа (recall; '.' — клетка почти пустая):")
    for row in rows:
        print("  " + " ".join("  .  " if v is None else f"{v:5.3f}" for v in row))


def run(image: pathlib.Path, out: pathlib.Path) -> int:
    import cv2
    import numpy as np

    from app.ai.cad_ir.png_render import rasterize_entities
    from app.ai.cad_recognize.cv import CvRecognizer
    from app.ai.cad_recognize.topology import consolidate_entities
    from app.ai.drawing_vectorize import _coverage_dilate_px
    from app.tasks.cad_trace import _binarize

    ink, w, h = _binarize(image.read_bytes())
    ink_bool = np.asarray(ink) > 0

    output = CvRecognizer().recognize(ink)
    if output is None:
        print("векторизатор отказался от листа — воспроизвести нечего")
        return 1
    entities, stats = consolidate_entities(output.entities)

    drawn = rasterize_entities(entities, w, h, output.thin_px, output.thick_px) < 128
    keep = output.keep_raster
    covered = drawn if keep is None else (drawn | np.asarray(keep).astype(bool))

    k = 2 * _coverage_dilate_px(h, w) + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    covered_grown = cv2.dilate(covered.astype(np.uint8), kernel) > 0
    ink_grown = cv2.dilate(ink_bool.astype(np.uint8), kernel) > 0

    ink_total = int(ink_bool.sum())
    recall = float((covered_grown & ink_bool).sum()) / max(1, ink_total)
    precision = float((ink_grown & drawn).sum()) / max(1, int(drawn.sum()))

    canvas, missed, invented = _diff_image(ink_bool, drawn, covered_grown, ink_grown)
    tiles = _tile_report(ink_bool, missed, invented)

    out.mkdir(parents=True, exist_ok=True)
    diff_path = out / f"{image.stem}_diff.png"
    cv2.imwrite(str(diff_path), canvas)
    report = {
        "sheet": image.name,
        "size": [w, h],
        "entities": len(entities),
        "recall": round(recall, 4),
        "precision": round(precision, 4),
        "ink_px": ink_total,
        "missed_px": int(missed.sum()),
        "invented_px": int(invented.sum()),
        "tiles": tiles,
        "consolidation": stats,
    }
    (out / f"{image.stem}_recon.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"{image.name}: {w}x{h}, сущностей {len(entities)}")
    print(
        f"  recall    {recall:.4f}  — не воспроизведено {int(missed.sum())} из {ink_total} px чернил"
    )
    print(f"  precision {precision:.4f}  — дорисовано лишнего {int(invented.sum())} px")
    _print_tiles(tiles)
    print(f"\nкартинка расхождений: {diff_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", default="test-results/recon")
    args = parser.parse_args()
    return run(pathlib.Path(args.image), pathlib.Path(args.out))


if __name__ == "__main__":
    raise SystemExit(main())
