"""Machine-readable, versioned ЕСКД rule profile (C2).

A single source of truth for every ЕСКД check the deterministic validator
enforces: each rule carries a STABLE machine key (``rule_id``, independent of
message wording), the ГОСТ it enforces (with year), the specific clause, its
default severity, and a concrete FIX PATH the reviewer/agent can act on. The
profile is versioned (``ESKD_PROFILE_VERSION``) so a stored validation report
records which ruleset produced it — a rule tightening bumps the version, and
old reports remain interpretable.

``cad_validate`` looks rules up by the ``CadCheckCode`` value and stamps
``rule_id``/``fix_hint``/``norm_ref`` onto each issue via ``eskd_issue``.
Adding ЕСКД coverage = adding a rule here plus its check function — the
citation, level and fix path travel with the rule, not scattered across the
validator.
"""

from __future__ import annotations

from dataclasses import dataclass

# Bump on any rule change (new rule, tightened threshold, severity change) so
# a stored report's ``profile_version`` pins exactly which ruleset judged it.
ESKD_PROFILE_VERSION = "1.0.0"


@dataclass(frozen=True)
class EskdRule:
    rule_id: str          # stable key, e.g. "ESKD.2.303.line_weight"
    code: str             # CadCheckCode value this rule is emitted under
    gost: str             # citation with year, e.g. "ГОСТ 2.303-68"
    clause: str           # specific clause/table, e.g. "п. 2, табл. 1"
    level: int            # assurance-pipeline level (Ф7.1); ЕСКД = 4
    default_severity: str  # "error" | "warn" | "info"
    fix_hint: str         # concrete path to fix the violation


# Keyed by CadCheckCode value. Every ЕСКД-level check resolves through here.
_RULES: tuple[EskdRule, ...] = (
    EskdRule(
        rule_id="ESKD.2.303.line_weight",
        code="ESKD_LINE_WEIGHT",
        gost="ГОСТ 2.303-68",
        clause="п. 2, табл. 1",
        level=4,
        default_severity="warn",
        fix_hint="Сделайте осевые/размерные/штриховые линии тонкими "
                 "(тип линии в свойствах элемента).",
    ),
    EskdRule(
        rule_id="ESKD.2.302.scale",
        code="ESKD_SCALE_NONSTANDARD",
        gost="ГОСТ 2.302-68",
        clause="табл. 1",
        level=4,
        default_severity="warn",
        fix_hint="Приведите масштаб основной надписи к стандартному ряду "
                 "(1:1, 1:2, 1:2,5, 1:5, 2:1, 5:1 …).",
    ),
    EskdRule(
        rule_id="ESKD.2.301.format",
        code="ESKD_SHEET_FORMAT_UNKNOWN",
        gost="ГОСТ 2.301-68",
        clause="п. 1, табл. 1",
        level=4,
        default_severity="info",
        fix_hint="Выберите формат листа из ряда A0–A4 (подтверждение формата "
                 "в редакторе), либо задайте масштаб вручную.",
    ),
    EskdRule(
        rule_id="ESKD.2.104.title_block",
        code="ESKD_TITLE_BLOCK_INCOMPLETE",
        gost="ГОСТ 2.104-2006",
        clause="форма 1",
        level=4,
        default_severity="info",
        fix_hint="Заполните основную надпись: обозначение, наименование, "
                 "материал, масштаб, подписи.",
    ),
    EskdRule(
        rule_id="ESKD.2.109.no_contour",
        code="ESKD_NO_CONTOUR_GEOMETRY",
        gost="ГОСТ 2.109-73",
        clause="п. 1.1",
        level=4,
        default_severity="warn",
        fix_hint="Добавьте основную контурную геометрию — на чертеже нет "
                 "линий видимого контура.",
    ),
    EskdRule(
        rule_id="ESKD.2789.roughness",
        code="RA_INVALID",
        gost="ГОСТ 2789-73",
        clause="табл. 1",
        level=3,
        default_severity="warn",
        fix_hint="Приведите значение Ra к стандартному ряду ГОСТ 2789 "
                 "(…0,8; 1,6; 3,2; 6,3; 12,5…).",
    ),
    EskdRule(
        rule_id="ESKD.2.304.text_height",
        code="ESKD_TEXT_HEIGHT",
        gost="ГОСТ 2.304-81",
        clause="п. 1, табл. 1",
        level=4,
        default_severity="info",
        fix_hint="Приведите высоту шрифта к стандартному ряду "
                 "(2,5; 3,5; 5; 7; 10; 14 мм).",
    ),
    EskdRule(
        rule_id="ESKD.2.307.dimension_value",
        code="ESKD_DIMENSION_INCOMPLETE",
        gost="ГОСТ 2.307-2011",
        clause="п. 1.2",
        level=3,
        default_severity="warn",
        fix_hint="Проставьте числовое значение размера — размерная линия "
                 "без величины не допускается.",
    ),
)

RULES: dict[str, EskdRule] = {rule.code: rule for rule in _RULES}

# ГОСТ 2.304-81 nominal font heights (mm). A text height not near this series
# is flagged (only when the sheet scale is known, so px → mm is meaningful).
GOST_2304_TEXT_HEIGHTS_MM = (1.8, 2.5, 3.5, 5.0, 7.0, 10.0, 14.0, 20.0, 28.0, 40.0)


def rule_for(code: str) -> EskdRule | None:
    return RULES.get(code)
