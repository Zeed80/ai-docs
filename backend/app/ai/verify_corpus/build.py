"""Один образец корпуса: спек → тело в ядре → лист из тела → растр + эталон.

Цепочка та же, что у перечерчивания в продукте (`_build_spec_solid`), без
гейтов и графа: здесь спек верен по построению, проверять его незачем, а
лист должен выглядеть так, как выглядят листы, которые продукт рисует сам.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.ai.verify_corpus.render import render_sheet


@dataclass
class Sample:
    png: bytes
    truth: dict[str, Any]


class SampleUnavailable(RuntimeError):
    """Ядро не собрало тело или не нарисовало лист — образца нет."""


async def build_sample(spec: dict, *, dpi: int = 300) -> Sample:
    from app.ai.cad_ir.sheet_from_solid import PAPER_PX_PER_MM, build_sheet_from_solid
    from app.ai.cad_solid import feature_tree_from_spec
    from app.services.cad_kernel import compile_candidate

    candidate = feature_tree_from_spec(spec)
    if candidate is None:
        raise SampleUnavailable("спек не даёт дерева операций")
    artifacts = await compile_candidate(
        candidate,
        confirm_assumptions=True,
        metadata={"source": "verify_corpus"},
    )
    sheet = await build_sheet_from_solid(candidate, spec, artifacts.report)
    if sheet is None:
        raise SampleUnavailable("ядро не нарисовало лист")

    rendered = render_sheet(sheet.ir, dpi=dpi, ir_px_per_mm=PAPER_PX_PER_MM)
    truth = {
        "spec": spec,
        "dpi": dpi,
        "image_size_px": [rendered.width_px, rendered.height_px],
        # Сколько пикселей растра на миллиметр ДЕТАЛИ: масштаб листа (1:2, 2:1)
        # умножается на разрешение. Это то, что обязана найти калибровка.
        "px_per_part_mm": (dpi / 25.4) * float(sheet.plan.ratio),
        "sheet": {
            "format": sheet.plan.sheet_format,
            "scale_label": sheet.plan.scale_label,
            "ratio": float(sheet.plan.ratio),
            "part_class": sheet.plan.part_class,
            "views": [view.get("kind") for view in sheet.plan.views],
        },
        "labels": rendered.labels,
        "report": {
            key: artifacts.report.get(key)
            for key in ("volume_mm3", "bounds_mm", "surface_area_mm2")
            if isinstance(artifacts.report, dict)
        },
        "sheet_verified": bool((sheet.verification or {}).get("ok")),
        "warnings": list(sheet.warnings or []),
    }
    return Sample(png=rendered.png, truth=truth)
