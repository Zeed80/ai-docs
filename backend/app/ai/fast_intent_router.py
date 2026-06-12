"""Deterministic fast-path router for high-confidence count intents.

The marker tables now live in ``aiagent/config/routes.yml`` (the single
declarative route table); this module re-exports the fast-path API from
:mod:`app.ai.route_table` so existing imports keep working.

Scope is intentionally narrow: only unambiguous "how many X" questions that map
to one capability list call. Everything else — tables, listings, analysis — is
left to the LLM tool-calling loop, where ``_deliver_final_content`` publishes
rich output. The router is provider/model agnostic and stateless.
"""

from __future__ import annotations

from app.ai.route_table import FastIntent, match_fast_intent

__all__ = ["FastIntent", "match_fast_intent"]
