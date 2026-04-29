"""add graph review queue

Revision ID: d9a1b6e4c230
Revises: c8e4f2a9b731
Create Date: 2026-04-27 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision: str = "d9a1b6e4c230"
down_revision: Union[str, None] = "c8e4f2a9b731"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "graph_review_items",
        sa.Column("item_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("document_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("node_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("edge_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("mention_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("suggested_by", sa.String(length=50), nullable=False),
        sa.Column("decided_by", sa.String(length=100), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["edge_id"], ["knowledge_edges.id"]),
        sa.ForeignKeyConstraint(["mention_id"], ["entity_mentions.id"]),
        sa.ForeignKeyConstraint(["node_id"], ["knowledge_nodes.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_graph_review_items_item_type"), "graph_review_items", ["item_type"])
    op.create_index(op.f("ix_graph_review_items_status"), "graph_review_items", ["status"])
    op.create_index(op.f("ix_graph_review_items_document_id"), "graph_review_items", ["document_id"])
    op.create_index(op.f("ix_graph_review_items_node_id"), "graph_review_items", ["node_id"])
    op.create_index(op.f("ix_graph_review_items_edge_id"), "graph_review_items", ["edge_id"])
    op.create_index(op.f("ix_graph_review_items_mention_id"), "graph_review_items", ["mention_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_graph_review_items_mention_id"), table_name="graph_review_items")
    op.drop_index(op.f("ix_graph_review_items_edge_id"), table_name="graph_review_items")
    op.drop_index(op.f("ix_graph_review_items_node_id"), table_name="graph_review_items")
    op.drop_index(op.f("ix_graph_review_items_document_id"), table_name="graph_review_items")
    op.drop_index(op.f("ix_graph_review_items_status"), table_name="graph_review_items")
    op.drop_index(op.f("ix_graph_review_items_item_type"), table_name="graph_review_items")
    op.drop_table("graph_review_items")
