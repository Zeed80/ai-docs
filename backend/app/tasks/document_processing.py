from __future__ import annotations

import json
import base64
from io import BytesIO
from pathlib import Path

from app.ai import AIRouter
from app.ai.schemas import AIRequest, AITask, ChatMessage
from app.domain.models import Document, ProcessingJobStatus
from app.domain.schemas import (
    DocumentArtifactRead,
    DocumentExtractionResult,
    StructuredDocumentExtraction,
)
from app.domain.storage import LocalFileStorage


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
TEXT_PREVIEW_LIMIT = 12000


class RawExtractionResult:
    def __init__(
        self,
        *,
        status: ProcessingJobStatus,
        parser_name: str,
        text: str = "",
        unsupported_reason: str | None = None,
    ) -> None:
        self.status = status
        self.parser_name = parser_name
        self.text = text
        self.unsupported_reason = unsupported_reason


async def process_document(
    document: Document,
    ai_router: AIRouter,
    storage: LocalFileStorage | None = None,
) -> DocumentExtractionResult:
    raw = extract_text(document)
    if raw.status != ProcessingJobStatus.COMPLETED:
        artifacts = create_preview_artifacts(document, storage) if storage else []
        ocr_text = await ocr_preview_artifacts(document, artifacts, ai_router)
        if ocr_text.strip():
            structured = await structure_extracted_text(document, ocr_text, ai_router)
            return DocumentExtractionResult(
                status=ProcessingJobStatus.COMPLETED.value,
                parser_name=f"{raw.parser_name}+ocr",
                text_preview=ocr_text[:TEXT_PREVIEW_LIMIT],
                text_length=len(ocr_text),
                unsupported_reason=raw.unsupported_reason,
                artifacts=artifacts,
                structured=structured,
            )
        return DocumentExtractionResult(
            status=raw.status.value,
            parser_name=raw.parser_name,
            text_preview=None,
            text_length=0,
            unsupported_reason=raw.unsupported_reason,
            artifacts=artifacts,
            structured=None,
        )

    structured = await structure_extracted_text(document, raw.text, ai_router)
    return DocumentExtractionResult(
        status=ProcessingJobStatus.COMPLETED.value,
        parser_name=raw.parser_name,
        text_preview=raw.text[:TEXT_PREVIEW_LIMIT],
        text_length=len(raw.text),
        artifacts=[],
        structured=structured,
    )


def extract_text(document: Document) -> RawExtractionResult:
    """Extract text via the shared parser registry.

    Delegates to :func:`app.ai.parsers.parse_document` (single source of truth,
    also used by the live Celery pipeline). When the registry flags ``needs_ocr``
    (images / scanned PDFs), this returns UNSUPPORTED so :func:`process_document`
    runs the VLM OCR-artifact fallback.
    """
    from app.ai.parsers import parse_document

    path = Path(document.storage_path)
    if not path.exists():
        return RawExtractionResult(
            status=ProcessingJobStatus.FAILED,
            parser_name="missing_file",
            unsupported_reason="Stored file does not exist.",
        )

    parsed = parse_document(
        path.read_bytes(), document.filename, document.content_type
    )
    if parsed.text.strip():
        return RawExtractionResult(
            status=ProcessingJobStatus.COMPLETED,
            parser_name=parsed.parser_name,
            text=parsed.text,
        )
    if parsed.needs_ocr:
        return RawExtractionResult(
            status=ProcessingJobStatus.UNSUPPORTED,
            parser_name=parsed.parser_name,
            unsupported_reason="No text layer; routing to VLM OCR fallback.",
        )
    return RawExtractionResult(
        status=ProcessingJobStatus.UNSUPPORTED,
        parser_name=parsed.parser_name,
        unsupported_reason="No extractable text for this document.",
    )


async def structure_extracted_text(
    document: Document,
    text: str,
    ai_router: AIRouter,
) -> StructuredDocumentExtraction:
    prompt = (
        "Extract verifiable manufacturing document metadata as JSON matching this shape: "
        "{document_type: string, summary: string, fields: "
        "[{name: string, value: string|number|boolean|null, confidence: number, reason: string, "
        "source: string|null}]}.\n"
        "Use confidence 0..1 and short field-level reasons. "
        "Do not propose actions or external tool calls.\n\n"
        f"Filename: {document.filename}\n"
        f"Content-Type: {document.content_type or 'unknown'}\n"
        f"Known type: {document.document_type or 'unknown'}\n"
        f"Text:\n{text[:TEXT_PREVIEW_LIMIT]}"
    )
    response = await ai_router.run(
        AIRequest(
            task=AITask.STRUCTURED_EXTRACTION,
            messages=[ChatMessage(role="user", content=prompt)],
            response_schema=StructuredDocumentExtraction,
            confidential=True,
            metadata={"document_id": document.id, "local_only": True},
        )
    )
    if isinstance(response.data, StructuredDocumentExtraction):
        return response.data
    if isinstance(response.data, dict):
        return StructuredDocumentExtraction.model_validate(response.data)
    return StructuredDocumentExtraction.model_validate(json.loads(response.text or "{}"))


def create_preview_artifacts(
    document: Document,
    storage: LocalFileStorage,
) -> list[DocumentArtifactRead]:
    path = Path(document.storage_path)
    suffix = path.suffix.lower()
    if not path.exists():
        return []
    if suffix in IMAGE_EXTENSIONS:
        return _create_image_preview(document, path, storage)
    if suffix == ".pdf":
        return _create_pdf_page_previews(document, path, storage)
    return []


async def ocr_preview_artifacts(
    document: Document,
    artifacts: list[DocumentArtifactRead],
    ai_router: AIRouter,
) -> str:
    image_artifacts = [
        artifact
        for artifact in artifacts
        if artifact.content_type and artifact.content_type.startswith("image/")
    ]
    if not image_artifacts:
        return ""
    images = [_artifact_as_data_uri(artifact) for artifact in image_artifacts]
    response = await ai_router.run(
        AIRequest(
            task=AITask.INVOICE_OCR,
            messages=[
                ChatMessage(
                    role="user",
                    content=(
                        "Transcribe visible text from these document preview images. "
                        "Return only the text and preserve important numbers, dates, supplier names, "
                        "drawing labels, and table rows."
                    ),
                )
            ],
            images=images,
            confidential=True,
            metadata={"document_id": document.id, "local_only": True},
        )
    )
    return response.text or ""


def _create_image_preview(
    document: Document,
    path: Path,
    storage: LocalFileStorage,
) -> list[DocumentArtifactRead]:
    try:
        normalized = _normalize_image(path)
        content = normalized["content"]
        filename = f"{path.stem}.preview.png"
        content_type = "image/png"
    except Exception:
        content = path.read_bytes()
        filename = f"{path.stem}.preview{path.suffix.lower() or '.bin'}"
        content_type = _image_content_type(path)
        normalized = {"width": None, "height": None, "normalized": False}

    storage_path, sha256, size_bytes = storage.save_artifact(document.id, filename, content)
    return [
        DocumentArtifactRead(
            artifact_type="image_preview",
            storage_path=storage_path,
            content_type=content_type,
            width=normalized.get("width"),
            height=normalized.get("height"),
            metadata={
                "source": "image_normalization",
                "normalized": bool(normalized.get("normalized")),
                "sha256": sha256,
                "size_bytes": size_bytes,
            },
        )
    ]


def _create_pdf_page_previews(
    document: Document,
    path: Path,
    storage: LocalFileStorage,
) -> list[DocumentArtifactRead]:
    try:
        import fitz  # type: ignore[import-not-found]
    except ImportError:
        return []

    artifacts: list[DocumentArtifactRead] = []
    try:
        with fitz.open(path) as pdf:
            for page_index in range(min(3, len(pdf))):
                page = pdf[page_index]
                pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                content = pixmap.tobytes("png")
                filename = f"{path.stem}.page-{page_index + 1}.png"
                storage_path, sha256, size_bytes = storage.save_artifact(
                    document.id,
                    filename,
                    content,
                )
                artifacts.append(
                    DocumentArtifactRead(
                        artifact_type="pdf_page_preview",
                        storage_path=storage_path,
                        content_type="image/png",
                        page_number=page_index + 1,
                        width=pixmap.width,
                        height=pixmap.height,
                        metadata={
                            "source": "pymupdf_render",
                            "sha256": sha256,
                            "size_bytes": size_bytes,
                        },
                    )
                )
    except Exception:
        return []
    return artifacts


def _normalize_image(path: Path) -> dict:
    from PIL import Image  # type: ignore[import-not-found]

    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((1600, 1600))
        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return {
            "content": buffer.getvalue(),
            "width": image.width,
            "height": image.height,
            "normalized": True,
        }


def _artifact_as_data_uri(artifact: DocumentArtifactRead) -> str:
    content = Path(artifact.storage_path).read_bytes()
    encoded = base64.b64encode(content).decode("ascii")
    content_type = artifact.content_type or "application/octet-stream"
    return f"data:{content_type};base64,{encoded}"


def _image_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix in {".tif", ".tiff"}:
        return "image/tiff"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".bmp":
        return "image/bmp"
    return "application/octet-stream"
