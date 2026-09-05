"""Which file extensions the system is willing to ingest.

There were two implementations of this rule and they disagreed. The upload
endpoint (app/api/documents.py) falls back to a built-in default set when the
``file_extension_allowlist`` table has no matching row; the e-mail ingest path
(app/tasks/ingest.py) did not, so on a deployment where that table is empty —
which is the live state — the SAME pdf was accepted when a person uploaded it
and quarantined when it arrived as an attachment. That silently defeated the
whole Ф6.1 automation: every emailed invoice went to quarantine and nothing
was ever recognised.

One definition, one fallback, both callers.
"""

from __future__ import annotations

DEFAULT_ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".bmp",
        ".csv",
        ".doc",
        ".docx",
        ".dwg",
        ".dxf",
        ".eml",
        ".gif",
        ".iges",
        ".igs",
        ".jpeg",
        ".jpg",
        ".json",
        ".log",
        ".md",
        ".msg",
        ".odt",
        ".pdf",
        ".png",
        ".step",
        ".stp",
        ".svg",
        ".tif",
        ".tiff",
        ".txt",
        ".webp",
        ".xls",
        ".xlsx",
        ".xlsm",
        ".xml",
    }
)


def extension_of(filename: str | None) -> str:
    """Lowercase extension including the dot, or "" when there is none."""
    name = filename or ""
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
