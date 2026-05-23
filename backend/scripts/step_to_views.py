#!/usr/bin/env python3
"""FreeCAD headless STEP/IGES → orthographic view images.

Used as a subprocess fallback when pythonocc-core is not installed.

Usage:
    freecadcmd step_to_views.py <input_file> <output_dir>

Writes:
    <output_dir>/front.png
    <output_dir>/side.png
    <output_dir>/top.png
    <output_dir>/meta.json  — {bounding_box_mm, shape_class, product_names}

Called from step_extractor.py when pythonocc is unavailable.
FreeCAD (headless) must be installed: apt install freecad or conda install freecad.
"""

import json
import math
import os
import sys


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: freecadcmd step_to_views.py <input_file> <output_dir>", file=sys.stderr)
        sys.exit(1)

    input_path = sys.argv[1]
    output_dir = sys.argv[2]
    os.makedirs(output_dir, exist_ok=True)

    try:
        import FreeCAD  # type: ignore
        import Import  # type: ignore
        import Part  # type: ignore
        from FreeCAD import BoundBox  # type: ignore
    except ImportError:
        print("FreeCAD Python modules not available", file=sys.stderr)
        sys.exit(2)

    # Open file
    doc = FreeCAD.newDocument("step_import")
    try:
        Import.insert(input_path, doc.Name)
    except Exception as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        sys.exit(3)

    doc.recompute()

    # Collect all shapes
    shapes = []
    for obj in doc.Objects:
        if hasattr(obj, "Shape") and obj.Shape and not obj.Shape.isNull():
            shapes.append(obj.Shape)

    if not shapes:
        print("No shapes found", file=sys.stderr)
        sys.exit(4)

    # Compound bounding box
    bb = shapes[0].BoundBox
    for s in shapes[1:]:
        bb.add(s.BoundBox)

    bounding_box_mm = {
        "x_min": round(bb.XMin, 3), "x_max": round(bb.XMax, 3),
        "y_min": round(bb.YMin, 3), "y_max": round(bb.YMax, 3),
        "z_min": round(bb.ZMin, 3), "z_max": round(bb.ZMax, 3),
    }

    x_size = bb.XLength
    y_size = bb.YLength
    z_size = bb.ZLength
    dims = sorted([x_size, y_size, z_size])
    if dims[2] > 3 * dims[0] and dims[0] > 0:
        shape_class = "shaft"
    elif dims[0] < 0.2 * dims[2] and dims[2] > 0:
        shape_class = "plate"
    else:
        shape_class = "block"

    # Product names
    product_names = [obj.Label for obj in doc.Objects if hasattr(obj, "Label")][:8]

    # Export orthographic view images
    _export_views(doc, output_dir)

    # Write metadata
    meta = {
        "bounding_box_mm": bounding_box_mm,
        "shape_class": shape_class,
        "product_names": product_names,
    }
    with open(os.path.join(output_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"OK: {shape_class} shape, bbox={bounding_box_mm}", flush=True)


def _export_views(doc: any, output_dir: str) -> None:
    """Export front/side/top orthographic views as PNG."""
    try:
        import FreeCADGui  # type: ignore
        FreeCADGui.setupWithoutGUI()

        views = {
            "front": (0, -1, 0),
            "side":  (1, 0, 0),
            "top":   (0, 0, 1),
        }

        for view_name, direction in views.items():
            try:
                view = FreeCADGui.ActiveDocument.ActiveView
                view.viewIsometric()
                view.setViewDirection(direction)
                view.fitAll()
                out_path = os.path.join(output_dir, f"{view_name}.png")
                view.saveImage(out_path, 640, 480, "White")
            except Exception as exc:
                print(f"View {view_name} failed: {exc}", file=sys.stderr)

    except ImportError:
        # FreeCADGui not available in strict headless mode — use TechDraw fallback
        try:
            import TechDraw  # type: ignore
            import TechDrawGui  # type: ignore
            page = doc.addObject("TechDraw::DrawPage", "Page")
            template = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
            template.Template = FreeCAD.getResourceDir() + "Mod/TechDraw/Templates/A4_LandscapeTD.svg"
            page.Template = template

            view = doc.addObject("TechDraw::DrawViewPart", "FrontView")
            view.Source = [obj for obj in doc.Objects if hasattr(obj, "Shape")]
            view.Direction = FreeCAD.Vector(0, -1, 0)
            page.addView(view)
            doc.recompute()
            TechDraw.writeDXFPage(page, os.path.join(output_dir, "front.dxf"))
        except Exception:
            pass


if __name__ == "__main__":
    main()
