# ADR 001: Capability-based agent routing

**Status**: Accepted  
**Date**: 2026-05

## Context

The original design registered 52 individual skills as separate AiAgent tools. Each skill had its own JSON schema and was independently callable. This led to:

- Context window overload: 52 tool schemas × ~300 tokens = ~15k tokens per turn just for tools
- High coupling: every endpoint change required updating the skill registry
- Combinatorial explosion: the agent struggled to pick the right tool among similar ones

## Decision

Collapse 52 skills into **15 broad capabilities** defined in `aiagent/skills/capabilities.yml`. Each capability covers a domain (invoices, suppliers, anomalies, etc.) and accepts a single `action` parameter that the backend dispatcher routes internally.

The dispatcher lives at `POST /api/agent/cap/{capability_name}` and maps `(capability, action)` → `(HTTP method, backend endpoint)`.

## Consequences

**Positive:**
- Tool schema budget reduced to ~2k tokens (15 capabilities × ~130 tokens)
- Single registry file (`capabilities.yml`) is the source of truth
- Adding a new backend endpoint requires only a dispatcher entry, not a skill schema update
- Agent planning is simpler: "what domain?" then "what action?"

**Negative:**
- Agent cannot discover granular parameter schemas for each action
- Error messages from unknown actions expose internal routing details
- Testing requires knowledge of valid `(capability, action)` pairs

## Alternatives considered

- **Individual tool schemas**: rejected due to context window cost
- **Dynamic tool discovery**: would require MCP protocol or a tool-listing endpoint; deferred to Эпик 6
- **Hierarchical tools**: parent tool + sub-tools; similar to capabilities but more complex to implement
