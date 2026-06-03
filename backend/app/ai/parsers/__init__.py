"""Document text-extraction parser registry.

Single source of truth for turning raw document bytes into plain text,
keyed by file type. Reused by both the live Celery extraction pipeline
(`app.tasks.extraction`) and the agent execution path
(`app.tasks.document_processing`).

OCR is deliberately NOT performed here — image/scanned-PDF inputs return
``needs_ocr=True`` with empty text, and the caller (which has model-resolution
and Celery context) runs the VLM OCR fallback.
"""

from app.ai.parsers.registry import ParsedDocument, parse_document

__all__ = ["ParsedDocument", "parse_document"]
