"""Сечение на листе ↔ паз ↔ вид спека (межвидовое соответствие вала).

Живой z4-r4: паз подтверждён на главном виде, сечения А-А и Б-Б найдены
кругами, но граф держал `cross_view_not_available` — связи не было.
"""

from __future__ import annotations

import asyncio
import io

from PIL import Image

from app.ai.cad_recognize.verifiers.section_link import (
    _letters,
    attach_section_views,
    label_crop_box,
    label_links,
    link_disks_to_keyways,
)


def _z4_spec(views=None):
    # M18×15 · Ø25×19 · Ø35×31 · Ø30×30 · Ø25×20 · M24×18 · Ø22×52 — как лист.
    steps = [(18, 15), (25, 19), (35, 31), (30, 30), (25, 20), (24, 18), (22, 52)]
    return {
        "main_view": {
            "outer": [
                {"id": f"0:outer:{i}", "diameter_mm": d, "length_mm": length}
                for i, (d, length) in enumerate(steps)
            ],
            "keyways": [
                {"id": "0:keyways:0", "axial_start_mm": 71.0, "length_mm": 22.0},
                {"id": "sheet:keyways:1", "axial_start_mm": 150.0, "length_mm": 25.0},
            ],
        },
        "views": views
        if views is not None
        else [
            {"kind": "section", "view_id": "B-B", "label": "Б-Б", "body_index": 0},
            {"kind": "section", "view_id": "A-A", "label": "А-А", "body_index": 0},
        ],
    }


# Круги сечений z4-r4 (section_disk на живом листе): Ø30,9 и Ø23.
_DISKS = [
    {"center_px": [900.0, 300.0], "radius_px": 60.0, "diameter_mm": 30.9, "solid": True},
    {"center_px": [1300.0, 300.0], "radius_px": 45.0, "diameter_mm": 23.0, "solid": True},
]


def test_each_z4_disk_goes_to_the_keyway_of_its_step():
    links = link_disks_to_keyways(_z4_spec(), _DISKS)

    assert [(link["keyway_id"], link["step_diameter_mm"]) for link in links] == [
        ("0:keyways:0", 30.0),
        ("sheet:keyways:1", 22.0),
    ]


def test_a_disk_between_two_keyed_steps_or_two_keyways_on_a_step_is_not_linked():
    spec = _z4_spec()
    spec["main_view"]["outer"][6]["diameter_mm"] = 28.0  # паз 2 теперь на Ø28
    # Ø29,5 между Ø30 и Ø28 — ступень по кругу не различить.
    between = [{**_DISKS[0], "diameter_mm": 29.0}]
    assert link_disks_to_keyways(spec, between) == []

    two = _z4_spec()
    two["main_view"]["keyways"].append(
        {"id": "0:keyways:2", "axial_start_mm": 66.0, "length_mm": 4.0}
    )
    assert [link["keyway_id"] for link in link_disks_to_keyways(two, _DISKS)] == ["sheet:keyways:1"]
    # Кольцо (полый вал) — не сечение через паз сплошного вала.
    assert link_disks_to_keyways(_z4_spec(), [{**_DISKS[0], "solid": False}]) == []


def test_section_letters_are_normalised():
    assert _letters("Б–Б") == "Б-Б"
    assert _letters("Сечение б-б (2:1)") == "Б-Б"
    assert _letters("A-A") == "А-А"  # латиница как кириллица
    assert _letters("А-Б") == ""
    assert _letters(None) == ""


def _png(size=(1600, 800)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_the_model_reads_the_label_and_only_a_unique_match_links_a_view():
    spec = _z4_spec()
    links = link_disks_to_keyways(spec, _DISKS)
    answers = iter([{"label": "A–A"}, {"label": "Б-Б"}])
    prompts = []

    async def ask(prompt, crop):
        prompts.append((prompt, crop.size))
        return next(answers)

    labelled, log = asyncio.run(label_links(_png(), spec, links, ask=ask))

    assert [(link.get("view_id"), link["keyway_id"]) for link in labelled] == [
        ("A-A", "0:keyways:0"),
        ("B-B", "sheet:keyways:1"),
    ]
    assert [entry["outcome"] for entry in log] == ["связано: А-А", "связано: Б-Б"]
    # Модели не подсказывают ожидаемое: вопрос одинаков для каждого круга.
    assert len({p for p, _ in prompts}) == 1

    same = iter([{"label": "Б-Б"}, {"label": "Б-Б"}])

    async def repeat(prompt, crop):
        return next(same)

    labelled, log = asyncio.run(label_links(_png(), spec, links, ask=repeat))
    assert [link.get("view_id") for link in labelled] == ["B-B", None]
    assert "уже связан" in log[1]["outcome"]

    async def silent(prompt, crop):
        return {"label": None}

    labelled, _log = asyncio.run(label_links(_png(), spec, links, ask=silent))
    assert not any(link.get("view_id") for link in labelled)


def test_the_label_crop_reaches_above_the_disk():
    box = label_crop_box(_DISKS[0] | {"center_px": [900.0, 300.0]}, (1600, 800))
    assert box[1] < 300.0 - 60.0 - 100.0  # над кругом с запасом
    assert box[0] < 900.0 - 60.0 and box[2] > 900.0 + 60.0


def test_a_linked_keyway_on_its_section_gives_the_graph_its_cross_view_edge():
    """Граф целиком: паз на главном виде и на своём сечении — уровень 6 снят."""
    from app.ai.cad_emg_compat import spec_feature_tree_as_graph
    from app.ai.cad_ir.feature_tree import FeatureTreeCandidate
    from app.ai.cad_recognize.verifiers.stage import attach_verified_views
    from app.services.engineering_model_graph import verify_graph

    spec = _z4_spec()
    report = {
        "items": [
            {"kind": "shaft_step", "feature_id": "0:outer:3", "status": "confirmed"},
            {"kind": "keyway", "feature_id": "0:keyways:0", "status": "confirmed"},
        ],
        "frame": {"bbox_px": [100.0, 200.0, 800.0, 400.0]},
    }
    placed = attach_verified_views(spec, report)
    links = [
        {**link, "view_id": view, "label": label}
        for link, (view, label) in zip(
            link_disks_to_keyways(placed, _DISKS), [("A-A", "А-А"), ("B-B", "Б-Б")], strict=True
        )
    ]
    tree = FeatureTreeCandidate(features=[], score=0.5, label="t")

    before = spec_feature_tree_as_graph(placed, tree, graph_id="g")
    linked = attach_section_views(placed, links)
    after = spec_feature_tree_as_graph(linked, tree, graph_id="g")

    by_id = {view["view_id"]: view for view in linked["views"]}
    assert by_id["A-A"]["features_shown"] == ["0:keyways:0"]
    # Паз, найденный по листу, показан на главном виде — и на своём сечении.
    assert by_id["B-B"]["features_shown"] == ["sheet:keyways:1"]

    # Паз, не подтверждённый на главном виде, на сечение не кладётся.
    unconfirmed = attach_verified_views(_z4_spec(), {**report, "items": report["items"][:1]})
    kept = attach_section_views(unconfirmed, links)
    assert not {v["view_id"]: v for v in kept["views"]}["A-A"].get("features_shown")
    codes = lambda graph: [issue["code"] for issue in verify_graph(graph)[1]]  # noqa: E731
    assert "cross_view_not_available" in codes(before)
    assert "cross_view_not_available" not in codes(after)
