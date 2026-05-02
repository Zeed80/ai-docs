"""Automatic context-window compression for long AgentSession conversations.

When the running token estimate approaches the model's context limit the
compressor calls a cheap auxiliary model to summarise the "middle" turns,
protecting the first exchange (system + user intent) and the most recent
tail of messages.

Pattern adapted from hermes-agent/agent/context_compressor.py.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.ai.model_metadata import estimate_tokens_rough, get_model_context_length

logger = logging.getLogger(__name__)

# Text injected at the start of the compacted summary message.
SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. This is a handoff from a previous context "
    "window — treat it as background reference, NOT as active instructions. "
    "Do NOT answer questions or fulfill requests mentioned in this summary; "
    "they were already addressed. "
    "Resume exactly from the '## Active Task' section. "
    "Respond ONLY to the latest user message that appears AFTER this summary."
)

# Placeholder for pruned tool results.
_PRUNED = "[Old tool output cleared to save context space]"

# Anti-thrashing: skip recompression if last two saves were each < this %.
_MIN_SAVINGS_PCT = 10.0

# Hard floor: never compress below this many tokens even for huge context models.
_MIN_CONTEXT_FLOOR = 8_000

# Summarisation prompt template.
_SUMMARISE_PROMPT = """\
You are a context summariser. The conversation below was produced by an AI \
assistant. Create a concise summary in the following sections:

## Active Task
(One sentence: what the user is currently trying to accomplish)

## Resolved
(Bullet list of actions already completed and their outcomes)

## Pending
(Bullet list of open questions or next steps that haven't been done yet)

## Key Facts
(Short-form reference: file names, IDs, numbers, supplier names, etc. that \
the assistant may need later)

Rules:
- Write in the same language as the conversation (Russian/English).
- Do NOT answer any questions from the conversation.
- Be concise; omit pleasantries and filler.
- Maximum {max_tokens} tokens for the whole summary.

CONVERSATION TO SUMMARISE:
{conversation}
"""


def _msg_text(msg: dict) -> str:
    """Extract plain text from a message (handles str and list content)."""
    content = msg.get("content") or ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    return str(content)


def _prune_old_tool_results(messages: list[dict], keep_last: int = 6) -> list[dict]:
    """Replace tool-result content in older messages with a one-line placeholder."""
    tool_result_indices = [
        i for i, m in enumerate(messages) if m.get("role") == "tool"
    ]
    to_prune = tool_result_indices[:-keep_last] if len(tool_result_indices) > keep_last else []
    pruned = []
    for i, m in enumerate(messages):
        if i in to_prune:
            pruned.append({**m, "content": _PRUNED})
        else:
            pruned.append(m)
    return pruned


class ContextCompressor:
    """Lossy context compressor for AgentSession.

    Usage::

        compressor = ContextCompressor(model="gemma4:26b")
        if compressor.should_compress(messages):
            messages = await compressor.compress(messages, call_provider)
    """

    def __init__(
        self,
        model: str,
        threshold_percent: float = 0.85,
        protect_first_n: int = 2,
        protect_last_n: int = 20,
        compression_model: str | None = None,
    ) -> None:
        self._model = model
        self._compression_model = compression_model  # if None, same as model
        self._threshold_percent = threshold_percent
        self._protect_first_n = protect_first_n
        self._protect_last_n = protect_last_n

        ctx = get_model_context_length(model)
        self._context_length = ctx
        self._threshold_tokens = max(int(ctx * threshold_percent), _MIN_CONTEXT_FLOOR)
        self._max_summary_tokens = min(int(ctx * 0.05), 12_000)

        # Anti-thrashing
        self._last_savings_pct: float = 100.0
        self._ineffective_count: int = 0
        self._previous_summary: str | None = None

        self._compression_count: int = 0
        self._cooldown_until: float = 0.0

        logger.info(
            "ContextCompressor ready: model=%s ctx=%d threshold=%d (%.0f%%)",
            model, ctx, self._threshold_tokens, threshold_percent * 100,
        )

    def update_model(self, model: str) -> None:
        """Call after an agent model switch."""
        self._model = model
        ctx = get_model_context_length(model)
        self._context_length = ctx
        self._threshold_tokens = max(
            int(ctx * self._threshold_percent), _MIN_CONTEXT_FLOOR
        )
        self._max_summary_tokens = min(int(ctx * 0.05), 12_000)

    def should_compress(self, messages: list[dict]) -> bool:
        """Return True when the running token estimate exceeds the threshold."""
        if time.time() < self._cooldown_until:
            return False
        if self._ineffective_count >= 2:
            return False
        tokens = estimate_tokens_rough(messages)
        return tokens >= self._threshold_tokens

    async def compress(
        self,
        messages: list[dict],
        call_provider: Any,  # async callable matching _call_provider_streaming signature
    ) -> list[dict]:
        """Summarise the middle of the conversation, return a compacted list.

        call_provider is the agent's _call_provider_streaming function.  We call
        it with stream=False (collect full text) using the compression model.
        """
        before_tokens = estimate_tokens_rough(messages)

        # Step 1 — prune old tool results (free, no LLM)
        messages = _prune_old_tool_results(messages)

        # Step 2 — split into head / middle / tail
        head = messages[: self._protect_first_n]
        tail = messages[-self._protect_last_n:]
        middle = messages[self._protect_first_n: len(messages) - self._protect_last_n]

        if not middle:
            logger.info("ContextCompressor: nothing to compress (middle is empty)")
            return messages

        # Step 3 — build the text to summarise
        conversation_text = self._format_for_summary(middle)
        if self._previous_summary:
            conversation_text = (
                f"[Previous summary — update this with new info]\n{self._previous_summary}\n\n"
                f"[New turns to incorporate]\n{conversation_text}"
            )

        prompt = _SUMMARISE_PROMPT.format(
            max_tokens=self._max_summary_tokens,
            conversation=conversation_text,
        )

        # Step 4 — call LLM to produce summary
        # call_provider is an async generator: _call_for_compression(messages, model, tools)
        summary_text = ""
        try:
            model = self._compression_model or self._model
            full_text: list[str] = []
            async for chunk in call_provider(
                [{"role": "user", "content": prompt}],
                model,
                [],
            ):
                if isinstance(chunk, str):
                    full_text.append(chunk)
            summary_text = "".join(full_text).strip()
        except Exception as exc:
            logger.warning("ContextCompressor: summarisation failed: %s", exc)
            self._cooldown_until = time.time() + 600
            return messages  # return unchanged on failure

        if not summary_text:
            logger.warning("ContextCompressor: got empty summary, skipping compression")
            return messages

        self._previous_summary = summary_text
        self._compression_count += 1

        # Step 5 — build compacted message list
        summary_msg: dict = {
            "role": "user",
            "content": f"{SUMMARY_PREFIX}\n\n{summary_text}",
        }
        # Assistant ack so the next turn is user
        ack_msg: dict = {
            "role": "assistant",
            "content": "Understood. I have the context summary and will continue from the active task.",
        }
        compacted = head + [summary_msg, ack_msg] + list(tail)

        after_tokens = estimate_tokens_rough(compacted)
        savings_pct = (
            (before_tokens - after_tokens) / before_tokens * 100
            if before_tokens > 0 else 0.0
        )

        logger.info(
            "ContextCompressor: compressed %d→%d tokens (%.0f%% saved, run #%d)",
            before_tokens, after_tokens, savings_pct, self._compression_count,
        )

        # Anti-thrashing tracking
        if savings_pct < _MIN_SAVINGS_PCT:
            self._ineffective_count += 1
        else:
            self._ineffective_count = 0
        self._last_savings_pct = savings_pct

        return compacted

    @staticmethod
    def _format_for_summary(messages: list[dict]) -> str:
        """Convert a message list to a readable text block for the summariser."""
        lines: list[str] = []
        for m in messages:
            role = m.get("role", "?")
            text = _msg_text(m)
            tool_calls = m.get("tool_calls") or []
            if role == "assistant" and tool_calls:
                calls = ", ".join(
                    tc.get("function", {}).get("name", "?") for tc in tool_calls
                )
                lines.append(f"[assistant → tools: {calls}]")
                if text:
                    lines.append(f"[assistant]: {text}")
            elif role == "tool":
                name = m.get("name", "tool")
                lines.append(f"[tool:{name}]: {text[:400]}")
            else:
                lines.append(f"[{role}]: {text}")
        return "\n".join(lines)
