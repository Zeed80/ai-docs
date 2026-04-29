"""Fast checks for manufacturing technology models and graph integration."""

import json
import uuid
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import (
    KnowledgeEdge,
    KnowledgeNode,
    ManufacturingNormEstimate,
    ManufacturingOperation,
    ManufacturingOperationTemplate,
    ManufacturingProcessPlan,
    ManufacturingResource,
    ManufacturingCheckResult,
    TechnologyCorrection,
    TechnologyLearningRule,
)
from app.api.technology import (
    _build_process_plan_checks,
    _draft_operations,
    _estimate_operation_norm,
    _learning_suggestion,
    _operation_type,
)


def test_manufacturing_technology_tables_are_registered() -> None:
    expected = {
        "manufacturing_resources",
        "manufacturing_process_plans",
        "manufacturing_operations",
        "manufacturing_norm_estimates",
        "manufacturing_operation_templates",
        "manufacturing_check_results",
        "technology_corrections",
        "technology_learning_rules",
    }

    assert expected.issubset(Base.metadata.tables)


def test_process_plan_operations_resources_and_norms_can_persist() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        machine = ManufacturingResource(
            resource_type="machine",
            name="Токарный станок 16К20",
            model="16К20",
        )
        tool = ManufacturingResource(
            resource_type="tool",
            name="Резец проходной",
            code="T-001",
        )
        fixture = ManufacturingResource(
            resource_type="fixture",
            name="Патрон трехкулачковый",
        )
        plan = ManufacturingProcessPlan(
            product_name="Вал",
            product_code="VAL-001",
            standard_system="ЕСТД",
            material="Сталь 40Х",
        )
        session.add_all([machine, tool, fixture, plan])
        session.flush()

        operation = ManufacturingOperation(
            process_plan_id=plan.id,
            sequence_no=10,
            operation_code="010",
            name="Токарная черновая",
            operation_type="turning",
            machine_resource_id=machine.id,
            tool_resource_id=tool.id,
            fixture_resource_id=fixture.id,
            machine_minutes=18,
            labor_minutes=22,
        )
        session.add(operation)
        session.flush()

        estimate = ManufacturingNormEstimate(
            process_plan_id=plan.id,
            operation_id=operation.id,
            machine_minutes=18,
            labor_minutes=22,
            confidence=0.8,
            method="engineer_estimate",
        )
        session.add(estimate)
        session.commit()

        saved = session.get(ManufacturingOperation, operation.id)
        assert saved is not None
        assert saved.process_plan.product_name == "Вал"
        assert saved.machine_resource.name == "Токарный станок 16К20"
        assert saved.tool_resource.name == "Резец проходной"
        assert saved.fixture_resource.name == "Патрон трехкулачковый"
        assert session.get(ManufacturingNormEstimate, estimate.id).confidence == 0.8


def test_technology_entities_can_be_represented_in_graph_memory() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        plan = ManufacturingProcessPlan(product_name="Корпус", standard_system="ЕСТД")
        machine = ManufacturingResource(resource_type="machine", name="Фрезерный центр HAAS")
        session.add_all([plan, machine])
        session.flush()

        plan_node = KnowledgeNode(
            node_type="process_plan",
            title=plan.product_name,
            canonical_key=f"process_plan:{plan.id}",
            entity_type="manufacturing_process_plan",
            entity_id=plan.id,
        )
        machine_node = KnowledgeNode(
            node_type="machine",
            title=machine.name,
            canonical_key=f"machine:{machine.id}",
            entity_type="manufacturing_resource",
            entity_id=machine.id,
        )
        session.add_all([plan_node, machine_node])
        session.flush()

        edge = KnowledgeEdge(
            source_node_id=plan_node.id,
            target_node_id=machine_node.id,
            edge_type="uses_machine",
            confidence=0.9,
            reason="Техпроцесс использует станок",
        )
        session.add(edge)
        session.commit()

        saved = session.execute(select(KnowledgeEdge)).scalar_one()
        assert saved.source.title == "Корпус"
        assert saved.target.title == "Фрезерный центр HAAS"


def test_draft_operations_use_document_memory_mentions() -> None:
    mentions = {
        "material": ["Сталь 40Х"],
        "machine": ["токарный станок 16К20"],
        "tool": ["резец проходной"],
        "standard": ["ГОСТ 2789-73"],
    }

    operations = _draft_operations(
        mentions,
        machine_resource_id=None,
        tool_resource_id=None,
        fixture_resource_id=None,
    )

    assert _operation_type("токарный станок 16К20", "резец проходной") == "turning"
    assert [operation["operation_code"] for operation in operations] == ["010", "020", "090"]
    assert operations[1]["name"] == "Токарная"
    assert operations[2]["control_requirements"] == "ГОСТ 2789-73"


def test_operation_templates_and_check_results_can_persist() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        template = ManufacturingOperationTemplate(
            operation_type="turning",
            name="Токарная черновая",
            standard_system="ЕСТД",
            required_resource_types=["machine", "tool"],
            default_control_requirements="Контроль размеров после прохода",
        )
        plan = ManufacturingProcessPlan(product_name="Вал", standard_system="ЕСТД")
        session.add_all([template, plan])
        session.flush()
        check = ManufacturingCheckResult(
            process_plan_id=plan.id,
            check_code="missing_material",
            severity="critical",
            status="open",
            message="В техпроцессе не указан материал.",
            created_by="system",
        )
        session.add(check)
        session.commit()

        assert session.get(ManufacturingOperationTemplate, template.id).operation_type == "turning"
        assert session.get(ManufacturingCheckResult, check.id).severity == "critical"


def test_process_plan_checks_find_missing_technology_data() -> None:
    plan = ManufacturingProcessPlan(product_name="Вал", standard_system="ЕСТД")
    operation = ManufacturingOperation(
        process_plan_id=plan.id,
        sequence_no=20,
        operation_code="020",
        name="Токарная",
        operation_type="turning",
    )
    plan.operations = [operation]
    plan.norm_estimates = []

    checks = _build_process_plan_checks(plan)
    codes = {check["check_code"] for check in checks}

    assert "missing_material" in codes
    assert "missing_machine" in codes
    assert "missing_tool" in codes
    assert "missing_quality_control" in codes
    assert "missing_norm" in codes


def test_operation_norm_estimate_uses_conservative_heuristics() -> None:
    operation = ManufacturingOperation(
        process_plan_id=uuid.uuid4(),
        sequence_no=20,
        name="Токарная",
        operation_type="turning",
    )

    estimate = _estimate_operation_norm(operation, batch_size=5)

    assert estimate["setup_minutes"] == 3.0
    assert estimate["machine_minutes"] == 22.0
    assert estimate["labor_minutes"] == 28.0
    assert estimate["confidence"] == 0.55
    assert estimate["cutting_parameters"]["vc_m_min"] == 90


def test_technology_corrections_can_persist_and_generate_learning_suggestion() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        entity_id = uuid.uuid4()
        correction = TechnologyCorrection(
            entity_type="manufacturing_operation",
            entity_id=entity_id,
            field_name="operation_type",
            old_value="machining",
            new_value="turning",
            corrected_by="engineer",
        )
        session.add(correction)
        session.commit()

        saved = session.get(TechnologyCorrection, correction.id)
        assert saved.entity_id == entity_id
        assert saved.new_value == "turning"

    suggestion = _learning_suggestion(
        entity_type="manufacturing_operation",
        field_name="operation_type",
        old_value="machining",
        new_value="turning",
        occurrences=3,
        min_occurrences=3,
    )

    assert suggestion.confidence == 0.65
    assert suggestion.suggestion_type == "normalization_rule"


def test_technology_learning_rules_can_be_proposed_and_activated() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        rule = TechnologyLearningRule(
            rule_type="normalization_rule",
            entity_type="manufacturing_operation",
            field_name="operation_type",
            match_old_value="machining",
            replacement_value="turning",
            confidence=0.75,
            occurrences=5,
            status="proposed",
        )
        session.add(rule)
        session.commit()

        saved = session.get(TechnologyLearningRule, rule.id)
        saved.status = "active"
        saved.activated_by = "chief_technologist"
        session.commit()

        active = session.get(TechnologyLearningRule, rule.id)
        assert active.status == "active"
        assert active.activated_by == "chief_technologist"


def test_technology_regression_cases_keep_expected_draft_behavior() -> None:
    for path in Path("docs/technology-regression-cases").glob("*.json"):
        case = json.loads(path.read_text(encoding="utf-8"))
        operations = _draft_operations(
            case["mentions"],
            machine_resource_id=None,
            tool_resource_id=None,
            fixture_resource_id=None,
        )
        operation_codes = [operation["operation_code"] for operation in operations]

        assert operation_codes == case["expected_operations"]
        assert operations[1]["operation_type"] == case["expected_operation_type"]
