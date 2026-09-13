"""Профиль вала по листу: геометрия замером, числа — надписями (план, Ф3/Ф8).

Живой z4-r4 (фото, апскейл): ридер ошибся в 12 из 13 величин профиля
(Ø канавок из выносных видов принял за ступени, длину паза — за длину
ступени), и проверка честно сказала «не тот вид» — сверять было не с чем.
Но и вид, и надписи на месте: уступы вида делят вал на ступени, а числа,
которые ридер САМ выписал с листа (`spec["dimensions"]`), дают точные
значения. Здесь из них собирается конкурирующий профиль:

* ступени — площадки профиля вида; канавка и фаска (короткие площадки ниже
  соседних ступеней) ступенью не считаются;
* габарит — надпись, при которой станции всех уступов объясняются
  надписями; станция уступа — надпись от торца или от соседней уже
  найденной станции (цепочка в любую сторону, база от торцов, смесь);
* Ø — ближайшая надпись Ø или номинал резьбы; вертикальный масштаб вида
  уточняется по самим надписям (выпрямленное фото анизотропно);
* каждая резьба обязана лечь на одну ступень (M18 на z4-r4 нарисована
  Ø16 — ближе к «Ø15,7» с выносного вида).

Допуск станции — от точности самого листа: реальный чертёж нарисован не
точно в масштабе (z4-r4: уступы до 1,7 мм от надписей), синтетический —
точно. Любая неоднозначность (две надписи одинаково близко, уступ без
надписи) — отказ: предложение либо объясняет весь профиль, либо его нет.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import permutations
from typing import Any

_NUM = r"(\d+(?:[.,]\d+)?)"
_DIAMETER = re.compile(r"[ØφФ⌀]\s*" + _NUM)
_THREAD = re.compile(r"(?<![A-Za-zА-Яа-я])[MМ]\s*" + _NUM + r"(?:\s*[xх×]\s*" + _NUM + r")?")
_BARE = re.compile(r"\s*" + _NUM + r"\s*")
# Доля габарита — допуск первого прохода и потолок допуска станции.
_STATION_SHARE = 0.02
# Две надписи ближе друг к другу, чем эта доля допуска, — неоднозначно.
_AMBIGUOUS = 0.25
# Допуск Ø после уточнения масштаба — доля номинала; резьба — шире: её
# ступень рисуют условно (z4-r4: M18 — 16 мм), но резьба обязана лечь.
_DIAMETER_SHARE = 0.03
_THREAD_SHARE = 0.15
# Площадка короче этой доли вида — канавка или фаска, если она ниже соседних
# ступеней (канавки корпуса 2–3 мм при габарите 125–350, z4-r4 — 2,4 из 185;
# самая короткая ступень корпуса — 12 из 350).
_SHORT_SHARE = 0.03
# Надпись размера стоит над своим отрезком: её центр — между станциями с
# запасом в эту долю габарита (рамки ридера на z4-r4 мимо до 12 мм из 185).
_LABEL_MARGIN = 0.08


@dataclass(frozen=True)
class SheetLabels:
    """Числа листа по смыслу.

    ``axial`` — голые числа с повторами: ``(значение, столбец центра надписи
    или None)``; ``diameters`` — Ø; ``threads`` — ``(номинал, шаг)``.
    """

    axial: tuple[tuple[float, float | None], ...]
    diameters: tuple[float, ...]
    threads: tuple[tuple[float, float | None], ...]


@dataclass(frozen=True)
class ProfileProposal:
    """Профиль, собранный по листу: ступени с числами надписей."""

    steps: tuple[dict[str, Any], ...]
    total_mm: float
    # Худшее расхождение уступа с надписью, мм — точность самого листа.
    station_error_mm: float
    diameter_correction: float
    notes: tuple[str, ...] = field(default=())

    def as_payload(self) -> dict[str, Any]:
        return {
            "steps": [dict(step) for step in self.steps],
            "total_mm": self.total_mm,
            "station_error_mm": round(self.station_error_mm, 3),
            "diameter_correction": round(self.diameter_correction, 4),
            "notes": list(self.notes),
        }


def sheet_labels(spec: dict[str, Any]) -> SheetLabels:
    """Надписи, которые ридер выписал с листа, разобранные по смыслу.

    Осевые — только голые числа («185», «1,5»), с повторами: пояснения ридера
    («Длина ступени: 15») дублируют надписи и несут чужие числа (Ra, номер
    пункта). Столбец центра — из рамки надписи, если ридер её дал.
    """
    axial: list[tuple[float, float | None]] = []
    diameters: set[float] = set()
    threads: dict[float, float | None] = {}
    for item in spec.get("dimensions") or []:
        text = str((item.get("value") if isinstance(item, dict) else item) or "")
        for match in _THREAD.finditer(text):
            nominal = _float(match.group(1))
            pitch = _float(match.group(2)) if match.group(2) else None
            # «M18×15» — запятая шага потеряна при чтении: шаг больше четверти
            # номинала не бывает, такой шаг не берётся.
            if pitch is not None and pitch > nominal / 4.0:
                pitch = None
            if threads.get(nominal) is None:
                threads[nominal] = pitch
        for match in _DIAMETER.finditer(text):
            diameters.add(_float(match.group(1)))
        bare = _BARE.fullmatch(text)
        if bare and _float(bare.group(1)) > 0:
            box = item.get("bbox") if isinstance(item, dict) else None
            column = (
                (float(box[0]) + float(box[2])) / 2.0
                if isinstance(box, (list, tuple))
                and len(box) == 4
                and all(isinstance(v, (int, float)) for v in box)
                else None
            )
            axial.append((_float(bare.group(1)), column))
    return SheetLabels(
        axial=tuple(sorted(axial, key=lambda item: item[0])),
        diameters=tuple(sorted(v for v in diameters if v > 0)),
        threads=tuple(sorted(threads.items())),
    )


def propose_profile(
    profile: Any,
    labels: SheetLabels,
    read_total_mm: float | None,
    *,
    allow_threads: bool = True,
    reserved: tuple[float, ...] = (),
) -> tuple[ProfileProposal | None, str]:
    """Профиль по виду ``profile`` (`shaft_frame.ShaftProfile`) и надписям.

    ``read_total_mm`` — прочитанный габарит: им задан масштаб вида, габарит
    из надписей ищется в пределах ×0,7…1,4 от него. ``allow_threads`` —
    резьбы можно класть на наружные ступени (у полой детали резьба может
    быть внутренней — тогда нет). ``reserved`` — числа, которые ридер отдал
    другим элементам (длина и ширина паза): одна такая надпись из осевых
    убирается (z4-r4: «22» паза давала уступу 93 = 115 − 22 вместо 95).
    Возвращает предложение или ``None`` и причину отказа.
    """
    from app.ai.cad_recognize.verifiers.shaft_profile import _MIN_LINE_PX, _shoulders

    length_px = float(profile.x1 - profile.x0)
    pool = list(labels.axial)
    for value in reserved:
        index = next((i for i, (v, _c) in enumerate(pool) if abs(v - value) <= 1e-6), None)
        if index is not None:
            pool.pop(index)
    if length_px <= 0 or not pool:
        return None, "на листе нет осевых надписей"
    line_px = float(getattr(profile, "line_px", 0.0) or 0.0)
    if 0.0 < line_px < _MIN_LINE_PX:
        # Как у проверки профиля: на грубом листе соседние ступени сливаются
        # (корпус, 75 dpi: Ø32 и Ø35 — одна ступень, и цепочка это «объясняла»).
        return None, f"лист слишком грубый: основная линия {line_px:.1f} px"
    steps = _main_steps(profile)
    if len(steps) < 2:
        return None, "на виде меньше двух ступеней"
    shoulders = [
        x - profile.x0 for x, _jump in _shoulders(profile, jump_px=2.0, min_plateau_px=4.0)
    ]
    # Кандидаты столбца каждого уступа: кромки соседних ступеней и грани
    # между ними — канавка у уступа даёт две грани, надпись стоит у одной.
    bounds_px = [
        sorted({float(a[1]), float(b[0]), *(s for s in shoulders if a[1] - 3 <= s <= b[0] + 3)})
        for a, b in zip(steps, steps[1:])
    ]
    base = (read_total_mm / length_px) if read_total_mm else None

    def placed(scale: float) -> tuple[list[list[float]], list[tuple[float, float | None]]]:
        bounds = [[c * scale for c in candidates] for candidates in bounds_px]
        marks = [
            (value, None if column is None else (column - profile.x0) * scale)
            for value, column in pool
        ]
        return bounds, marks

    best: tuple[float, float, list[float]] | None = None
    for total in sorted({value for value, _column in pool}):
        scale = total / length_px
        if base is not None and not 0.7 * base <= scale <= 1.4 * base:
            continue
        bounds, marks = placed(scale)
        found = _assign(bounds, marks, total, _STATION_SHARE * total, strict=False)
        if isinstance(found, str):
            continue
        cost = sum(found[1]) / len(found[1])
        if best is None or cost < best[0]:
            best = (cost, total, found[1])
    if best is None:
        return None, "уступы вида не объясняются надписями ни при каком габарите"
    _cost, total, residuals = best
    scale = total / length_px
    # Второй проход — допуск по точности листа: у точного листа расхождения
    # десятые доли мм, и широкий допуск первого прохода пускал бы чужие
    # надписи; у нарисованного от руки — миллиметры.
    ordered = sorted(residuals)
    tolerance = min(_STATION_SHARE * total, max(0.5, 4.0 * ordered[len(ordered) // 2]))
    bounds, marks = placed(scale)
    found = _assign(bounds, marks, total, tolerance, strict=True)
    if isinstance(found, str):
        return None, found
    exact, residuals = found
    diameters = _diameters(
        [2.0 * level * scale for _a, _b, level in steps], labels, allow_threads=allow_threads
    )
    if isinstance(diameters, str):
        return None, diameters
    values, correction = diameters
    stations = [0.0, *exact, total]
    result = []
    for (diameter, thread), start, end in zip(values, stations, stations[1:]):
        step: dict[str, Any] = {"diameter_mm": diameter, "length_mm": round(end - start, 3)}
        if thread is not None:
            step["thread"] = thread
        result.append(step)
    proposal = ProfileProposal(
        steps=tuple(result),
        total_mm=total,
        station_error_mm=max(residuals) if residuals else 0.0,
        diameter_correction=correction,
    )
    return proposal, ""


def _main_steps(profile: Any) -> list[tuple[int, int, float]]:
    """Ступени вида: площадки одного уровня слиты, канавки и фаски выброшены.

    Канавка со скруглённой стенкой — несколько коротких площадок подряд
    (shaft-2: 1 мм стенки и 2 мм дна), поэтому выбрасывается вся короткая
    серия, если она ниже ступеней по обе стороны (у торца — ниже соседней).
    """
    from app.ai.cad_recognize.verifiers.shaft_profile import _plateaus

    def same(a: float, b: float) -> bool:
        return abs(a - b) <= max(1.5, 0.01 * max(a, b))

    merged: list[tuple[int, int, float]] = []
    for start, end, level in _plateaus(profile):
        if end - start + 1 < 4:
            continue
        # Разрыв кромки подписью или выносной — та же ступень.
        if merged and same(merged[-1][2], level):
            merged[-1] = (merged[-1][0], end, (merged[-1][2] + level) / 2.0)
        else:
            merged.append((start, end, level))
    short = _SHORT_SHARE * float(profile.x1 - profile.x0)
    steps: list[tuple[int, int, float]] = []

    def add(item: tuple[int, int, float]) -> None:
        # Канавка посреди ступени делит её на две площадки одного уровня —
        # это одна ступень: уступа там нет.
        if steps and same(steps[-1][2], item[2]):
            steps[-1] = (steps[-1][0], item[1], (steps[-1][2] + item[2]) / 2.0)
        else:
            steps.append(item)

    index = 0
    while index < len(merged):
        if merged[index][1] - merged[index][0] >= short:
            add(merged[index])
            index += 1
            continue
        end = index
        while end < len(merged) and merged[end][1] - merged[end][0] < short:
            end += 1
        run = merged[index:end]
        flanks = [
            f
            for f in (steps[-1] if steps else None, merged[end] if end < len(merged) else None)
            if f
        ]
        if not (flanks and all(item[2] < min(f[2] for f in flanks) for item in run)):
            for item in run:
                add(item)
        index = end
    return steps


def _assign(
    bounds: list[list[float]],
    marks: list[tuple[float, float | None]],
    total: float,
    tolerance: float,
    *,
    strict: bool,
) -> tuple[list[float], list[float]] | str:
    """Точные станции уступов по графу размеров: лучшая невязка — первой.

    Надпись связывает уступ с торцом (база) или с ближайшей уже найденной
    станцией слева или справа (цепочка в любую сторону). Каждая надпись
    объясняет не больше одной станции: на неточном листе иначе чужое звено
    (z4-r4: 115 + 15 = 130) перебивало верную надпись 133 = 185 − 52.
    Габарит — тоже надпись, он занят. Надпись с известным положением должна
    стоять над своим отрезком. В строгом проходе вторая надпись, дающая
    другую станцию почти так же близко, — отказ.
    """
    margin = _LABEL_MARGIN * total
    left = list(marks)
    overall = next((i for i, (v, _c) in enumerate(left) if abs(v - total) <= 1e-6), None)
    if overall is None:
        return "габарита нет среди надписей"
    left.pop(overall)
    last = len(bounds) + 1
    known: dict[int, float] = {0: 0.0, last: total}
    residuals: dict[int, float] = {}
    while len(known) < last + 1:
        best = None
        for index in range(1, last):
            if index in known:
                continue
            low = max(j for j in known if j < index)
            high = min(j for j in known if j > index)
            options = []
            for position, (label, column) in enumerate(left):
                for anchor in {0, last, low, high}:
                    value = round(
                        known[anchor] + label if anchor < index else known[anchor] - label, 3
                    )
                    if not known[low] < value < known[high]:
                        continue
                    residual = min(abs(value - c) for c in bounds[index - 1])
                    if residual > tolerance:
                        continue
                    if column is not None and not (
                        min(known[anchor], value) - margin
                        <= column
                        <= max(known[anchor], value) + margin
                    ):
                        continue
                    options.append((residual, value, position))
            if not options:
                continue
            options.sort()
            residual, value, position = options[0]
            rival = next((o for o in options[1:] if abs(o[1] - value) > 1e-3), None)
            if best is None or residual < best[0]:
                best = (residual, index, value, position, rival)
        if best is None:
            missing = [i for i in range(1, last) if i not in known]
            return f"уступ {missing[0]} не объясняется ни одной надписью"
        residual, index, value, position, rival = best
        if strict and rival is not None and rival[0] - residual < _AMBIGUOUS * tolerance:
            return (
                f"уступ {index}: надписи дают {value:g} и {rival[1]:g} почти одинаково "
                f"(допуск {tolerance:.1f} мм)"
            )
        known[index] = value
        residuals[index] = residual
        left.pop(position)
    return [known[i] for i in range(1, last)], [residuals[i] for i in range(1, last)]


def _diameters(
    measured: list[float], labels: SheetLabels, *, allow_threads: bool
) -> tuple[list[tuple[float, dict[str, Any] | None]], float] | str:
    """Ø каждой ступени — надпись Ø или номинал резьбы; поправка вертикального масштаба."""
    import statistics

    threads = list(labels.threads) if allow_threads else []
    names = [*labels.diameters, *(nominal for nominal, _pitch in threads)]
    if not names:
        return "на листе нет надписей Ø"
    ratios = []
    for value in measured:
        nearest = min(names, key=lambda name: abs(name - value))
        if abs(nearest - value) <= 0.06 * nearest:
            ratios.append(nearest / value)
    if len(ratios) < 2:
        return "Ø ступеней не совпадают с надписями Ø даже приблизительно"
    correction = statistics.median(ratios)
    if not 0.9 <= correction <= 1.1:
        return f"поправка вертикального масштаба {correction:.3f} вне 0,9…1,1"
    corrected = [value * correction for value in measured]
    if len(threads) > len(corrected):
        return "резьб на листе больше, чем ступеней"
    best: tuple[float, list[tuple[float, dict[str, Any] | None]]] | None = None
    # Каждая резьба — на одной ступени; остальные — ближайшая надпись Ø.
    for placement in permutations(range(len(corrected)), len(threads)):
        cost = 0.0
        chosen: list[tuple[float, dict[str, Any] | None]] = []
        for index, value in enumerate(corrected):
            if index in placement:
                nominal, pitch = threads[placement.index(index)]
                share = abs(nominal - value) / nominal
                if share > _THREAD_SHARE:
                    break
                designation = f"M{nominal:g}" + (f"x{pitch:g}" if pitch else "")
                chosen.append(
                    (
                        nominal,
                        {
                            "designation": designation,
                            "system": "metric",
                            "nominal_diameter_mm": nominal,
                            "pitch_mm": pitch,
                            "internal": False,
                        },
                    )
                )
                cost += share
                continue
            if not labels.diameters:
                break
            ranked = sorted(labels.diameters, key=lambda name: abs(name - value))
            tolerance = max(0.3, _DIAMETER_SHARE * ranked[0])
            if abs(ranked[0] - value) > tolerance:
                break
            if (
                len(ranked) > 1
                and abs(ranked[1] - value) <= tolerance
                and abs(ranked[1] - value) - abs(ranked[0] - value) < _AMBIGUOUS * tolerance
            ):
                break
            chosen.append((ranked[0], None))
            cost += abs(ranked[0] - value) / ranked[0]
        else:
            if best is None or cost < best[0]:
                best = (cost, chosen)
    if best is None:
        return "Ø ступеней не объясняются надписями Ø и резьб: " + ", ".join(
            f"{value:.2f}" for value in corrected
        )
    return best[1], correction


def _float(text: str) -> float:
    return float(text.replace(",", "."))
