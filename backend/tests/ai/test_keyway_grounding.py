"""Шпоночный паз: одна ступень и сечение по ГОСТ 23360.

Оператор: «положение и размеры шпоночных пазов не правильные и не всегда на
правильных элементах вала». Разобрано на его же прогоне `z4-r4.jpg`.

Профиль: Ø18(0-15) Ø25(15-34) Ø35(34-54) Ø30(54-78) Ø25(78-93) Ø24,5(93-103)
Ø24(103-118) Ø22(118-153). Пазы: 46..68 и 78..103 — каждый верхом на границе
двух ступеней, чего у фрезерованного паза не бывает. Проверялось при этом
только, что паз укладывается в общую длину детали.

Сечение не проверялось ничем: глубина сравнивалась с радиусом САМОЙ ТОЛСТОЙ
ступени, а не той, где паз лежит, а ширина — ни с чем. На ступени Ø30 стояла
ширина 3,5 мм при табличных 8 — число пришло с чужой выноски.
"""

from __future__ import annotations

from app.ai.cad_recognize.keyway_standard import (
    ground_keyways,
    standard_section,
    step_for,
    steps_with_stations,
)

# Настоящий профиль из прогона c85344ae.
OUTER = [
    {"id": "0:outer:0", "diameter_mm": 18.0, "length_mm": 15.0},
    {"id": "0:outer:1", "diameter_mm": 25.0, "length_mm": 19.0},
    {"id": "0:outer:2", "diameter_mm": 35.0, "length_mm": 20.0},
    {"id": "0:outer:3", "diameter_mm": 30.0, "length_mm": 24.0},
    {"id": "0:outer:4", "diameter_mm": 25.0, "length_mm": 15.0},
    {"id": "0:outer:5", "diameter_mm": 24.5, "length_mm": 10.0},
    {"id": "0:outer:6", "diameter_mm": 24.0, "length_mm": 15.0},
    {"id": "0:outer:7", "diameter_mm": 22.0, "length_mm": 35.0},
]


# ── Таблица стандарта ────────────────────────────────────────────────────────


def test_the_section_is_fixed_by_the_shaft_diameter():
    assert standard_section(30.0) == (8.0, 4.0)
    assert standard_section(35.0) == (10.0, 5.0)
    assert standard_section(22.0) == (6.0, 3.5)


def test_the_ranges_are_over_and_up_to_inclusive():
    """Границы таблицы «свыше d0 до d1»: Ø22 — ещё 6×3,5, Ø22,1 — уже 8×4."""
    assert standard_section(22.0) == (6.0, 3.5)
    assert standard_section(22.1) == (8.0, 4.0)


def test_a_diameter_outside_the_table_yields_nothing_rather_than_a_guess():
    assert standard_section(3.0) is None
    assert standard_section(400.0) is None


# ── Привязка к ступени ───────────────────────────────────────────────────────


def test_a_keyway_is_assigned_to_the_step_holding_its_middle():
    stations = steps_with_stations(OUTER)
    holder, contained = step_for(stations, 58.0, 12.0)

    assert holder[2]["id"] == "0:outer:3"
    assert contained is True


def test_a_keyway_straddling_a_step_boundary_is_recognised():
    """Живой паз 46..68: начинается на Ø35, заканчивается на Ø30."""
    stations = steps_with_stations(OUTER)
    holder, contained = step_for(stations, 46.0, 22.0)

    assert holder[2]["id"] == "0:outer:3"
    assert contained is False


# ── Что видит оператор ───────────────────────────────────────────────────────


def test_the_live_keyways_are_reported_as_straddling_and_mispatterned():
    body = {
        "outer": OUTER,
        "keyways": [
            {"axial_start_mm": 46.0, "length_mm": 22.0, "width_mm": 3.5, "depth_mm": 3.0},
            {"axial_start_mm": 78.0, "length_mm": 25.0, "width_mm": 4.0, "depth_mm": 4.0},
        ],
    }
    unresolved: list[str] = []

    summary = ground_keyways(body, unresolved)

    assert summary["examined"] == 2
    assert summary["straddling"] == 2
    straddling = [item for item in unresolved if "выходит за ступень" in item]
    assert len(straddling) == 2
    # Названы обе величины: где паз и какая это ступень.
    assert "Ø30" in straddling[0]
    assert "Ø25" in straddling[1]
    # Паз привязан к ступени, а не висит в воздухе.
    assert body["keyways"][0]["on_section_id"] == "0:outer:3"
    assert all(item["review_required"] for item in body["keyways"])


def test_a_width_that_disagrees_with_the_table_is_reported_not_rewritten():
    """Подстановка табличного значения ломала сборку и угадывала.

    Ширина 3,5 → 8 мм — единственное, что изменилось между последним успешным
    прогоном и первым, где cad-kernel ответил HTTP 500: тело детали перестало
    строиться вообще. И даже без этого подстановка неверна по существу —
    расхождение означает, что неверно прочитано ЧТО-ТО одно: ширина паза,
    диаметр ступени или её положение. Выбирая ширину, мы гадаем и отправляем
    в ядро геометрию, которой на чертеже нет.
    """
    body = {
        "outer": OUTER,
        "keyways": [{"axial_start_mm": 58.0, "length_mm": 12.0, "width_mm": 3.5, "depth_mm": 4.0}],
    }
    unresolved: list[str] = []

    ground_keyways(body, unresolved)

    assert body["keyways"][0]["width_mm"] == 3.5  # прочитанное остаётся
    assert body["keyways"][0]["standard_ref"] == "ГОСТ 23360"
    assert body["keyways"][0]["review_required"] is True
    assert any("ГОСТ 23360 даёт 8 мм" in item for item in unresolved)


def test_a_width_backed_by_evidence_is_kept_and_shown_to_a_human():
    """Нестандартные шпонки существуют: прочитанное с подтверждением не трогаем."""
    body = {
        "outer": OUTER,
        "keyways": [
            {
                "axial_start_mm": 58.0,
                "length_mm": 12.0,
                "width_mm": 3.5,
                "depth_mm": 4.0,
                "evidence": [{"image_index": 0, "raw_text": "3,5"}],
            }
        ],
    }
    unresolved: list[str] = []

    ground_keyways(body, unresolved)

    assert body["keyways"][0]["width_mm"] == 3.5
    assert body["keyways"][0]["review_required"] is True
    assert any("проверьте выноску" in item for item in unresolved)


def test_a_standard_keyway_passes_without_a_word():
    body = {
        "outer": OUTER,
        "keyways": [{"axial_start_mm": 58.0, "length_mm": 12.0, "width_mm": 8.0, "depth_mm": 4.0}],
    }
    unresolved: list[str] = []

    ground_keyways(body, unresolved)

    assert unresolved == []
    assert body["keyways"][0]["width_mm"] == 8.0


def test_a_part_without_a_read_profile_changes_nothing():
    """Не с чем сверять — молчим, а не выдумываем ступень."""
    body = {"outer": [], "keyways": [{"axial_start_mm": 1.0, "length_mm": 5.0, "width_mm": 2.0}]}
    unresolved: list[str] = []

    ground_keyways(body, unresolved)

    assert unresolved == []


# ── Глубина сравнивается со своей ступенью ──────────────────────────────────


def test_a_keyway_deeper_than_its_own_step_is_refused():
    """Проверка брала радиус САМОЙ ТОЛСТОЙ ступени, а не той, где паз лежит.

    Паз глубиной 12 мм на ступени Ø22 проходил только потому, что где-то на
    валу есть Ø35. Такую деталь построить нельзя: паз прорезал бы вал насквозь.
    """
    import pytest

    from app.ai.cad_recognize.spec_vectorize import SpecBody

    with pytest.raises(ValueError, match="deeper than"):
        SpecBody(
            outer=[
                {"diameter_mm": 35.0, "length_mm": 20.0},
                {"diameter_mm": 22.0, "length_mm": 30.0},
            ],
            keyways=[
                {"axial_start_mm": 30.0, "length_mm": 12.0, "width_mm": 6.0, "depth_mm": 12.0}
            ],
        )


def test_the_same_keyway_on_the_thick_step_is_allowed():
    from app.ai.cad_recognize.spec_vectorize import SpecBody

    body = SpecBody(
        outer=[
            {"diameter_mm": 35.0, "length_mm": 20.0},
            {"diameter_mm": 22.0, "length_mm": 30.0},
        ],
        keyways=[{"axial_start_mm": 4.0, "length_mm": 12.0, "width_mm": 6.0, "depth_mm": 12.0}],
    )

    assert body.keyways[0].depth_mm == 12.0


# ── Холостой прогон должен быть виден ───────────────────────────────────────


def test_a_body_without_keyways_reports_that_it_examined_nothing():
    """Вызов, поставленный ДО того, как пазы попадают в тело, молчал.

    Поймано живьём: поля on_section_id и review_required появились в ответе,
    но пустые — функция возвращалась на первой строке, потому что keyways в
    теле ещё не было. Сводка делает такой холостой проход видимым в журнале
    вместо того, чтобы выглядеть как «проверено, всё хорошо».
    """
    summary = ground_keyways({"outer": OUTER, "keyways": []}, [])

    assert summary["examined"] == 0


def test_the_summary_counts_what_actually_happened():
    body = {
        "outer": OUTER,
        "keyways": [
            {"axial_start_mm": 58.0, "length_mm": 12.0, "width_mm": 3.5, "depth_mm": 4.0},
            {"axial_start_mm": 20.0, "length_mm": 8.0, "width_mm": 8.0, "depth_mm": 4.0},
        ],
    }

    summary = ground_keyways(body, [])

    assert summary["examined"] == 2
    assert summary["flagged"] == 1
    assert summary["straddling"] == 0


# ── Место вызова: тесты функции его не проверяли ────────────────────────────


def test_grounding_runs_on_the_shared_tail_of_every_read_path():
    """Дефект был не в функции, а в том, ГДЕ её звали.

    Сначала вызов стоял в сборке тела, до того как туда попадают пазы, — и
    молча возвращался на первой строке. Потом, после переноса ниже, он работал,
    но записывал пустой `on_section_id`: идентификаторы ступеней проставляются
    позже, в `assign_stable_feature_ids`. И всё это время сверку проходил
    только фрагментный путь — полное чтение листа шло мимо неё целиком.

    Общий хвост `_finalize_spec` снимает все три вопроса разом: он идёт после
    простановки идентификаторов и через него выходят ВСЕ пути чтения.
    """
    import inspect

    from app.ai.cad_recognize import spec_fragments

    tail = inspect.getsource(spec_fragments._finalize_spec)

    assert "assign_stable_feature_ids" in tail
    assert tail.index("assign_stable_feature_ids") < tail.index("_ground_all_keyways")

    read = inspect.getsource(spec_fragments.read_spec_best_effort)
    # Каждый выход чтения — через общий хвост, иначе путь снова окажется мимо.
    assert read.count("return await _finalize_spec(") == 3
    assert "return fragments" not in read


def test_every_body_is_grounded_not_just_the_main_view():
    body = {
        "outer": OUTER,
        "keyways": [{"axial_start_mm": 58.0, "length_mm": 12.0, "width_mm": 3.5, "depth_mm": 4.0}],
    }
    spec = {"main_view": dict(body), "parts": [dict(body)]}

    from app.ai.cad_recognize.spec_fragments import _ground_all_keyways

    total = _ground_all_keyways(spec)

    assert total["examined"] == 2
    assert spec["main_view"]["keyways"][0]["review_required"] is True
    assert spec["parts"][0]["keyways"][0]["review_required"] is True


# ── Корень целого класса жалоб: профиль короче листа ────────────────────────


def test_a_profile_shorter_than_the_sheet_is_reported():
    """Замерено на `z4-r4.jpg`: ступени дают 153 мм при габарите 195.

    Сорок два миллиметра недочитанной длины сдвигают ВСЁ, что привязано к
    осевой координате: пазы оказываются на соседних ступенях, канавки — не
    там, где нарисованы. Оператор видит следствие («пазы не на тех
    элементах») и правит не ту величину. Проверки на это не было вовсе.
    """
    from app.ai.cad_recognize.spec_fragments import _flag_profile_length_mismatch

    spec = {
        "main_view": {"outer": OUTER},  # 153 мм суммарно
        "dimensions": [{"value": "195"}, {"value": "22"}],
    }

    _flag_profile_length_mismatch(spec)

    assert any("профиль короче листа" in item for item in spec["unresolved"])
    assert any("153" in item and "195" in item for item in spec["unresolved"])


def test_a_profile_that_matches_the_sheet_says_nothing():
    from app.ai.cad_recognize.spec_fragments import _flag_profile_length_mismatch

    spec = {
        "main_view": {"outer": OUTER},
        "dimensions": [{"value": "153"}, {"value": "22"}],
    }

    _flag_profile_length_mismatch(spec)

    assert spec.get("unresolved", []) == []


def test_a_diameter_callout_is_not_mistaken_for_the_overall_length():
    """Ø470 — не длина. Линейные выноски отбираются тем же проходом, что везде."""
    from app.ai.cad_recognize.spec_fragments import _flag_profile_length_mismatch

    spec = {
        "main_view": {"outer": OUTER},
        "dimensions": [{"value": "Ø470"}, {"value": "150"}],
    }

    _flag_profile_length_mismatch(spec)

    assert spec.get("unresolved", []) == []


def test_the_straddle_message_does_not_blame_the_keyway_when_the_step_is_suspect():
    """Паз, начинающийся ровно на границе ступени, прочитан скорее верно.

    Виновата тогда длина ступени, и посылать человека править паз — значит
    посылать его править не то.
    """
    body = {
        "outer": OUTER,
        # Ступень Ø25 идёт 78..93; паз начинается ровно с её начала.
        "keyways": [{"axial_start_mm": 78.0, "length_mm": 25.0, "width_mm": 8.0, "depth_mm": 4.0}],
    }
    unresolved: list[str] = []

    ground_keyways(body, unresolved)

    message = next(item for item in unresolved if "выходит за ступень" in item)
    assert "под подозрением длина ступени" in message


# ── Отказ ядра: жертвуем спорным вырезом, а не всей деталью ─────────────────


def test_the_retry_drops_only_the_cuts_the_reader_itself_disputed():
    """Правило по признаку вывести нечем — судьёй остаётся ядро.

    Замерено: паз 8 мм через уступ Ø35/Ø30 даёт невалидное тело, он же внутри
    одной ступени строится, а на эталонном detal_126 через уступ Ø80→Ø72
    спокойно проходит паз 12 мм. Одного правила из этого не следует, а запрет
    по неверному признаку выбрасывает законную геометрию.

    Поэтому при отказе ядра жертвуют тем, что ЧТЕНИЕ САМО пометило спорным.
    Ничего не выдумывается: спорное откладывается человеку, а остальная деталь
    перестаёт пропадать целиком из-за одного выреза.
    """
    from app.ai.cad_ir.feature_tree import Feature3D, FeatureTreeCandidate
    from app.tasks.cad_trace import _candidate_without_disputed_cuts

    def keyway(start: float) -> Feature3D:
        return Feature3D(
            kind="keyway",
            params={"axial_start_mm": start, "length_mm": 20.0, "width_mm": 8.0, "depth_mm": 4.0},
        )

    candidate = FeatureTreeCandidate(
        label="test",
        score=1.0,
        features=[keyway(46.0), keyway(120.0), Feature3D(kind="chamfer", params={})],
    )
    spec = {
        "main_view": {
            "keyways": [
                {"axial_start_mm": 46.0, "review_required": True},
                {"axial_start_mm": 120.0},
            ]
        }
    }

    retry, dropped = _candidate_without_disputed_cuts(candidate, spec)

    kinds = [(f.kind, (f.params or {}).get("axial_start_mm")) for f in retry.features]
    assert ("keyway", 46.0) not in kinds
    assert ("keyway", 120.0) in kinds  # бесспорный паз остаётся
    assert ("chamfer", None) in kinds  # прочая геометрия не трогается
    assert len(dropped) == 1 and "46" in dropped[0]


def test_nothing_disputed_means_the_refusal_stays_a_refusal():
    """Иначе отказ ядра превратился бы в тихо урезанную деталь."""
    from app.ai.cad_ir.feature_tree import Feature3D, FeatureTreeCandidate
    from app.tasks.cad_trace import _candidate_without_disputed_cuts

    candidate = FeatureTreeCandidate(
        label="test",
        score=1.0,
        features=[Feature3D(kind="keyway", params={"axial_start_mm": 46.0})],
    )

    assert _candidate_without_disputed_cuts(candidate, {"main_view": {"keyways": []}}) is None


def test_the_flag_never_enters_the_feature_params():
    """Параметры входят в канонический хэш — лишнее поле ломает golden-гейт.

    Проверено: положив пометку в params, я уронил детерминированный
    четырёхдоменный гейт EMG. Она берётся из спека.
    """
    import inspect

    from app.ai import cad_solid

    assert "review_required" not in inspect.getsource(cad_solid._cut_features)
