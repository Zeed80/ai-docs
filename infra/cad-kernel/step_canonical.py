"""Stable STEP serialization helpers independent of FreeCAD imports."""

from __future__ import annotations

import re


_NEGATIVE_ZERO = re.compile(
    rb"(?<![A-Za-z0-9_.])-0(?:\.0*)?(?:E[+-]?\d+)?(?=[,)\s])"
)
_STEP_STRING = re.compile(rb"'(?:''|[^'])*'")


def _normalize_data_negative_zero(data: bytes) -> bytes:
    """Normalize numeric signed zero while preserving quoted STEP strings."""
    chunks: list[bytes] = []
    cursor = 0
    for match in _STEP_STRING.finditer(data):
        chunks.append(_NEGATIVE_ZERO.sub(b"0.", data[cursor:match.start()]))
        chunks.append(match.group(0))
        cursor = match.end()
    chunks.append(_NEGATIVE_ZERO.sub(b"0.", data[cursor:]))
    return b"".join(chunks)


def canonicalize_step_bytes(step_bytes: bytes) -> bytes:
    """Remove process metadata and semantically irrelevant signed-zero drift."""
    canonical, replacements = re.subn(
        rb"(FILE_NAME\([^,]+,)'[^']*'",
        rb"\1'1970-01-01T00:00:00'",
        step_bytes,
        count=1,
    )
    if replacements != 1:
        raise ValueError("STEP has no canonical FILE_NAME header")
    canonical = re.sub(
        rb"Open CASCADE STEP translator 7\.9 [0-9]+",
        b"Open CASCADE STEP translator 7.9 1",
        canonical,
    )
    header, data_marker, remainder = canonical.partition(b"DATA;")
    data, end_marker, trailer = remainder.partition(b"ENDSEC;")
    if not data_marker or not end_marker:
        raise ValueError("STEP has no complete DATA section")
    return b"".join((
        header,
        data_marker,
        _normalize_data_negative_zero(data),
        end_marker,
        trailer,
    ))
