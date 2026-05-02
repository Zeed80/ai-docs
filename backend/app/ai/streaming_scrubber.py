"""Stateful scrubber for streaming LLM output.

Strips internal tags from token-by-token streams where a regex over the
full string would fail because tags span chunk boundaries.

Filtered tag pairs:
- <memory-context> … </memory-context>  — internal memory injection
- <think> … </think>                    — Ollama/qwen3 reasoning
- <thinking> … </thinking>              — DeepSeek / Anthropic extended thinking
- <reasoning> … </reasoning>            — some OpenAI-compatible models
"""

from __future__ import annotations

import re
from typing import ClassVar


def sanitize_context(text: str) -> str:
    """Strip all internal fence blocks from a complete string (non-streaming)."""
    for pattern in _FULL_BLOCK_PATTERNS:
        text = pattern.sub("", text)
    for pattern in _FENCE_TAG_PATTERNS:
        text = pattern.sub("", text)
    text = _SYSTEM_NOTE_RE.sub("", text)
    return text


_FULL_BLOCK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>", re.IGNORECASE),
    re.compile(r"<\s*think\s*>[\s\S]*?</\s*think\s*>", re.IGNORECASE),
    re.compile(r"<\s*thinking\s*>[\s\S]*?</\s*thinking\s*>", re.IGNORECASE),
    re.compile(r"<\s*reasoning\s*>[\s\S]*?</\s*reasoning\s*>", re.IGNORECASE),
]

_FENCE_TAG_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"</?\s*memory-context\s*>", re.IGNORECASE),
    re.compile(r"</?\s*think\s*>", re.IGNORECASE),
    re.compile(r"</?\s*thinking\s*>", re.IGNORECASE),
    re.compile(r"</?\s*reasoning\s*>", re.IGNORECASE),
]

_SYSTEM_NOTE_RE = re.compile(
    r"\[System note:\s*The following is recalled memory context,[^\]]*\]\s*",
    re.IGNORECASE,
)

# Ordered longest-first so partial-suffix checks prefer longer matches.
_TAG_PAIRS: list[tuple[str, str]] = [
    ("<memory-context>", "</memory-context>"),
    ("<thinking>", "</thinking>"),
    ("<reasoning>", "</reasoning>"),
    ("<think>", "</think>"),
]


class StreamingContextScrubber:
    """Stateful scrubber for streaming text that may contain split tag spans.

    The one-shot ``sanitize_context`` regex cannot survive chunk boundaries:
    a tag opened in one delta and closed in a later delta leaks its payload
    because the regex needs both tags in one string.  This scrubber runs a
    small state machine across deltas.

    Usage::

        scrubber = StreamingContextScrubber()
        for delta in stream:
            visible = scrubber.feed(delta)
            if visible:
                emit(visible)
        trailing = scrubber.flush()   # must call at end-of-stream
        if trailing:
            emit(trailing)

    Call ``reset()`` before reusing for a new top-level response.
    """

    # All open-tag strings checked (lower-cased) for partial-suffix detection.
    _ALL_OPEN: ClassVar[list[str]] = [pair[0] for pair in _TAG_PAIRS]
    _ALL_CLOSE: ClassVar[list[str]] = [pair[1] for pair in _TAG_PAIRS]

    def __init__(self) -> None:
        self._in_span: bool = False
        self._close_tag: str = ""
        self._buf: str = ""

    def reset(self) -> None:
        self._in_span = False
        self._close_tag = ""
        self._buf = ""

    def feed(self, text: str) -> str:
        """Return the visible portion of *text* after scrubbing.

        Trailing fragments that could be the start of an open/close tag are
        held back and surfaced on the next ``feed()`` call or by ``flush()``.
        """
        if not text:
            return ""
        buf = self._buf + text
        self._buf = ""
        out: list[str] = []

        while buf:
            if self._in_span:
                close = self._close_tag
                idx = buf.lower().find(close)
                if idx == -1:
                    held = _max_partial_suffix(buf, close)
                    self._buf = buf[-held:] if held else ""
                    return "".join(out)
                buf = buf[idx + len(close):]
                self._in_span = False
                self._close_tag = ""
            else:
                # Find the earliest opening tag.
                earliest_idx = len(buf) + 1
                matched_pair: tuple[str, str] | None = None
                for open_tag, close_tag in _TAG_PAIRS:
                    idx = buf.lower().find(open_tag)
                    if idx != -1 and idx < earliest_idx:
                        earliest_idx = idx
                        matched_pair = (open_tag, close_tag)

                if matched_pair is None:
                    # No open tag found; hold back a potential partial.
                    held = _max_partial_suffix_multi(buf, self._ALL_OPEN)
                    if held:
                        out.append(buf[:-held])
                        self._buf = buf[-held:]
                    else:
                        out.append(buf)
                    return "".join(out)

                open_tag, close_tag = matched_pair
                if earliest_idx > 0:
                    out.append(buf[:earliest_idx])
                buf = buf[earliest_idx + len(open_tag):]
                self._in_span = True
                self._close_tag = close_tag

        return "".join(out)

    def flush(self) -> str:
        """Emit any held-back buffer at end-of-stream.

        Unterminated span content is discarded; a held partial-tag tail
        that turned out not to be a real tag is emitted verbatim.
        """
        if self._in_span:
            self._buf = ""
            self._in_span = False
            self._close_tag = ""
            return ""
        tail = self._buf
        self._buf = ""
        return tail


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _max_partial_suffix(buf: str, tag: str) -> int:
    """Longest suffix of *buf* that is a prefix of *tag* (case-insensitive)."""
    tag_l = tag.lower()
    buf_l = buf.lower()
    max_check = min(len(buf_l), len(tag_l) - 1)
    for i in range(max_check, 0, -1):
        if tag_l.startswith(buf_l[-i:]):
            return i
    return 0


def _max_partial_suffix_multi(buf: str, tags: list[str]) -> int:
    """Longest partial suffix across multiple candidate tags."""
    return max((_max_partial_suffix(buf, t) for t in tags), default=0)
