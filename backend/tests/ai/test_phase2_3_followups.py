"""Follow-ups: clarify-on-gated-ambiguity, skill-success ranking."""

from __future__ import annotations

from app.ai import orchestrator_memory as om
from app.ai.orchestrator import (
    OrchestratorPlan,
    WorkerAssignment,
    WorkspaceOutputSpec,
    needs_clarification,
)
from app.ai.orchestrator_memory import SkillScore, rank_skills_by_success


def _plan(skills):
    return OrchestratorPlan(
        goal="g", intent="document_op",
        worker=WorkerAssignment(role="invoice_specialist", task="t", recommended_skills=skills),
        workspace=WorkspaceOutputSpec(channel="chat", output_type="text", required=False),
    )


# ── Clarify on gated + ambiguous ───────────────────────────────────────────────

def test_clarify_gated_vague_reference_asks():
    q = needs_clarification("отправь ему письмо", _plan(["email.send"]))
    assert q and "уточните" in q.lower()


def test_clarify_gated_short_no_target_asks():
    assert needs_clarification("утверди счёт", _plan(["invoice.approve"])) is not None


def test_no_clarify_gated_with_concrete_target():
    # Names a concrete target → proceed (approval gate still confirms later).
    assert needs_clarification(
        'отправь письмо в ООО "Ромашка" по счёту 145', _plan(["email.send"])) is None
    assert needs_clarification("утверди счёт 145", _plan(["invoice.approve"])) is None


def test_no_clarify_for_cheap_action():
    # Cheap (desktop) actions never block — they assume + show assumptions.
    assert needs_clarification("покажи это", _plan(["workspace.spec_table"])) is None


# ── Skill ranking by learned success ───────────────────────────────────────────

def test_rank_skills_by_success_promotes_reliable(monkeypatch):
    def fake_scores(skills):
        data = {
            "good": SkillScore("good", success=9, fail=1, avg_ms=0, last_at=0),
            "bad": SkillScore("bad", success=1, fail=9, avg_ms=0, last_at=0),
        }
        return [data[s] for s in skills if s in data]

    monkeypatch.setattr(om, "get_skill_scores", fake_scores)
    ranked = rank_skills_by_success(["bad", "good"])
    assert ranked == ["good", "bad"]


def test_rank_skills_untracked_stay_neutral(monkeypatch):
    def fake_scores(skills):
        return [SkillScore("bad", success=0, fail=5, avg_ms=0, last_at=0)
                for s in skills if s == "bad"]

    monkeypatch.setattr(om, "get_skill_scores", fake_scores)
    # 'new' has no history (neutral 0.5) → stays above the proven-bad skill,
    # and original order is preserved among neutral entries.
    ranked = rank_skills_by_success(["new1", "bad", "new2"])
    assert ranked.index("bad") == 2
    assert ranked.index("new1") < ranked.index("new2")


def test_rank_empty():
    assert rank_skills_by_success([]) == []


# ── Recipe penalised when its replayed result is corrected ─────────────────────

import pytest  # noqa: E402

from app.ai.orchestrator import AgentOrchestrator  # noqa: E402


def _orchestrator():
    async def _noop(_m):
        return None
    return AgentOrchestrator(_noop)


@pytest.mark.asyncio
async def test_recipe_penalised_when_corrected(monkeypatch):
    import app.ai.recipes as recipes
    calls: list = []

    async def fake_outcome(recipe_id, *, success, retire=False):
        calls.append((recipe_id, success))

    monkeypatch.setattr(recipes, "record_outcome", fake_outcome)
    sess = _orchestrator()
    sess._last_recipe_id = "rec-123"
    # A correction after a replayed recipe → penalise + clear.
    await sess._penalise_recipe_on_correction("нет, не так, по дате")
    assert calls == [("rec-123", False)]
    assert sess._last_recipe_id == ""


@pytest.mark.asyncio
async def test_recipe_not_penalised_for_new_request(monkeypatch):
    import app.ai.recipes as recipes
    calls: list = []

    async def fake_outcome(recipe_id, *, success, retire=False):
        calls.append((recipe_id, success))

    monkeypatch.setattr(recipes, "record_outcome", fake_outcome)
    sess = _orchestrator()
    sess._last_recipe_id = "rec-123"
    await sess._penalise_recipe_on_correction("покажи фрезы по поставщику")
    assert calls == []
    assert sess._last_recipe_id == "rec-123"  # kept — not a correction
