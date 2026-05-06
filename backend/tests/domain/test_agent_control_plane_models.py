from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai.policy_engine import classify_skill_risk
from app.db.base import Base
from app.db.models import (
    AgentConfigProposal,
    AgentCron,
    AgentPlugin,
    AgentTask,
    AgentTeam,
    CapabilityProposal,
    MemoryFact,
)


def test_agent_control_plane_tables_are_registered() -> None:
    expected = {
        "agent_config_proposals",
        "agent_tasks",
        "agent_teams",
        "agent_crons",
        "agent_plugins",
        "capability_proposals",
        "memory_facts",
    }

    assert expected.issubset(Base.metadata.tables)


def test_agent_control_plane_models_can_persist_lifecycle_state() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        team = AgentTeam(name="Invoice squad", purpose="Проверка счетов")
        session.add(team)
        session.flush()

        task = AgentTask(
            objective="Проверить новый счет",
            role="invoice_specialist",
            team_id=team.id,
        )
        cron = AgentCron(
            schedule="0 9 * * *",
            prompt="Проверить просроченные approvals",
            description="Daily approval sweep",
        )
        plugin = AgentPlugin(
            plugin_key="supplier-tools@local",
            name="Supplier tools",
            version="0.1.0",
            manifest={"tools": []},
            risk_level="low",
        )
        proposal = AgentConfigProposal(
            setting_path="system_prompt",
            proposed_value={"value": "new prompt"},
            current_value={"value": None},
            reason="Проверка protected proposal",
            risk_level="critical",
            protected=True,
        )
        fact = MemoryFact(
            title="Поставщик Альфа",
            summary="Поставщик Альфа закреплен в памяти",
            source="user_pin",
            pinned=True,
        )
        capability = CapabilityProposal(
            title="Draft workspace tool",
            missing_capability="Нет таблицы",
            reason="Unknown skill",
            suggested_artifact="workspace_template",
            draft={"tool_name": "workspace.generated"},
        )
        session.add_all([task, cron, plugin, proposal, fact, capability])
        session.commit()

        saved_task = session.get(AgentTask, task.id)
        saved_fact = session.get(MemoryFact, fact.id)
        assert saved_task is not None
        assert saved_task.team_id == team.id
        assert saved_fact is not None
        assert saved_fact.pinned is True
        assert session.get(CapabilityProposal, capability.id).status == "draft"


def test_skill_risk_classification_matches_control_plane_labels() -> None:
    assert classify_skill_risk("memory.search") == "low"
    assert classify_skill_risk("email.search") == "medium"
    assert classify_skill_risk("email.send") == "high"
