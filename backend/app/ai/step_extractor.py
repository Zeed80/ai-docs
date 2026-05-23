"""STEP/IGES 3D geometry extractor.

Extracts bounding box, cylindrical/planar faces, product names, and generates
orthographic view renders from STEP/IGES files for use in drawing analysis
and blank selection.

Primary path: pythonocc-core (OpenCASCADE) — full geometry.
Fallback: text-only parsing of STEP entity list (always available).
"""

from __future__ import annotations

import io
import re
import structlog
from dataclasses import dataclass, field
from typing import Any

logger = structlog.get_logger()


@dataclass
class CylFace:
    axis: list[float]          # [x, y, z] direction vector
    diameter_mm: float
    length_mm: float


@dataclass
class PlanarFace:
    normal: list[float]        # [x, y, z]
    area_mm2: float


@dataclass
class StepGeometryResult:
    bounding_box_mm: dict[str, float]     # {x_min, x_max, y_min, y_max, z_min, z_max}
    face_count: int
    cylindrical_faces: list[CylFace] = field(default_factory=list)
    planar_faces: list[PlanarFace] = field(default_factory=list)
    edge_count: int = 0
    product_names: list[str] = field(default_factory=list)
    view_images: dict[str, bytes] = field(default_factory=dict)  # {"front": png, ...}
    shape_class: str = "block"       # "shaft"|"plate"|"block"
    volume_mm3: float = 0.0
    source: str = "text_fallback"    # "pythonocc"|"text_fallback"


def extract_step_geometry(
    file_bytes: bytes,
    filename: str,
    generate_views: bool = True,
) -> StepGeometryResult:
    """Extract 3D geometry data from STEP or IGES file.

    Attempts pythonocc-core first; falls back to regex text parsing.
    """
    try:
        return _extract_via_pythonocc(file_bytes, filename, generate_views=generate_views)
    except ImportError:
        logger.info("pythonocc_not_available", filename=filename)
    except Exception as exc:
        logger.warning("pythonocc_extraction_failed", filename=filename, error=str(exc))

    return _extract_via_text(file_bytes, filename)


# ── pythonocc (OpenCASCADE) path ───────────────────────────────────────────────


def _extract_via_pythonocc(
    file_bytes: bytes,
    filename: str,
    generate_views: bool = True,
) -> StepGeometryResult:
    import os
    import tempfile

    from OCC.Core.STEPControl import STEPControl_Reader
    from OCC.Core.IGESControl import IGESControl_Reader
    from OCC.Core.BRep import BRep_Builder
    from OCC.Core.BRepBndLib import brepbndlib
    from OCC.Core.Bnd import Bnd_Box
    from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
    from OCC.Core.TopExp import TopExp_Explorer
    from OCC.Core.TopAbs import TopAbs_FACE, TopAbs_EDGE
    from OCC.Core.BRepAdaptor import BRepAdaptor_Surface
    from OCC.Core.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane
    from OCC.Core.GProp import GProp_GProps
    from OCC.Core.BRepGProp import brepgprop
    from OCC.Core.TopoDS import topods

    is_iges = filename.lower().endswith((".igs", ".iges"))
    ext = ".igs" if is_iges else ".stp"

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
        tf.write(file_bytes)
        tmp_path = tf.name

    try:
        if is_iges:
            reader = IGESControl_Reader()
        else:
            reader = STEPControl_Reader()

        status = reader.ReadFile(tmp_path)
        if status != 1:
            raise RuntimeError(f"Reader returned status {status}")

        reader.TransferRoots()
        shape = reader.OneShape()

        # Tessellate for face/edge iteration
        mesh = BRepMesh_IncrementalMesh(shape, 0.1)
        mesh.Perform()

        # Bounding box
        bbox = Bnd_Box()
        brepbndlib.Add(shape, bbox)
        x_min, y_min, z_min, x_max, y_max, z_max = bbox.Get()

        bounding_box_mm = {
            "x_min": round(x_min, 3), "x_max": round(x_max, 3),
            "y_min": round(y_min, 3), "y_max": round(y_max, 3),
            "z_min": round(z_min, 3), "z_max": round(z_max, 3),
        }

        # Volume
        props = GProp_GProps()
        brepgprop.VolumeProperties(shape, props)
        volume_mm3 = props.Mass()

        # Face exploration
        face_count = 0
        cylindrical_faces: list[CylFace] = []
        planar_faces: list[PlanarFace] = []

        face_exp = TopExp_Explorer(shape, TopAbs_FACE)
        while face_exp.More():
            face = topods.Face(face_exp.Current())
            face_count += 1
            try:
                adaptor = BRepAdaptor_Surface(face)
                surf_type = adaptor.GetType()

                if surf_type == GeomAbs_Cylinder:
                    cyl = adaptor.Cylinder()
                    axis_dir = cyl.Axis().Direction()
                    r = cyl.Radius()
                    fp_props = GProp_GProps()
                    brepgprop.SurfaceProperties(face, fp_props)
                    area = fp_props.Mass()
                    length = area / (2 * 3.14159 * r) if r > 0 else 0
                    cylindrical_faces.append(CylFace(
                        axis=[axis_dir.X(), axis_dir.Y(), axis_dir.Z()],
                        diameter_mm=round(r * 2, 4),
                        length_mm=round(length, 4),
                    ))

                elif surf_type == GeomAbs_Plane:
                    pln = adaptor.Plane()
                    n = pln.Axis().Direction()
                    fp_props = GProp_GProps()
                    brepgprop.SurfaceProperties(face, fp_props)
                    area = fp_props.Mass()
                    planar_faces.append(PlanarFace(
                        normal=[round(n.X(), 4), round(n.Y(), 4), round(n.Z(), 4)],
                        area_mm2=round(area, 4),
                    ))
            except Exception:
                pass
            face_exp.Next()

        # Edge count
        edge_count = 0
        edge_exp = TopExp_Explorer(shape, TopAbs_EDGE)
        while edge_exp.More():
            edge_count += 1
            edge_exp.Next()

        # Shape classification
        shape_class = _classify_shape(bounding_box_mm, volume_mm3)

        # Product names from STEP (text fallback for names even in pythonocc path)
        product_names = _extract_product_names(file_bytes)

        # Orthographic view images
        view_images: dict[str, bytes] = {}
        if generate_views:
            try:
                view_images = _render_views(shape)
            except Exception as view_exc:
                logger.warning("step_view_render_failed", error=str(view_exc))

        logger.info(
            "step_pythonocc_ok",
            filename=filename,
            faces=face_count,
            cylinders=len(cylindrical_faces),
            volume=round(volume_mm3, 1),
        )
        return StepGeometryResult(
            bounding_box_mm=bounding_box_mm,
            face_count=face_count,
            cylindrical_faces=cylindrical_faces,
            planar_faces=planar_faces,
            edge_count=edge_count,
            product_names=product_names,
            view_images=view_images,
            shape_class=shape_class,
            volume_mm3=round(volume_mm3, 3),
            source="pythonocc",
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _render_views(shape: Any) -> dict[str, bytes]:
    """Render front/side/top orthographic views using pythonocc offscreen renderer."""
    from OCC.Core.gp import gp_Dir, gp_Pnt, gp_Vec, gp_Ax2
    from OCC.Display.SimpleGui import init_display  # type: ignore
    from OCC.Core.V3d import V3d_DirectionalLight  # noqa — not used directly

    # Attempt headless rendering via offscreen viewer
    try:
        from OCC.Display.OCCViewer import Viewer3d  # type: ignore
        viewer = Viewer3d()
        viewer.Create()
        viewer.SetModeShaded()

        views: dict[str, bytes] = {}
        view_dirs = {
            "front": (0, -1, 0),
            "side": (1, 0, 0),
            "top": (0, 0, 1),
        }
        for view_name, (vx, vy, vz) in view_dirs.items():
            viewer.View.SetProj(vx, vy, vz)
            viewer.FitAll()
            viewer.DisplayShape(shape, update=True)
            png_bytes = viewer.GetImageData(640, 480, "PNG")
            if png_bytes:
                views[view_name] = png_bytes

        return views

    except Exception as exc:
        logger.debug("pythonocc_headless_renderer_failed", error=str(exc))
        return {}


# ── Text-only fallback path ────────────────────────────────────────────────────


def _extract_via_text(file_bytes: bytes, filename: str) -> StepGeometryResult:
    """Parse STEP/IGES text to extract product names and basic entity statistics.

    Computes an approximate bounding box heuristic from CARTESIAN_POINT values.
    """
    text = file_bytes.decode("utf-8", errors="replace")

    product_names = _extract_product_names(file_bytes)

    # Sample Cartesian points for bounding box estimation
    coord_matches = re.findall(
        r"CARTESIAN_POINT\s*\([^,)]+,\s*\(([^)]+)\)\s*\)",
        text[:2_000_000],
    )

    xs, ys, zs = [], [], []
    for match in coord_matches[:5000]:
        parts = match.split(",")
        if len(parts) >= 3:
            try:
                xs.append(float(parts[0].strip()))
                ys.append(float(parts[1].strip()))
                zs.append(float(parts[2].strip()))
            except ValueError:
                pass

    if xs:
        bounding_box_mm = {
            "x_min": round(min(xs), 3), "x_max": round(max(xs), 3),
            "y_min": round(min(ys), 3), "y_max": round(max(ys), 3),
            "z_min": round(min(zs), 3), "z_max": round(max(zs), 3),
        }
    else:
        bounding_box_mm = {
            "x_min": 0.0, "x_max": 0.0,
            "y_min": 0.0, "y_max": 0.0,
            "z_min": 0.0, "z_max": 0.0,
        }

    shape_class = _classify_shape(bounding_box_mm, volume_mm3=0.0)

    logger.info(
        "step_text_fallback_ok",
        filename=filename,
        products=len(product_names),
        sample_points=len(xs),
    )
    return StepGeometryResult(
        bounding_box_mm=bounding_box_mm,
        face_count=0,
        product_names=product_names,
        shape_class=shape_class,
        source="text_fallback",
    )


# ── Shared helpers ─────────────────────────────────────────────────────────────


def _extract_product_names(file_bytes: bytes) -> list[str]:
    """Extract PRODUCT / PART_NAME strings from STEP or IGES file header."""
    text = file_bytes.decode("utf-8", errors="replace")
    # STEP: PRODUCT('name', ...)
    names = list(dict.fromkeys(re.findall(r"PRODUCT\s*\(\s*'([^']{1,80})'", text[:500_000])))
    # IGES: PART_NAME = 'name'
    if not names:
        names = list(dict.fromkeys(re.findall(r"PART_NAME\s*=\s*'([^']{1,80})'", text[:100_000])))
    return names[:8]


def _classify_shape(bbox: dict[str, float], volume_mm3: float) -> str:
    """Classify geometric shape for blank selection.

    shaft  — longest dimension > 3× shortest  (round bar stock)
    plate  — one dimension < 20% of longest   (sheet/plate stock)
    block  — everything else                   (rectangular billet)
    """
    x_size = bbox.get("x_max", 0) - bbox.get("x_min", 0)
    y_size = bbox.get("y_max", 0) - bbox.get("y_min", 0)
    z_size = bbox.get("z_max", 0) - bbox.get("z_min", 0)

    dims = sorted([abs(x_size), abs(y_size), abs(z_size)])
    if not any(d > 0 for d in dims):
        return "block"

    min_dim = dims[0]
    max_dim = dims[2]

    if max_dim > 3 * min_dim and min_dim > 0:
        return "shaft"
    if min_dim < 0.2 * max_dim and max_dim > 0:
        return "plate"
    return "block"


def recommend_blank_from_geometry(result: StepGeometryResult, density_g_cm3: float = 7.85) -> dict:
    """Generate blank/stock recommendation based on StepGeometryResult.

    Returns a dict with shape_class, dimensions, stock_type, allowance_mm, kim.
    """
    bbox = result.bounding_box_mm
    x_size = abs(bbox.get("x_max", 0) - bbox.get("x_min", 0))
    y_size = abs(bbox.get("y_max", 0) - bbox.get("y_min", 0))
    z_size = abs(bbox.get("z_max", 0) - bbox.get("z_min", 0))

    allowance_mm = 5.0  # machining allowance per side in mm

    if result.shape_class == "shaft":
        max_d = max(x_size, y_size, z_size)
        radial_size = sorted([x_size, y_size, z_size])[1]
        blank_diameter = radial_size + 2 * allowance_mm
        blank_length = max_d + 2 * allowance_mm
        volume_blank_cm3 = 3.14159 * (blank_diameter / 20) ** 2 * (blank_length / 10)
        stock_type = "round_bar"
        dims = {"diameter_mm": round(blank_diameter, 1), "length_mm": round(blank_length, 1)}
    elif result.shape_class == "plate":
        blank_x = x_size + 2 * allowance_mm
        blank_y = y_size + 2 * allowance_mm
        blank_z = z_size + 2 * allowance_mm
        volume_blank_cm3 = (blank_x * blank_y * blank_z) / 1000
        stock_type = "sheet"
        dims = {
            "length_mm": round(max(blank_x, blank_y, blank_z), 1),
            "width_mm": round(sorted([blank_x, blank_y, blank_z])[1], 1),
            "thickness_mm": round(min(blank_x, blank_y, blank_z), 1),
        }
    else:
        blank_x = x_size + 2 * allowance_mm
        blank_y = y_size + 2 * allowance_mm
        blank_z = z_size + 2 * allowance_mm
        volume_blank_cm3 = (blank_x * blank_y * blank_z) / 1000
        stock_type = "rectangular_billet"
        dims = {
            "length_mm": round(blank_x, 1),
            "width_mm": round(blank_y, 1),
            "height_mm": round(blank_z, 1),
        }

    # KIM (material utilization coefficient)
    if result.volume_mm3 > 0 and volume_blank_cm3 > 0:
        volume_part_cm3 = result.volume_mm3 / 1000
        kim = round(volume_part_cm3 / volume_blank_cm3, 3)
    else:
        kim = None

    mass_blank_kg = round(volume_blank_cm3 * density_g_cm3 / 1000, 3) if volume_blank_cm3 > 0 else None

    return {
        "shape_class": result.shape_class,
        "stock_type": stock_type,
        "dimensions": dims,
        "allowance_mm": allowance_mm,
        "kim": kim,
        "mass_blank_kg": mass_blank_kg,
        "source": result.source,
    }
