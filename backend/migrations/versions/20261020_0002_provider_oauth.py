"""Sign-in through the provider's site (OAuth) for cloud provider nodes.

Revision ID: 20261020_0002
Revises: 20261020_0001
"""

import sqlalchemy as sa
from alembic import op

revision = "20261020_0002"
down_revision = "20261020_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # "api_key" (as before) or "oauth": the node signs in through the
    # provider's site and holds tokens instead of a key.
    op.add_column(
        "provider_instances",
        sa.Column("auth_mode", sa.String(20), nullable=False, server_default="api_key"),
    )
    # Fernet-encrypted JSON: access/refresh tokens, expiry, account, protocol.
    op.add_column("provider_instances", sa.Column("oauth_encrypted", sa.Text()))


def downgrade() -> None:
    op.drop_column("provider_instances", "oauth_encrypted")
    op.drop_column("provider_instances", "auth_mode")
