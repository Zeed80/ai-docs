"""add ntd norm control

Revision ID: 5f60718293a4
Revises: 4e5f60718293
Create Date: 2026-04-28 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision: str = "5f60718293a4"
down_revision: Union[str, None] = "4e5f60718293"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "graph_build_statuses",
        sa.Column("document_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("build_scope", sa.String(length=50), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_graph_build_statuses_document_id"), "graph_build_statuses", ["document_id"])
    op.create_index(op.f("ix_graph_build_statuses_document_version_id"), "graph_build_statuses", ["document_version_id"])
    op.create_index(op.f("ix_graph_build_statuses_status"), "graph_build_statuses", ["status"])

    op.create_table(
        "ntd_control_settings",
        sa.Column("singleton_key", sa.String(length=50), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("updated_by", sa.String(length=100), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("singleton_key"),
    )

    op.create_table(
        "normative_documents",
        sa.Column("code", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("document_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("current_version_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("source_document_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["source_document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_normative_documents_code"), "normative_documents", ["code"])
    op.create_index(op.f("ix_normative_documents_current_version_id"), "normative_documents", ["current_version_id"])
    op.create_index(op.f("ix_normative_documents_document_type"), "normative_documents", ["document_type"])
    op.create_index(op.f("ix_normative_documents_source_document_id"), "normative_documents", ["source_document_id"])
    op.create_index(op.f("ix_normative_documents_status"), "normative_documents", ["status"])
    op.create_index(op.f("ix_normative_documents_title"), "normative_documents", ["title"])

    op.create_table(
        "normative_document_versions",
        sa.Column("normative_document_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("version_label", sa.String(length=100), nullable=False),
        sa.Column("effective_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("source_document_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("text_hash", sa.String(length=64), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["normative_document_id"], ["normative_documents.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_normative_document_versions_normative_document_id"), "normative_document_versions", ["normative_document_id"])
    op.create_index(op.f("ix_normative_document_versions_source_document_id"), "normative_document_versions", ["source_document_id"])
    op.create_index(op.f("ix_normative_document_versions_status"), "normative_document_versions", ["status"])
    op.create_index(op.f("ix_normative_document_versions_text_hash"), "normative_document_versions", ["text_hash"])
    op.create_index(op.f("ix_normative_document_versions_version_label"), "normative_document_versions", ["version_label"])

    op.create_table(
        "normative_clauses",
        sa.Column("normative_document_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("parent_clause_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("clause_number", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["normative_document_id"], ["normative_documents.id"]),
        sa.ForeignKeyConstraint(["parent_clause_id"], ["normative_clauses.id"]),
        sa.ForeignKeyConstraint(["version_id"], ["normative_document_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_normative_clauses_clause_number"), "normative_clauses", ["clause_number"])
    op.create_index(op.f("ix_normative_clauses_normative_document_id"), "normative_clauses", ["normative_document_id"])
    op.create_index(op.f("ix_normative_clauses_parent_clause_id"), "normative_clauses", ["parent_clause_id"])
    op.create_index(op.f("ix_normative_clauses_title"), "normative_clauses", ["title"])
    op.create_index(op.f("ix_normative_clauses_version_id"), "normative_clauses", ["version_id"])

    op.create_table(
        "normative_requirements",
        sa.Column("normative_document_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("clause_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("requirement_code", sa.String(length=120), nullable=False),
        sa.Column("requirement_type", sa.String(length=80), nullable=False),
        sa.Column("applies_to", sa.JSON(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("required_keywords", sa.JSON(), nullable=True),
        sa.Column("severity", sa.String(length=30), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["clause_id"], ["normative_clauses.id"]),
        sa.ForeignKeyConstraint(["normative_document_id"], ["normative_documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_normative_requirements_clause_id"), "normative_requirements", ["clause_id"])
    op.create_index(op.f("ix_normative_requirements_is_active"), "normative_requirements", ["is_active"])
    op.create_index(op.f("ix_normative_requirements_normative_document_id"), "normative_requirements", ["normative_document_id"])
    op.create_index(op.f("ix_normative_requirements_requirement_code"), "normative_requirements", ["requirement_code"])
    op.create_index(op.f("ix_normative_requirements_requirement_type"), "normative_requirements", ["requirement_type"])
    op.create_index(op.f("ix_normative_requirements_severity"), "normative_requirements", ["severity"])

    op.create_table(
        "ntd_check_runs",
        sa.Column("document_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("triggered_by", sa.String(length=20), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("findings_total", sa.Integer(), nullable=False),
        sa.Column("findings_open", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ntd_check_runs_document_id"), "ntd_check_runs", ["document_id"])
    op.create_index(op.f("ix_ntd_check_runs_document_version_id"), "ntd_check_runs", ["document_version_id"])
    op.create_index(op.f("ix_ntd_check_runs_mode"), "ntd_check_runs", ["mode"])
    op.create_index(op.f("ix_ntd_check_runs_status"), "ntd_check_runs", ["status"])
    op.create_index(op.f("ix_ntd_check_runs_triggered_by"), "ntd_check_runs", ["triggered_by"])

    op.create_table(
        "ntd_check_findings",
        sa.Column("check_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("normative_document_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("clause_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("requirement_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("severity", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("finding_code", sa.String(length=120), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("evidence_text", sa.Text(), nullable=True),
        sa.Column("recommendation", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("decided_by", sa.String(length=100), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["check_id"], ["ntd_check_runs.id"]),
        sa.ForeignKeyConstraint(["clause_id"], ["normative_clauses.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["normative_document_id"], ["normative_documents.id"]),
        sa.ForeignKeyConstraint(["requirement_id"], ["normative_requirements.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ntd_check_findings_check_id"), "ntd_check_findings", ["check_id"])
    op.create_index(op.f("ix_ntd_check_findings_clause_id"), "ntd_check_findings", ["clause_id"])
    op.create_index(op.f("ix_ntd_check_findings_document_id"), "ntd_check_findings", ["document_id"])
    op.create_index(op.f("ix_ntd_check_findings_finding_code"), "ntd_check_findings", ["finding_code"])
    op.create_index(op.f("ix_ntd_check_findings_normative_document_id"), "ntd_check_findings", ["normative_document_id"])
    op.create_index(op.f("ix_ntd_check_findings_requirement_id"), "ntd_check_findings", ["requirement_id"])
    op.create_index(op.f("ix_ntd_check_findings_severity"), "ntd_check_findings", ["severity"])
    op.create_index(op.f("ix_ntd_check_findings_status"), "ntd_check_findings", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_ntd_check_findings_status"), table_name="ntd_check_findings")
    op.drop_index(op.f("ix_ntd_check_findings_severity"), table_name="ntd_check_findings")
    op.drop_index(op.f("ix_ntd_check_findings_requirement_id"), table_name="ntd_check_findings")
    op.drop_index(op.f("ix_ntd_check_findings_normative_document_id"), table_name="ntd_check_findings")
    op.drop_index(op.f("ix_ntd_check_findings_finding_code"), table_name="ntd_check_findings")
    op.drop_index(op.f("ix_ntd_check_findings_document_id"), table_name="ntd_check_findings")
    op.drop_index(op.f("ix_ntd_check_findings_clause_id"), table_name="ntd_check_findings")
    op.drop_index(op.f("ix_ntd_check_findings_check_id"), table_name="ntd_check_findings")
    op.drop_table("ntd_check_findings")
    op.drop_index(op.f("ix_ntd_check_runs_triggered_by"), table_name="ntd_check_runs")
    op.drop_index(op.f("ix_ntd_check_runs_status"), table_name="ntd_check_runs")
    op.drop_index(op.f("ix_ntd_check_runs_mode"), table_name="ntd_check_runs")
    op.drop_index(op.f("ix_ntd_check_runs_document_version_id"), table_name="ntd_check_runs")
    op.drop_index(op.f("ix_ntd_check_runs_document_id"), table_name="ntd_check_runs")
    op.drop_table("ntd_check_runs")
    op.drop_index(op.f("ix_normative_requirements_severity"), table_name="normative_requirements")
    op.drop_index(op.f("ix_normative_requirements_requirement_type"), table_name="normative_requirements")
    op.drop_index(op.f("ix_normative_requirements_requirement_code"), table_name="normative_requirements")
    op.drop_index(op.f("ix_normative_requirements_normative_document_id"), table_name="normative_requirements")
    op.drop_index(op.f("ix_normative_requirements_is_active"), table_name="normative_requirements")
    op.drop_index(op.f("ix_normative_requirements_clause_id"), table_name="normative_requirements")
    op.drop_table("normative_requirements")
    op.drop_index(op.f("ix_normative_clauses_version_id"), table_name="normative_clauses")
    op.drop_index(op.f("ix_normative_clauses_title"), table_name="normative_clauses")
    op.drop_index(op.f("ix_normative_clauses_parent_clause_id"), table_name="normative_clauses")
    op.drop_index(op.f("ix_normative_clauses_normative_document_id"), table_name="normative_clauses")
    op.drop_index(op.f("ix_normative_clauses_clause_number"), table_name="normative_clauses")
    op.drop_table("normative_clauses")
    op.drop_index(op.f("ix_normative_document_versions_version_label"), table_name="normative_document_versions")
    op.drop_index(op.f("ix_normative_document_versions_text_hash"), table_name="normative_document_versions")
    op.drop_index(op.f("ix_normative_document_versions_status"), table_name="normative_document_versions")
    op.drop_index(op.f("ix_normative_document_versions_source_document_id"), table_name="normative_document_versions")
    op.drop_index(op.f("ix_normative_document_versions_normative_document_id"), table_name="normative_document_versions")
    op.drop_table("normative_document_versions")
    op.drop_index(op.f("ix_normative_documents_title"), table_name="normative_documents")
    op.drop_index(op.f("ix_normative_documents_status"), table_name="normative_documents")
    op.drop_index(op.f("ix_normative_documents_source_document_id"), table_name="normative_documents")
    op.drop_index(op.f("ix_normative_documents_document_type"), table_name="normative_documents")
    op.drop_index(op.f("ix_normative_documents_current_version_id"), table_name="normative_documents")
    op.drop_index(op.f("ix_normative_documents_code"), table_name="normative_documents")
    op.drop_table("normative_documents")
    op.drop_table("ntd_control_settings")
    op.drop_index(op.f("ix_graph_build_statuses_status"), table_name="graph_build_statuses")
    op.drop_index(op.f("ix_graph_build_statuses_document_version_id"), table_name="graph_build_statuses")
    op.drop_index(op.f("ix_graph_build_statuses_document_id"), table_name="graph_build_statuses")
    op.drop_table("graph_build_statuses")
