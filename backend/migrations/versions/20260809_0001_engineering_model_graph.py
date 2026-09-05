"""EngineeringModelGraph immutable storage and query projections.

Revision ID: 20260809_0001
Revises: 20260724_0003
Create Date: 2026-08-09
"""

import sqlalchemy as sa
from alembic import op

from app.db.base import GUID

revision = "20260809_0001"
down_revision = "20260724_0003"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "engineering_graph_revisions",
        *_timestamps(),
        sa.Column(
            "engineering_project_id",
            GUID(),
            sa.ForeignKey("engineering_projects.id"),
            nullable=False,
        ),
        sa.Column("engineering_revision_id", GUID(), sa.ForeignKey("engineering_revisions.id")),
        sa.Column("graph_id", sa.String(255), nullable=False),
        sa.Column("schema_version", sa.String(30), nullable=False, server_default="emg/1.0"),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("parent_revision", sa.Integer()),
        sa.Column("canonical_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("profile", sa.String(40), nullable=False),
        sa.Column("storage_path", sa.String(1000), nullable=False),
        sa.Column(
            "comprehension_status", sa.String(30), nullable=False, server_default="accumulating"
        ),
        sa.Column("build_status", sa.String(30), nullable=False, server_default="not_ready"),
        sa.Column("release_status", sa.String(30), nullable=False, server_default="blocked"),
        sa.Column("reader_manifest", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index(
        "ix_engineering_graph_revisions_engineering_project_id",
        "engineering_graph_revisions",
        ["engineering_project_id"],
    )
    op.create_index(
        "ix_engineering_graph_revisions_engineering_revision_id",
        "engineering_graph_revisions",
        ["engineering_revision_id"],
    )
    op.create_index(
        "ix_engineering_graph_revisions_graph_id", "engineering_graph_revisions", ["graph_id"]
    )
    op.create_index(
        "ix_emg_graph_revision",
        "engineering_graph_revisions",
        ["graph_id", "revision"],
        unique=True,
    )
    op.create_index(
        "ix_engineering_graph_revisions_profile", "engineering_graph_revisions", ["profile"]
    )
    op.create_index(
        "ix_emg_project_created",
        "engineering_graph_revisions",
        ["engineering_project_id", "created_at"],
    )

    op.create_table(
        "graph_patches",
        *_timestamps(),
        sa.Column("graph_id", sa.String(255), nullable=False),
        sa.Column("patch_id", sa.String(255), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("base_sha256", sa.String(64), nullable=False),
        sa.Column("result_revision_id", GUID(), sa.ForeignKey("engineering_graph_revisions.id")),
        sa.Column("producer", sa.String(40), nullable=False),
        sa.Column("pass_id", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("validation_errors", sa.JSON(), nullable=False, server_default="[]"),
        sa.UniqueConstraint("graph_id", "idempotency_key", name="uq_graph_patch_idempotency"),
    )
    for name in ("graph_id", "patch_id", "result_revision_id", "producer", "accepted"):
        op.create_index(f"ix_graph_patches_{name}", "graph_patches", [name])

    op.create_table(
        "graph_verification_runs",
        *_timestamps(),
        sa.Column(
            "graph_revision_id",
            GUID(),
            sa.ForeignKey("engineering_graph_revisions.id"),
            nullable=False,
        ),
        sa.Column("levels", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("result", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("artifact_hashes", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index(
        "ix_graph_verification_runs_graph_revision_id",
        "graph_verification_runs",
        ["graph_revision_id"],
    )

    op.create_table(
        "trace_proposals",
        *_timestamps(),
        sa.Column(
            "graph_revision_id",
            GUID(),
            sa.ForeignKey("engineering_graph_revisions.id"),
            nullable=False,
        ),
        sa.Column("proposal_id", sa.String(255), nullable=False),
        sa.Column("source_region_id", sa.String(255), nullable=False),
        sa.Column("assertion_id", sa.String(255)),
        sa.Column("rank", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="proposed"),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("score", sa.Float()),
        sa.UniqueConstraint("graph_revision_id", "proposal_id", name="uq_trace_proposal_revision"),
        sa.CheckConstraint("rank BETWEEN 1 AND 3", name="ck_trace_proposal_rank"),
    )
    for name in ("graph_revision_id", "source_region_id", "assertion_id", "status"):
        op.create_index(f"ix_trace_proposals_{name}", "trace_proposals", [name])

    op.create_table(
        "visual_verification_runs",
        *_timestamps(),
        sa.Column("trace_proposal_id", GUID(), sa.ForeignKey("trace_proposals.id"), nullable=False),
        sa.Column("verifier_model", sa.String(255), nullable=False),
        sa.Column("verdict", sa.String(30), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("raw_output", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_visual_verification_runs_trace_proposal_id",
        "visual_verification_runs",
        ["trace_proposal_id"],
    )
    op.create_index("ix_visual_verification_runs_verdict", "visual_verification_runs", ["verdict"])

    op.create_table(
        "engineering_graph_nodes",
        *_timestamps(),
        sa.Column(
            "graph_revision_id",
            GUID(),
            sa.ForeignKey("engineering_graph_revisions.id"),
            nullable=False,
        ),
        sa.Column("node_id", sa.String(255), nullable=False),
        sa.Column("node_type", sa.String(60), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("graph_revision_id", "node_id", name="uq_emg_node_revision"),
    )
    op.create_index(
        "ix_engineering_graph_nodes_graph_revision_id",
        "engineering_graph_nodes",
        ["graph_revision_id"],
    )
    op.create_index(
        "ix_engineering_graph_nodes_node_type", "engineering_graph_nodes", ["node_type"]
    )

    op.create_table(
        "engineering_graph_edges",
        *_timestamps(),
        sa.Column(
            "graph_revision_id",
            GUID(),
            sa.ForeignKey("engineering_graph_revisions.id"),
            nullable=False,
        ),
        sa.Column("edge_id", sa.String(255), nullable=False),
        sa.Column("edge_type", sa.String(60), nullable=False),
        sa.Column("source_node_id", sa.String(255), nullable=False),
        sa.Column("target_node_id", sa.String(255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("graph_revision_id", "edge_id", name="uq_emg_edge_revision"),
    )
    for name in ("graph_revision_id", "edge_type", "source_node_id", "target_node_id"):
        op.create_index(f"ix_engineering_graph_edges_{name}", "engineering_graph_edges", [name])

    op.create_table(
        "engineering_graph_assertions",
        *_timestamps(),
        sa.Column(
            "graph_revision_id",
            GUID(),
            sa.ForeignKey("engineering_graph_revisions.id"),
            nullable=False,
        ),
        sa.Column("assertion_id", sa.String(255), nullable=False),
        sa.Column("subject_node_id", sa.String(255), nullable=False),
        sa.Column("predicate", sa.String(255), nullable=False),
        sa.Column("origin", sa.String(30), nullable=False),
        sa.Column("assurance", sa.String(40), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("graph_revision_id", "assertion_id", name="uq_emg_assertion_revision"),
    )
    for name in (
        "graph_revision_id",
        "subject_node_id",
        "predicate",
        "origin",
        "assurance",
        "state",
    ):
        op.create_index(
            f"ix_engineering_graph_assertions_{name}", "engineering_graph_assertions", [name]
        )


def downgrade() -> None:
    for table in (
        "engineering_graph_assertions",
        "engineering_graph_edges",
        "engineering_graph_nodes",
        "visual_verification_runs",
        "trace_proposals",
        "graph_verification_runs",
        "graph_patches",
        "engineering_graph_revisions",
    ):
        op.drop_table(table)
