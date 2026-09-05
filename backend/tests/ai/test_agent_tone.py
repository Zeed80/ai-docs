"""Ф7 (AGENT_AUTONOMY_ROADMAP.md): agent_tone is a pure response-style
suffix — never touches the base prompt, tools, or approval gates, only adds
wording/register guidance to the end of the system prompt actually sent to
the model for that turn.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.ai.agent_config import BuiltinAgentConfig
from app.ai.agent_loop import _TONE_STYLE_HINTS, AgentSession, _tone_style_hint


def _fake_session(*, system: str, tone: str, role_context: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        _system=system,
        _config=BuiltinAgentConfig(agent_tone=tone),
        _role_context=role_context,
    )


def test_tone_style_hint_neutral_is_none():
    """neutral (the default for every config that predates Ф7) adds no
    suffix at all — nobody who never touched agent_tone sees any prompt
    change from before this feature existed."""
    assert _tone_style_hint("neutral") is None


def test_tone_style_hint_known_tones_are_present_and_distinct():
    hints = {tone: _tone_style_hint(tone) for tone in ("friendly", "formal", "concise")}
    assert all(hints.values())
    assert len(set(hints.values())) == 3  # each tone reads differently


def test_effective_system_neutral_tone_adds_no_suffix():
    fake = _fake_session(system="BASE PROMPT", tone="neutral")
    result = AgentSession._effective_system(fake)
    for hint in _TONE_STYLE_HINTS.values():
        assert hint not in result
    assert result.startswith("BASE PROMPT\n\n")


def test_effective_system_friendly_tone_appends_style_hint():
    fake = _fake_session(system="BASE PROMPT", tone="friendly")
    result = AgentSession._effective_system(fake)
    assert result.startswith("BASE PROMPT\n\n")  # base prompt itself is untouched
    assert _TONE_STYLE_HINTS["friendly"] in result
    assert _TONE_STYLE_HINTS["formal"] not in result
    assert _TONE_STYLE_HINTS["concise"] not in result


def test_effective_system_formal_and_concise_also_append_their_own_hint():
    for tone in ("formal", "concise"):
        fake = _fake_session(system="BASE", tone=tone)
        result = AgentSession._effective_system(fake)
        assert _TONE_STYLE_HINTS[tone] in result


def test_effective_system_tone_hint_appears_before_role_context():
    """Ordering matters for readability, not correctness, but pins the
    established layout: base -> date -> tone -> per-turn role."""
    fake = _fake_session(system="BASE", tone="formal", role_context="Ты сейчас бухгалтер.")
    result = AgentSession._effective_system(fake)
    assert _TONE_STYLE_HINTS["formal"] in result
    assert "Ты сейчас бухгалтер." in result
    assert result.index(_TONE_STYLE_HINTS["formal"]) < result.index("Ты сейчас бухгалтер.")


def test_effective_system_still_includes_live_date_grounding_regardless_of_tone():
    """Ф7 must not regress the Ф-earlier live-date-grounding fix (see
    _effective_system's own docstring) — the date context line must survive
    the tone suffix being added."""
    from app.ai.agent_loop import _today_context

    fake = _fake_session(system="BASE", tone="concise")
    result = AgentSession._effective_system(fake)
    assert _today_context() in result
