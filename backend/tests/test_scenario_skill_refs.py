"""Conformance validator: every skill referenced by a YAML scenario must be
either a real registry skill, a recognised runner control-flow directive, or an
explicitly-declared (documented) capability gap.

This is the honesty guard the existing ``test_all_scenarios_yaml.py`` lacks —
that test mocks ``_call_skill`` so it never notices a reference to a skill that
does not exist. This test makes the decorative references explicit and fails on
any NEW unknown reference (drift), so scenarios cannot silently rot again.
"""

from __future__ import annotations

from pathlib import Path

import yaml

SCENARIOS_DIR = Path(__file__).parents[2] / "aiagent" / "scenarios"


# Runner-level control-flow / UI directives — intentionally NOT backend skills.
# The scenario engine interprets these as gates / UI panels, not HTTP skills.
CONTROL_FLOW_SKILLS = {
    "approval.wait",
    "approval.wait_chain",
    "approval.create_chain",
    "ui.show_anomaly_panel",
    "ui.show_workspace",
}

# Declared capability gaps: scenarios that reference a not-yet-implemented
# drawing/tooling skill surface. Documented here on purpose so the gap is
# VISIBLE and tracked rather than hidden behind a universal test mock. Implement
# the backend skill, then remove it from this set (the test below enforces that
# this set stays minimal — no stale entries).
UNIMPLEMENTED_SKILLS = {
    "drawing.get",
    "drawing.analyze",
    "drawing.link_tool",
    "tool_catalog.suggest",
    "anomaly.filter",
}


def _valid_registry_skills() -> set[str]:
    # The same merged catalog the scenario runner resolves against:
    # active registry + capability names (image_studio, engineering, …).
    from app.ai.scenario_runner import _skill_catalog

    return {k.replace("__", ".") for k in _skill_catalog()}


def _scenario_skill_refs() -> dict[str, set[str]]:
    """filename → set of skill names referenced anywhere in the scenario."""

    def walk(node, acc: set[str]) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "skill" and isinstance(v, str) and v:
                    acc.add(v)
                else:
                    walk(v, acc)
        elif isinstance(node, list):
            for item in node:
                walk(item, acc)

    out: dict[str, set[str]] = {}
    for path in sorted(SCENARIOS_DIR.glob("*.yml")):
        data = yaml.safe_load(path.read_text()) or {}
        refs: set[str] = set()
        walk(data, refs)
        out[path.name] = refs
    return out


def test_every_scenario_skill_is_known():
    """No scenario may reference a skill that is not real, not a control-flow
    directive, and not a declared gap."""
    valid = _valid_registry_skills()
    allowed = valid | CONTROL_FLOW_SKILLS | UNIMPLEMENTED_SKILLS

    offenders: dict[str, list[str]] = {}
    for fname, refs in _scenario_skill_refs().items():
        unknown = sorted(r for r in refs if r not in allowed)
        if unknown:
            offenders[fname] = unknown

    assert not offenders, (
        "Scenarios reference unknown skills (add a real skill, fix the name, or "
        f"declare the gap in UNIMPLEMENTED_SKILLS): {offenders}"
    )


def test_no_stale_declared_gaps():
    """UNIMPLEMENTED_SKILLS must not list skills that are now real or unused —
    keeps the documented-gap list honest as the backend grows."""
    valid = _valid_registry_skills()
    all_refs: set[str] = set()
    for refs in _scenario_skill_refs().values():
        all_refs |= refs

    now_real = sorted(s for s in UNIMPLEMENTED_SKILLS if s in valid)
    assert not now_real, f"Declared gaps are now real skills — remove them: {now_real}"

    unused = sorted(s for s in UNIMPLEMENTED_SKILLS if s not in all_refs)
    assert not unused, f"Declared gaps no longer referenced by any scenario: {unused}"


def test_control_flow_skills_not_in_registry():
    """Control-flow directives must stay pseudo-skills, never shadow a real skill."""
    valid = _valid_registry_skills()
    clash = sorted(s for s in CONTROL_FLOW_SKILLS if s in valid)
    assert not clash, f"Control-flow directive collides with a real skill: {clash}"
