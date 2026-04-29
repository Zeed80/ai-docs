"""Deterministic NTD norm-control helpers shared by API and tasks."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.db.models import Document, NTDCheckFinding, NTDCheckRun, NormativeRequirement


class SemanticNTDFindingCandidate(BaseModel):
    requirement_code: str | None = None
    severity: str = Field("warning", pattern="^(info|warning|error|critical)$")
    finding_code: str = "semantic_ntd_issue"
    message: str
    evidence_text: str | None = None
    recommendation: str | None = None
    confidence: float = Field(0.5, ge=0.0, le=1.0)


class SemanticNTDResponse(BaseModel):
    findings: list[SemanticNTDFindingCandidate] = Field(default_factory=list)


def build_ntd_findings(
    check: NTDCheckRun,
    document: Document,
    text: str,
    requirements: list[NormativeRequirement],
) -> list[NTDCheckFinding]:
    lower_text = text.lower()
    findings: list[NTDCheckFinding] = []
    if is_engineering_document(document, lower_text) and not has_standard_reference(lower_text):
        findings.append(
            NTDCheckFinding(
                check_id=check.id,
                document_id=document.id,
                severity="warning",
                finding_code="missing_normative_reference",
                message="В документе не найдена явная ссылка на НТД/ГОСТ/ОСТ/ТУ/СТП.",
                evidence_text=excerpt(text),
                recommendation="Укажите применимый нормативный документ или подтвердите, что ссылка не требуется.",
                confidence=0.75,
            )
        )

    for requirement in requirements:
        keywords = [str(item).strip().lower() for item in (requirement.required_keywords or [])]
        missing = [keyword for keyword in keywords if keyword and keyword not in lower_text]
        if not missing:
            continue
        findings.append(
            NTDCheckFinding(
                check_id=check.id,
                document_id=document.id,
                normative_document_id=requirement.normative_document_id,
                clause_id=requirement.clause_id,
                requirement_id=requirement.id,
                severity=requirement.severity,
                finding_code=f"requirement_keywords_missing:{requirement.requirement_code}",
                message=f"Не найдены обязательные признаки требования {requirement.requirement_code}: {', '.join(missing)}.",
                evidence_text=excerpt(text),
                recommendation=requirement.text,
                confidence=0.8,
                metadata_={"missing_keywords": missing},
            )
        )
    return findings


async def build_semantic_ntd_findings(
    check: NTDCheckRun,
    document: Document,
    text: str,
    requirements: list[NormativeRequirement],
    *,
    max_requirements: int = 8,
) -> list[NTDCheckFinding]:
    if not requirements:
        return []
    try:
        from app.ai.router import AIRouter
        from app.ai.schemas import AIRequest, AITask

        selected_requirements = requirements[:max_requirements]
        response = await AIRouter().run(
            AIRequest(
                task=AITask.ENGINEERING_REASONING,
                prompt=_semantic_prompt(document, text, selected_requirements),
                response_schema=SemanticNTDResponse,
                confidential=True,
                metadata={"document_id": str(document.id), "mode": "semantic_ntd_check"},
            )
        )
        data = response.data
        semantic_response = (
            data if isinstance(data, SemanticNTDResponse) else SemanticNTDResponse.model_validate(data)
        )
    except Exception:
        return []

    requirements_by_code = {requirement.requirement_code: requirement for requirement in requirements}
    findings: list[NTDCheckFinding] = []
    for candidate in semantic_response.findings:
        requirement = requirements_by_code.get(candidate.requirement_code or "")
        findings.append(
            NTDCheckFinding(
                check_id=check.id,
                document_id=document.id,
                normative_document_id=requirement.normative_document_id if requirement else None,
                clause_id=requirement.clause_id if requirement else None,
                requirement_id=requirement.id if requirement else None,
                severity=candidate.severity,
                finding_code=f"semantic:{candidate.finding_code}",
                message=candidate.message,
                evidence_text=excerpt(candidate.evidence_text or text),
                recommendation=candidate.recommendation or (requirement.text if requirement else None),
                confidence=candidate.confidence,
                metadata_={
                    "source": "semantic_ai",
                    "requirement_code": candidate.requirement_code,
                },
            )
        )
    return findings


def is_engineering_document(document: Document, lower_text: str) -> bool:
    if document.doc_type and document.doc_type.value in {"drawing", "other"}:
        return True
    markers = ("техпроцесс", "операция", "материал", "чертеж", "чертёж", "контроль")
    return any(marker in lower_text for marker in markers)


def has_standard_reference(lower_text: str) -> bool:
    return bool(re.search(r"\b(?:гост|ост|ту|стп|естд|ескд)\b", lower_text, re.I))


def excerpt(text: str, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _semantic_prompt(
    document: Document,
    text: str,
    requirements: list[NormativeRequirement],
) -> str:
    requirements_text = "\n".join(
        (
            f"- code: {requirement.requirement_code}\n"
            f"  type: {requirement.requirement_type}\n"
            f"  severity: {requirement.severity}\n"
            f"  text: {requirement.text}\n"
            f"  required_keywords: {requirement.required_keywords or []}"
        )
        for requirement in requirements
    )
    return f"""Ты выполняешь нормоконтроль инженерного документа по НТД.
Найди только содержательные нарушения требований, которые подтверждаются фрагментом документа.
Не дублируй замечания, если нарушение не подтверждается. Ответь строго JSON по схеме.

Документ: {document.file_name}

Требования НТД:
{requirements_text}

Текст документа:
{text[:8000]}

JSON:
{{
  "findings": [
    {{
      "requirement_code": "<code or null>",
      "severity": "info|warning|error|critical",
      "finding_code": "<short_code>",
      "message": "<краткое замечание>",
      "evidence_text": "<точный фрагмент документа>",
      "recommendation": "<что исправить>",
      "confidence": 0.0
    }}
  ]
}}"""
