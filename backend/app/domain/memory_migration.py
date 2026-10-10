"""E43: move old shared memory to the scope its provenance proves.

Memory written before scoped memory existed sits in ``project`` scope — visible
to everyone — including chat turns, which are one person's conversation. This
module inventories every fact outside a personal scope and sorts it into:

* ``proven_owner`` — provenance names one person (a chat session that still
  exists, a work order); target ``owner:<sub>``.
* ``proven_shared`` — a human decision made it company knowledge (an approved
  promotion, an approved/proposed web source registry entry); stays.
* ``system_state`` — bookkeeping of background jobs, no content; stays.
* ``derived`` — generated from documents (graph insights quoting document
  titles); its visibility is its sources', which ``project`` scope cannot
  express — quarantined with the source ids recorded.
* ``ambiguous`` — nothing proves an owner or a shared intent; quarantined.
  Never assigned by last reader, nearest name or a model's guess.

Quarantine is the scope ``quarantine:<old scope>``: no read path matches it
(API scopes, agent SQL policies), and it can be released by an owner's
decision. Every move records the old scope, the rule and a hash of the fact
in ``metadata.scope_migration``.

Dry-run is the default and writes nothing. Applying moves at most ``batch``
facts after ``after`` (resume cursor) and is idempotent: a fact already moved
is no longer outside a personal scope. Production apply needs the data
owner's reviewed approval (``approved_by``) — it is not a routine run.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatSession, MemoryFact, WorkOrder

RULE_VERSION = "e43-v1"
_SHARED_SCOPES = ("project", "global")
_SYSTEM_KINDS = frozenset({"graph_analytics_state", "idle_reflection_state"})


@dataclass
class Decision:
    fact_id: str
    kind: str
    scope: str
    category: str
    rule: str
    target_scope: str | None
    source_ids: list[str] = field(default_factory=list)
    fact_hash: str = ""


@dataclass
class Report:
    rule_version: str
    generated_at: str
    decisions: list[Decision]

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for decision in self.decisions:
            result[decision.category] = result.get(decision.category, 0) + 1
        return dict(sorted(result.items()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_version": self.rule_version,
            "generated_at": self.generated_at,
            "counts": self.counts(),
            "decisions": [asdict(d) for d in self.decisions],
        }


def _fact_hash(fact: MemoryFact) -> str:
    body = {
        "scope": fact.scope,
        "kind": fact.kind,
        "title": fact.title,
        "summary": fact.summary,
        "metadata": fact.metadata_ or {},
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


async def _session_owner(db: AsyncSession, session_id: Any) -> str | None:
    try:
        sid = uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return None
    return await db.scalar(select(ChatSession.user_key).where(ChatSession.id == sid))


async def _work_order_owner(db: AsyncSession, order_id: Any) -> str | None:
    try:
        oid = uuid.UUID(str(order_id))
    except (TypeError, ValueError):
        return None
    return await db.scalar(select(WorkOrder.owner_key).where(WorkOrder.id == oid))


async def classify(db: AsyncSession, fact: MemoryFact) -> Decision:
    meta = fact.metadata_ if isinstance(fact.metadata_, dict) else {}
    provenance = fact.provenance if isinstance(fact.provenance, dict) else {}
    base = {
        "fact_id": str(fact.id),
        "kind": fact.kind,
        "scope": fact.scope,
        "fact_hash": _fact_hash(fact),
    }
    quarantine = f"quarantine:{fact.scope}"

    if fact.kind in _SYSTEM_KINDS:
        return Decision(**base, category="system_state", rule="job_bookkeeping", target_scope=None)
    if fact.kind == "verified_fact" and meta.get("promotion_status") == "approved":
        return Decision(
            **base, category="proven_shared", rule="approved_promotion", target_scope=None
        )
    if fact.kind == "pinned_fact" and meta.get("owner_key"):
        # Shared pins need a human manager (api/memory.py pin_memory_fact),
        # who is recorded as owner_key: a deliberate share.
        return Decision(
            **base,
            category="proven_shared",
            rule="manager_pinned_shared",
            target_scope=None,
            source_ids=[str(meta["owner_key"])],
        )
    if fact.kind == "proposed_fact":
        # The review queue: reviewers must see it; a decision moves it.
        return Decision(**base, category="review_queue", rule="pending_review", target_scope=None)
    if fact.kind == "web_source" and meta.get("url"):
        return Decision(
            **base, category="proven_shared", rule="web_source_registry", target_scope=None
        )

    for key, resolver, rule in (
        ("session_id", _session_owner, "chat_session_owner"),
        ("work_order_id", _work_order_owner, "work_order_owner"),
    ):
        value = meta.get(key) or provenance.get(key)
        if value:
            owner = await resolver(db, value)
            if owner:
                return Decision(
                    **base,
                    category="proven_owner",
                    rule=rule,
                    target_scope=f"owner:{owner}",
                    source_ids=[str(value)],
                )
            return Decision(
                **base,
                category="ambiguous",
                rule=f"{key}_not_found",
                target_scope=quarantine,
                source_ids=[str(value)],
            )

    if fact.kind == "graph_insight":
        node_ids = [
            str(node.get("node_id"))
            for node in meta.get("nodes") or []
            if isinstance(node, dict) and node.get("node_id")
        ]
        for key in ("source_node_id", "target_node_id"):
            if meta.get(key):
                node_ids.append(str(meta[key]))
        return Decision(
            **base,
            category="derived",
            rule="graph_insight_quotes_documents",
            target_scope=quarantine,
            source_ids=node_ids,
        )
    return Decision(**base, category="ambiguous", rule="no_provenance", target_scope=quarantine)


async def build_report(db: AsyncSession) -> Report:
    facts = (
        await db.scalars(
            select(MemoryFact)
            .where(MemoryFact.scope.in_(_SHARED_SCOPES), MemoryFact.status == "active")
            .order_by(MemoryFact.id)
        )
    ).all()
    decisions = [await classify(db, fact) for fact in facts]
    return Report(
        rule_version=RULE_VERSION,
        generated_at=datetime.now(UTC).isoformat(),
        decisions=decisions,
    )


async def apply_report(
    db: AsyncSession,
    report: Report,
    *,
    approved_by: str,
    batch: int = 100,
    after: str | None = None,
) -> dict[str, Any]:
    """Move up to ``batch`` facts after ``after``; returns the next cursor.

    A fact whose content changed since the report (hash mismatch) is skipped,
    not moved on a decision made about other content.
    """
    if not approved_by:
        raise ValueError("applying needs the data owner's approval (approved_by)")
    moved = skipped = 0
    cursor = after
    pending = [
        d
        for d in sorted(report.decisions, key=lambda d: d.fact_id)
        if d.target_scope and (after is None or d.fact_id > after)
    ]
    for decision in pending[:batch]:
        cursor = decision.fact_id
        fact = await db.get(MemoryFact, uuid.UUID(decision.fact_id), with_for_update=True)
        if fact is None or fact.scope != decision.scope or _fact_hash(fact) != decision.fact_hash:
            skipped += 1
            continue
        fact.metadata_ = {
            **(fact.metadata_ or {}),
            "scope_migration": {
                "from": fact.scope,
                "to": decision.target_scope,
                "category": decision.category,
                "rule": decision.rule,
                "rule_version": report.rule_version,
                "source_ids": decision.source_ids,
                "fact_hash": decision.fact_hash,
                "approved_by": approved_by,
                "at": datetime.now(UTC).isoformat(),
            },
        }
        fact.scope = decision.target_scope
        moved += 1
    await db.commit()
    more = len(pending) > batch
    return {"moved": moved, "skipped": skipped, "next_after": cursor if more else None}
