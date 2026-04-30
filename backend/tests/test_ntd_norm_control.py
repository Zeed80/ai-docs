"""Fast checks for SQL-first NTD norm-control models and rules."""

import asyncio
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai.schemas import AIResponse, AITask, ProviderKind
from app.db.base import Base
from app.db.models import (
    Document,
    DocumentChunk,
    DocumentStatus,
    DocumentType,
    NormativeDocument,
    NormativeDocumentVersion,
    NormativeRequirement,
    NTDCheckFinding,
    NTDCheckRun,
    NTDControlSettings,
)
from app.domain.ntd import (
    NormativeRequirementCreate,
    NTDCheckAvailabilityResponse,
    NTDCheckRunRequest,
    NTDControlSettingsUpdate,
)
from app.domain.ntd_checker import (
    SemanticNTDResponse,
    build_ntd_findings,
    build_semantic_ntd_findings,
)
from app.domain.ntd_parser import detect_normative_metadata, parse_normative_text


def test_ntd_tables_are_registered() -> None:
    expected = {
        "ntd_control_settings",
        "normative_documents",
        "normative_document_versions",
        "normative_clauses",
        "normative_requirements",
        "ntd_check_runs",
        "ntd_check_findings",
        "graph_build_statuses",
    }

    assert expected.issubset(Base.metadata.tables)


def test_ntd_models_can_persist_requirement_and_finding() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        settings = NTDControlSettings(mode="manual", updated_by="admin")
        document = Document(
            file_name="process.txt",
            file_hash="ntd-model-hash",
            file_size=200,
            mime_type="text/plain",
            storage_path="documents/process.txt",
            doc_type=DocumentType.other,
            status=DocumentStatus.approved,
        )
        normative_doc = NormativeDocument(
            code="ГОСТ 3.1105-2011",
            title="ЕСТД. Формы и правила оформления документов",
            document_type="ГОСТ",
            status="active",
        )
        session.add_all([settings, document, normative_doc])
        session.flush()

        version = NormativeDocumentVersion(
            normative_document_id=normative_doc.id,
            version_label="2011",
            status="active",
        )
        session.add(version)
        session.flush()
        normative_doc.current_version_id = version.id

        requirement = NormativeRequirement(
            normative_document_id=normative_doc.id,
            requirement_code="ГОСТ 3.1105-2011:control",
            requirement_type="process_plan",
            applies_to=["other", "техпроцесс"],
            text="В технологическом документе должны быть указаны требования контроля.",
            required_keywords=["контроль"],
            severity="error",
        )
        check = NTDCheckRun(
            document_id=document.id,
            mode="manual",
            triggered_by="manual",
            status="completed",
        )
        session.add_all([requirement, check])
        session.flush()

        finding = NTDCheckFinding(
            check_id=check.id,
            document_id=document.id,
            normative_document_id=normative_doc.id,
            requirement_id=requirement.id,
            severity="error",
            finding_code="requirement_keywords_missing:control",
            message="Не найден контроль.",
            confidence=0.8,
        )
        session.add(finding)
        session.commit()

        saved = session.query(NTDCheckFinding).one()
        assert saved.requirement_id == requirement.id
        assert saved.status == "open"
        assert session.query(NTDControlSettings).one().mode == "manual"


def test_ntd_schemas_validate_modes_and_requirements() -> None:
    settings = NTDControlSettingsUpdate(mode="auto", updated_by="admin")
    requirement = NormativeRequirementCreate(
        normative_document_id="00000000-0000-0000-0000-000000000001",
        requirement_code="REQ-1",
        text="Документ должен содержать требования контроля.",
        required_keywords=["контроль"],
    )

    assert settings.mode == "auto"
    assert requirement.required_keywords == ["контроль"]
    availability = NTDCheckAvailabilityResponse(
        document_id="00000000-0000-0000-0000-000000000001",
        can_check=False,
        reasons=["ntd_requirements_not_configured"],
        active_requirements=0,
        has_text=True,
        mode="manual",
    )
    assert availability.reasons == ["ntd_requirements_not_configured"]
    semantic_request = NTDCheckRunRequest(
        document_id="00000000-0000-0000-0000-000000000001",
        semantic_ai=True,
        semantic_max_requirements=3,
    )
    assert semantic_request.semantic_ai is True
    assert semantic_request.semantic_max_requirements == 3


def test_ntd_build_findings_prefers_sql_requirements_and_evidence() -> None:
    document_id = uuid.uuid4()
    requirement_document_id = uuid.uuid4()
    document = Document(
        id=document_id,
        file_name="process.txt",
        file_hash="ntd-rule-hash",
        file_size=200,
        mime_type="text/plain",
        storage_path="documents/process.txt",
        doc_type=DocumentType.other,
        status=DocumentStatus.approved,
    )
    chunk = DocumentChunk(
        document_id=document.id,
        chunk_index=0,
        text="Техпроцесс. Материал Сталь 40Х. Операция токарная.",
    )
    requirement = NormativeRequirement(
        normative_document_id=requirement_document_id,
        requirement_code="REQ-CONTROL",
        requirement_type="process_plan",
        applies_to=["other"],
        text="В документе должен быть указан контроль.",
        required_keywords=["контроль"],
        severity="error",
    )
    check = NTDCheckRun(
        document_id=document.id,
        mode="manual",
        triggered_by="manual",
        status="completed",
    )

    findings = build_ntd_findings(check, document, chunk.text, [requirement])

    assert len(findings) == 2
    assert {finding.finding_code for finding in findings} == {
        "missing_normative_reference",
        "requirement_keywords_missing:REQ-CONTROL",
    }
    assert all(finding.evidence_text for finding in findings)


def test_semantic_ntd_findings_convert_ai_response_to_findings(monkeypatch) -> None:
    document_id = uuid.uuid4()
    requirement_document_id = uuid.uuid4()
    document = Document(
        id=document_id,
        file_name="semantic-process.txt",
        file_hash="ntd-semantic-hash",
        file_size=200,
        mime_type="text/plain",
        storage_path="documents/semantic-process.txt",
        doc_type=DocumentType.other,
        status=DocumentStatus.approved,
    )
    requirement = NormativeRequirement(
        id=uuid.uuid4(),
        normative_document_id=requirement_document_id,
        requirement_code="REQ-ROUGHNESS",
        requirement_type="process_plan",
        applies_to=["other"],
        text="Должна быть указана шероховатость.",
        required_keywords=[],
        severity="warning",
    )
    check = NTDCheckRun(
        id=uuid.uuid4(),
        document_id=document.id,
        mode="manual",
        triggered_by="manual",
        status="completed",
    )

    class FakeAIRouter:
        async def run(self, request):
            assert request.task == AITask.ENGINEERING_REASONING
            return AIResponse(
                task=AITask.ENGINEERING_REASONING,
                provider=ProviderKind.OLLAMA,
                model="fake",
                data=SemanticNTDResponse.model_validate(
                    {
                        "findings": [
                            {
                                "requirement_code": "REQ-ROUGHNESS",
                                "severity": "warning",
                                "finding_code": "missing_roughness",
                                "message": "Не указана шероховатость поверхности.",
                                "evidence_text": "Операция токарная.",
                                "recommendation": "Добавить Ra/Rz по ГОСТ 2789-73.",
                                "confidence": 0.74,
                            }
                        ]
                    }
                ),
            )

    monkeypatch.setattr("app.ai.router.AIRouter", FakeAIRouter)
    findings = asyncio.run(
        build_semantic_ntd_findings(
            check,
            document,
            "Операция токарная.",
            [requirement],
            max_requirements=1,
        )
    )

    assert len(findings) == 1
    assert findings[0].finding_code == "semantic:missing_roughness"
    assert findings[0].requirement_id == requirement.id
    assert findings[0].metadata_["source"] == "semantic_ai"


def test_ntd_parser_extracts_clauses_and_requirements() -> None:
    parsed = parse_normative_text(
        """
        1 Общие требования
        Документ должен содержать материал и маршрут обработки.
        1.1 Контроль
        Необходимо указать контроль и измерительный инструмент.
        2 Справочные данные
        Допускается добавлять пояснения.
        """,
        code="ГОСТ-ТЕСТ",
        default_requirement_type="process_plan",
    )

    assert [clause.clause_number for clause in parsed.clauses] == ["1", "1.1", "2"]
    assert len(parsed.requirements) == 2
    assert parsed.requirements[0].requirement_code == "ГОСТ-ТЕСТ:1"
    assert "материал" in parsed.requirements[0].required_keywords
    assert parsed.requirements[1].severity == "warning"


def test_ntd_parser_detects_metadata() -> None:
    meta = detect_normative_metadata(
        (
            "ГОСТ 3.1105-2011 Единая система технологической документации. "
            "Формы и правила оформления документов"
        ),
        fallback_title="fallback.pdf",
    )

    assert meta.code == "ГОСТ 3.1105-2011"
    assert meta.document_type == "ГОСТ"
    assert meta.version == "2011"
    assert "Единая система" in meta.title
