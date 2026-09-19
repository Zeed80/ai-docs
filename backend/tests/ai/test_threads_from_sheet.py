"""Резьбовое отверстие пластины, прочитанное гладким, — по замеру и надписи (X1)."""

from app.ai.cad_recognize.verifiers.reconcile import apply_threads, threads_from_sheet


def _case(labels: list[str], measured_d: float = 6.65):
    spec = {
        "dimensions": [{"value": v} for v in labels],
        "main_view": {
            "profile": {"holes": [{"center_x_mm": 8.0, "center_y_mm": -24.0, "diameter_mm": 6.0}]}
        },
    }
    report = {
        "items": [
            {
                "kind": "plate_hole",
                "path": "main_view.profile.holes[0]",
                "status": "refuted",
                "read": {"center_x_mm": 8.0, "center_y_mm": -24.0, "diameter_mm": 6.0},
                "measured": {"center_x_mm": 8.05, "center_y_mm": -23.9, "diameter_mm": measured_d},
                "tolerance_mm": {"position": 0.5, "diameter": 0.3},
            }
        ]
    }
    return spec, report


def test_a_tapped_hole_read_as_plain_takes_the_thread_named_on_the_sheet():
    """Живая пластина: «M8» прочитано как Ø6; на плане — Ø впадин M8 (6,65)."""
    spec, report = _case(["80", "M8", "M12"])
    (decision,) = threads_from_sheet(spec, report)
    fixed = apply_threads(spec, [decision])["main_view"]["profile"]["holes"][0]
    assert fixed["diameter_mm"] == 8.0 and fixed["thread"]["designation"] == "M8"


def test_without_the_designation_on_the_sheet_nothing_is_guessed():
    spec, report = _case(["80", "M12"])
    assert threads_from_sheet(spec, report) == []


def test_a_plain_hole_of_another_size_is_not_turned_into_a_thread():
    spec, report = _case(["80", "M8"], measured_d=7.4)
    assert threads_from_sheet(spec, report) == []


def test_a_misread_thread_is_asked_again_and_taken_only_if_it_fits_the_circle():
    import asyncio
    import io

    from PIL import Image

    from app.ai.cad_recognize.verifiers.reask import reask_hole_threads

    spec, report = _case(["80", "M6"], measured_d=6.69)  # ридер: «M6», Ø6
    report["items"][0]["evidence_bbox_px"] = [100, 100, 120, 120]
    buffer = io.BytesIO()
    Image.new("RGB", (400, 400), "white").save(buffer, format="PNG")

    async def says(value):
        async def ask(prompt, crop):
            return {"thread": value}

        return await reask_hole_threads(buffer.getvalue(), spec, report, ask=ask)

    (decision,) = asyncio.run(says("M8"))
    assert decision["value"]["designation"] == "M8"
    # Ответ, не совпавший с замером, и «не резьбовое» — ничего.
    assert asyncio.run(says("M10")) == [] and asyncio.run(says(None)) == []


def test_a_face_feature_note_without_a_front_view_is_not_about_this_part():
    from app.ai.cad_recognize.verifiers.reconcile import settle_phantom_wall_features

    note = "элементы граней не построены (top: глубина или положение не проставлены)"
    spec = {"unresolved": [note, "другое"]}
    plate = {"housing_views": {"plan_bbox_px": [0, 0, 1, 1], "side_bbox_px": [2, 0, 3, 1]}}
    assert settle_phantom_wall_features(spec, plate)["unresolved"] == ["другое"]
    housing = {"housing_views": {**plate["housing_views"], "front_bbox_px": [0, 2, 1, 3]}}
    assert settle_phantom_wall_features(spec, housing)["unresolved"] == [note, "другое"]
