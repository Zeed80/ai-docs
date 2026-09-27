"""Результат метода `views` → спек в схеме ридера (`EngineeringDrawingSpec`).

Метод строит тело сам, но общий путь спека (`_build_spec_solid`) даёт то,
чего у прямого построения нет: граф модели и 3D-редактор, проверку B-Rep
против спека, лист по ЕСКД из тела, свойства для техпроцесса. Метод,
выдающий тот же спек, что и ридер, встаёт в конвейер без второго пути
(решение оператора: «пока параллельно, чтобы потом просто заменить»).

Каждое значение несёт свидетельство — рамку изображения на листе, по
которому оно измерено. Чего спек не выражает (вторая полость, разделённая
перемычкой), — в `optional_unresolved`, а не молча теряется.
"""

from __future__ import annotations

from typing import Any


def _sections(
    points: list[dict[str, float]], evidence: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Ломаная (r, z) → сечения: площадка — Ø и длина, наклон — конус."""
    sections: list[dict[str, Any]] = []
    for a, b in zip(points, points[1:]):
        length = float(b["z"]) - float(a["z"])
        if length <= 1e-6 or a["r"] <= 0 or b["r"] <= 0:
            continue
        section: dict[str, Any] = {
            "diameter_mm": round(2.0 * float(a["r"]), 4),
            "length_mm": round(length, 4),
            "evidence": evidence,
        }
        end = 2.0 * float(b["r"])
        if abs(end - section["diameter_mm"]) > 0.01 * max(end, section["diameter_mm"]):
            section["taper"] = {"kind": "end_diameter", "end_diameter_mm": round(end, 4)}
        sections.append(section)
    return sections


def _threads(
    outer: list[dict[str, Any]], bore: list[dict[str, Any]], label_texts: list[str]
) -> None:
    """Резьба по надписи «M…» — на одном сечении с ближайшим номинальным Ø
    (наружном или расточке): одна надпись — одна резьба."""
    from app.ai.cad_views.labels import parse_label

    for text in label_texts:
        label = parse_label(text)
        if label.kind != "thread" or not label.value:
            continue
        candidates = [
            (abs(section["diameter_mm"] - label.value), section, internal)
            for sections, internal in ((outer, False), (bore, True))
            for section in sections
            if not section.get("taper") and not section.get("thread")
        ]
        candidates = [c for c in candidates if c[0] <= 0.12 * label.value]
        if not candidates:
            continue
        _gap, section, internal = min(candidates, key=lambda c: c[0])
        thread: dict[str, Any] = {
            "designation": text.split()[0][:40],
            "nominal_diameter_mm": label.value,
            "internal": internal,
        }
        if label.pitch:
            thread["pitch_mm"] = label.pitch
        section["thread"] = thread
        section["diameter_mm"] = label.value


def views_to_spec(
    result: Any, label_texts: list[str], *, source_box: tuple[int, int, int, int] | None
) -> dict[str, Any] | None:
    """Спек по результату метода; None — основа, которую спек не выражает."""
    evidence = [{"image_index": 0, "bbox": [float(v) for v in source_box]}] if source_box else []
    unresolved: list[str] = []
    profile = result.profile or {}
    if profile.get("kind") == "extrude":
        holes = []
        for feature in result.features or []:
            params = feature.get("params") or {}
            if "diameter_mm" not in params:
                continue
            holes.append(
                {
                    "center_x_mm": float(params["center_x_mm"]),
                    "center_y_mm": float(params["center_y_mm"]),
                    "diameter_mm": float(params["diameter_mm"]),
                    "evidence": evidence,
                }
            )
        body: dict[str, Any] = {
            "type": "plate",
            "profile": {
                "shape": "sketch",
                "sketch": [
                    {
                        key: value
                        for key, value in segment.items()
                        if key in ("kind", "to", "center", "clockwise")
                    }
                    for segment in profile.get("sketch") or []
                ],
                "thickness_mm": float(profile["thickness_mm"]),
                "holes": holes,
            },
        }
    else:
        outer = profile.get("outer") or []
        if len(outer) < 2:
            return None
        body = {"type": "rotation", "outer": _sections(outer, evidence)}
        bore = profile.get("bore") or []
        length = float(outer[-1]["z"])
        if bore:
            # Расточки — участки r > 0; спек выражает одну, открытую к торцу.
            runs: list[list[dict[str, float]]] = []
            for point in bore:
                if point["r"] > 0:
                    if runs and runs[-1] and runs[-1][-1]["r"] > 0:
                        runs[-1].append(point)
                    else:
                        runs.append([point])
                else:
                    runs.append([])
            runs = [run for run in runs if len(run) >= 2]
            opened = [run for run in runs if run[0]["z"] <= 0.1 or run[-1]["z"] >= length - 0.1]
            if opened:
                chosen = max(opened, key=lambda run: run[-1]["z"] - run[0]["z"])
                clipped = [{"r": p["r"], "z": min(max(p["z"], 0.0), length)} for p in chosen]
                from_right = chosen[0]["z"] > 0.1
                start = clipped[0]["z"]
                shifted = [{"r": p["r"], "z": p["z"] - start} for p in clipped]
                body["bore"] = _sections(shifted, evidence)
                if from_right:
                    body["bore_from_end"] = "right"
                    body["bore_start_mm"] = 0.0
                    body["bore_blind"] = True
                elif clipped[-1]["z"] < length - 0.1:
                    body["bore_blind"] = True
                for run in runs:
                    if run is not chosen:
                        unresolved.append(
                            f"полость Ø{2 * max(p['r'] for p in run):.1f} на "
                            f"{run[0]['z']:.1f}…{run[-1]['z']:.1f} мм — спек выражает одну "
                            "расточку от торца, эта не перенесена"
                        )
        _threads(body["outer"], body.get("bore") or [], label_texts)
        placed = []
        for feature in result.features or []:
            if feature.get("kind") not in ("hole", "pocket"):
                continue
            item = {
                "kind": feature["kind"],
                "profile": feature.get("profile") or "circle",
                "origin_mm": [float(v) for v in feature["origin_mm"]],
                "axis": [float(v) for v in feature["axis"]],
                "ref": [float(v) for v in feature.get("ref") or [1.0, 0.0, 0.0]],
                "evidence": evidence,
            }
            for key in ("diameter_mm", "width_mm", "height_mm", "depth_mm", "through"):
                if feature.get(key) is not None:
                    item[key] = feature[key]
            placed.append(item)
        if placed:
            body["placed_features"] = placed
    return {
        "schema_version": 1,
        "part": "",
        "main_view": body,
        "views": [
            {
                "kind": "section" if profile.get("role") == "section" else "front",
                "view_id": "views-main",
                "relation": "primary",
                "evidence": evidence,
            }
        ],
        "optional_unresolved": unresolved,
        "unresolved": [],
    }


__all__ = ["views_to_spec"]
