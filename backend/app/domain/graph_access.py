"""E40: which graph nodes and edges a user may see.

One policy for the graph API (node, neighborhood, path, review), memory search
and the agent's SQL (``aiw_rls_*`` functions in ``app.ai.sql_row_security``
implement the same rules):

* A node is visible when its source document is (``source_document_id``) and
  the entity it stands for is (``entity_type``/``entity_id``: a document, an
  invoice, an e-mail, an anomaly or approval about one of them).
* A node built only from mentions — an INN, an e-mail address, a name the
  regex found in a document; no source document, no entity — carries text out
  of the documents that mention it. It is visible when the user can see at
  least one of them. Such a node with no document behind it at all (made by
  hand, a supplier) is company data.
* An edge is visible when both ends are, its source document is and the
  document of its evidence span is.

Traversals expand only through visible nodes and edges, so a path through an
allowed node never reaches a closed neighbour. Admin and manager see every
document, as in ``app.domain.access``; personal mail stays its owner's.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import UserInfo
from app.db.models import (
    Document,
    EmailMessage,
    EntityMention,
    EvidenceSpan,
    Invoice,
    KnowledgeEdge,
    KnowledgeNode,
)

_AUTO_METHODS = frozenset({"deterministic_regex"})


def _auto_built(node: KnowledgeNode) -> bool:
    meta = node.metadata_ if isinstance(node.metadata_, dict) else {}
    return meta.get("method") in _AUTO_METHODS


class GraphAccess:
    """Per-request visibility with memoized document and node answers."""

    def __init__(self, db: AsyncSession, user: UserInfo) -> None:
        self.db = db
        self.user = user
        self._documents: dict[uuid.UUID, bool] = {}
        self._nodes: dict[uuid.UUID, bool] = {}
        self._hidden_mailboxes: set[str] | None = None
        self._clause_ready = False
        self._clause = None

    # ── documents and entities ──────────────────────────────────────────

    async def document_clause(self):
        if not self._clause_ready:
            from app.domain.access import document_visibility_filter

            self._clause = await document_visibility_filter(self.db, self.user)
            self._clause_ready = True
        return self._clause

    async def visible_documents(self, ids: Iterable[uuid.UUID | None]) -> set[uuid.UUID]:
        """The subset of ``ids`` this user may see; a deleted document is not."""
        wanted = {i for i in ids if i is not None}
        unknown = wanted - set(self._documents)
        if unknown:
            query = select(Document.id).where(Document.id.in_(unknown))
            clause = await self.document_clause()
            if clause is not None:
                query = query.where(clause)
            found = set(await self.db.scalars(query))
            for doc_id in unknown:
                self._documents[doc_id] = doc_id in found
        return {i for i in wanted if self._documents[i]}

    async def document_visible(self, doc_id: uuid.UUID | None) -> bool:
        return doc_id is None or doc_id in await self.visible_documents([doc_id])

    async def _mailbox_visible(self, mailbox: str | None) -> bool:
        if mailbox is None:
            return True
        if self._hidden_mailboxes is None:
            from app.domain.email_access import hidden_mailbox_names

            self._hidden_mailboxes = set(await hidden_mailbox_names(self.db, self.user))
        return mailbox not in self._hidden_mailboxes

    async def entity_visible(self, kind: str | None, entity_id, *, depth: int = 0) -> bool:
        if not kind or entity_id is None:
            return True
        kind = kind.lower()
        if kind == "document":
            return await self.document_visible(entity_id)
        if kind == "invoice":
            doc_id = await self.db.scalar(
                select(Invoice.document_id).where(Invoice.id == entity_id)
            )
            return doc_id is not None and await self.document_visible(doc_id)
        if kind in {"email", "email_message"}:
            mailbox = await self.db.scalar(
                select(EmailMessage.mailbox).where(EmailMessage.id == entity_id)
            )
            if mailbox is not None:
                return await self._mailbox_visible(mailbox)
            # An approval to send refers to its draft, not to a message.
            from app.db.models import DraftAction
            from app.domain.email_access import may_access_draft

            draft = await self.db.scalar(
                select(DraftAction.draft_data).where(DraftAction.id == entity_id)
            )
            return draft is not None and await may_access_draft(self.db, self.user, draft)
        if kind in {"anomaly", "approval"} and depth == 0:
            from app.db.models import AnomalyCard, Approval

            model = AnomalyCard if kind == "anomaly" else Approval
            row = (
                await self.db.execute(
                    select(model.entity_type, model.entity_id).where(model.id == entity_id)
                )
            ).first()
            if row is None:
                return False
            return await self.entity_visible(row[0], row[1], depth=1)
        # Suppliers, catalog entries, engineering objects: company data.
        return True

    # ── nodes and edges ─────────────────────────────────────────────────

    async def visible_nodes(self, nodes: Iterable[KnowledgeNode]) -> set[uuid.UUID]:
        nodes = [node for node in nodes if node.id not in self._nodes]
        mention_only = [
            node for node in nodes if node.source_document_id is None and node.entity_id is None
        ]
        support = await self._supporting_documents([node.id for node in mention_only])
        visible_support = await self.visible_documents(
            doc for docs in support.values() for doc in docs
        )
        for node in nodes:
            if node.source_document_id is not None or node.entity_id is not None:
                ok = await self.document_visible(
                    node.source_document_id
                ) and await self.entity_visible(node.entity_type, node.entity_id)
            else:
                docs = support.get(node.id, set())
                if docs:
                    ok = bool(docs & visible_support)
                else:
                    # No document behind it any more. A node the builder made
                    # from document text keeps that text in its title: once its
                    # sources are gone it is nobody's to read (E44). A node
                    # made by hand (a supplier, a manual note) is company data.
                    ok = not _auto_built(node)
            self._nodes[node.id] = ok
        return {node_id for node_id, ok in self._nodes.items() if ok}

    async def node_visible(self, node: KnowledgeNode) -> bool:
        return node.id in await self.visible_nodes([node])

    async def _supporting_documents(
        self, node_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, set[uuid.UUID]]:
        """Documents that mention each node or source an edge touching it."""
        support: dict[uuid.UUID, set[uuid.UUID]] = {}
        if not node_ids:
            return support
        rows = await self.db.execute(
            select(EntityMention.node_id, EntityMention.document_id).where(
                EntityMention.node_id.in_(node_ids)
            )
        )
        for node_id, doc_id in rows:
            if doc_id is not None:
                support.setdefault(node_id, set()).add(doc_id)
        rows = await self.db.execute(
            select(
                KnowledgeEdge.source_node_id,
                KnowledgeEdge.target_node_id,
                KnowledgeEdge.source_document_id,
            ).where(
                KnowledgeEdge.source_document_id.is_not(None),
                (KnowledgeEdge.source_node_id.in_(node_ids))
                | (KnowledgeEdge.target_node_id.in_(node_ids)),
            )
        )
        wanted = set(node_ids)
        for source_id, target_id, doc_id in rows:
            for node_id in (source_id, target_id):
                if node_id in wanted:
                    support.setdefault(node_id, set()).add(doc_id)
        return support

    async def visible_edges(self, edges: Iterable[KnowledgeEdge]) -> list[KnowledgeEdge]:
        edges = list(edges)
        if not edges:
            return []
        node_ids = {e.source_node_id for e in edges} | {e.target_node_id for e in edges}
        missing = node_ids - set(self._nodes)
        if missing:
            rows = await self.db.scalars(select(KnowledgeNode).where(KnowledgeNode.id.in_(missing)))
            found = list(rows)
            await self.visible_nodes(found)
            for node_id in missing - {node.id for node in found}:
                self._nodes[node_id] = False
        span_ids = {e.evidence_span_id for e in edges if e.evidence_span_id}
        span_documents: dict[uuid.UUID, uuid.UUID | None] = {}
        if span_ids:
            rows = await self.db.execute(
                select(EvidenceSpan.id, EvidenceSpan.document_id).where(
                    EvidenceSpan.id.in_(span_ids)
                )
            )
            span_documents = dict(rows.all())
        documents = await self.visible_documents(
            [e.source_document_id for e in edges] + list(span_documents.values())
        )
        result = []
        for edge in edges:
            if not (self._nodes.get(edge.source_node_id) and self._nodes.get(edge.target_node_id)):
                continue
            if edge.source_document_id is not None and edge.source_document_id not in documents:
                continue
            if edge.evidence_span_id is not None:
                span_doc = span_documents.get(edge.evidence_span_id)
                if edge.evidence_span_id not in span_documents or (
                    span_doc is not None and span_doc not in documents
                ):
                    continue
            result.append(edge)
        return result
