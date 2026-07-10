"""Ф9 (Evidence Engine): resolve a CAD-validation issue's plain norm
citation (``ValidationIssueIR.norm_ref``, e.g. "ГОСТ 2.303-68") against the
ingested НТД corpus (``NormativeDocument`` — see ``app/db/models.py``), when
that standard has actually been ingested — attaches the real stored
document so a validation report cites verifiable, versioned data instead of
staying a bare string forever.

Deliberately document-level, not clause-level: the checks in
``cad_validate.py`` know WHICH standard they enforce, not which specific
clause number — matching a specific ``NormativeClause`` would require each
check to carry a clause number, which none currently do. Document-level
citation ("this comes from ГОСТ 2.303-68, ingested, version X") is still a
real step up from an unverifiable string and is what this resolves; a
follow-up can push citations down to clause granularity once checks track
clause numbers.

Purely additive and safe to skip: ``resolve_norm_citations`` never invents
text — when the corpus doesn't have a cited standard ingested,
``norm_clause_text`` simply stays ``None`` and the plain ``norm_ref`` string
is still a correct, useful citation on its own.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.cad_ir.schema import ValidationIssueIR
from app.db.models import NormativeDocument


def _doc_code_base(norm_ref: str) -> str:
    """"ГОСТ 2.303-68" -> "ГОСТ 2.303" — strip the year suffix so a citation
    matches whichever edition/version happens to be ingested."""
    return norm_ref.split("-")[0].strip()


async def resolve_norm_citations(
    issues: list[ValidationIssueIR], db: AsyncSession
) -> list[ValidationIssueIR]:
    refs = {i.norm_ref for i in issues if i.norm_ref and not i.norm_clause_text}
    if not refs:
        return issues

    bases = {ref: _doc_code_base(ref) for ref in refs}
    docs = (await db.execute(select(NormativeDocument))).scalars().all()
    resolved: dict[str, NormativeDocument] = {}
    for ref, base in bases.items():
        match = next((d for d in docs if d.code.startswith(base)), None)
        if match is not None:
            resolved[ref] = match

    if not resolved:
        return issues

    for issue in issues:
        doc = resolved.get(issue.norm_ref or "")
        if doc is None:
            continue
        issue.norm_clause_text = f"{doc.code} — {doc.title}" if doc.title else doc.code
    return issues
