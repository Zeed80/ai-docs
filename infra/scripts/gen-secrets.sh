#!/usr/bin/env bash
# Generate strong production secrets. Paste the output into infra/.env.
# Usage: bash infra/scripts/gen-secrets.sh
set -euo pipefail

gen() { openssl rand -base64 36 | tr -d '/+=' | cut -c1-48; }

cat <<EOF
# ── Generated secrets ($(date -u +%Y-%m-%dT%H:%M:%SZ)) — copy into infra/.env ──
APP_ENV=production
AUTH_ENABLED=true
CSP_ENABLED=true

APP_SECRET_KEY=$(gen)
CSRF_SECRET=$(gen)
AGENT_SERVICE_KEY=$(gen)

POSTGRES_PASSWORD=$(gen)
MINIO_ACCESS_KEY=$(gen)
MINIO_SECRET_KEY=$(gen)

OAUTH_CLIENT_SECRET=$(gen)
AUTHENTIK_SECRET_KEY=$(gen)
AUTHENTIK_DB_PASSWORD=$(gen)
AUTHENTIK_BOOTSTRAP_PASSWORD=$(gen)
EOF

echo
echo "Reminder: also set TRAEFIK_DOMAIN, TRAEFIK_ACME_EMAIL, CORS_ORIGINS," >&2
echo "AUTHENTIK_EXTERNAL_URL to your domain, and NEXT_PUBLIC_API_URL=same-origin." >&2
