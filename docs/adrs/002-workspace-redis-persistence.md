# ADR 002: Workspace blocks persistence in Redis

**Status**: Accepted  
**Date**: 2026-05

## Context

The agent publishes structured results (tables, summaries, comparisons) as "workspace blocks" visible on the main dashboard (`/`). Initial implementation used an in-memory Python dict, which meant:

- All blocks lost on backend restart (hot-reload, deploy, crash)
- Celery workers couldn't share block state with the web process
- No TTL: stale blocks accumulated indefinitely

## Decision

Migrate workspace blocks to **Redis hashes**:
- Key: `workspace:block:{block_id}` (JSON value)
- Index: `workspace:block_ids` (sorted set by `updated_at` timestamp)
- TTL: 24 hours per block (sufficient to survive overnight, refreshed on update)
- Fallback: if Redis is unavailable, graceful degradation to empty block list

Implementation: `backend/app/domain/workspace.py` using `get_async_redis()`.

## Consequences

**Positive:**
- Blocks survive backend restarts and hot-reloads
- Multiple workers share the same block state
- Automatic expiry prevents stale data accumulation
- Redis is already a project dependency (Celery broker)

**Negative:**
- Redis becomes a hard dependency for the workspace feature
- TTL means blocks disappear after 24h (acceptable for a daily-review workflow)
- No long-term audit trail (blocks are ephemeral UI state, not business records)

## Alternatives considered

- **PostgreSQL table**: persistent but heavier; workspace blocks are UI state, not business entities
- **Browser localStorage**: client-only, doesn't survive multi-device or multi-tab scenarios
- **No persistence (status quo)**: unacceptable UX — workspace cleared on every deploy
