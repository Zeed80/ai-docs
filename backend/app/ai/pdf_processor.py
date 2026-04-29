"""PDF processing with PyMuPDF — text extraction, bbox mapping, page rendering."""

import io
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()


@dataclass
class TextBlock:
    """A block of text with bounding box on a PDF page."""

    text: str
    page: int
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass
class PageData:
    """Extracted data from a single PDF page."""

    page_number: int
    width: float
    height: float
    text: str
    blocks: list[TextBlock] = field(default_factory=list)
    png_bytes: bytes | None = None


@dataclass
class PDFData:
    """Complete extracted data from a PDF document."""

    page_count: int
    pages: list[PageData] = field(default_factory=list)
    full_text: str = ""
    metadata: dict = field(default_factory=dict)


def extract_pdf(content: bytes, *, render_pages: bool = True, dpi: int = 150) -> PDFData:
    """Extract text, bounding boxes, and page renders from PDF.

    Args:
        content: Raw PDF bytes
        render_pages: Whether to render pages as PNG
        dpi: Resolution for PNG rendering

    Returns:
        PDFData with full text, per-page blocks, and optional PNG renders
    """
    import fitz  # PyMuPDF

    doc = fitz.open(stream=content, filetype="pdf")

    pdf_data = PDFData(
        page_count=doc.page_count,
        metadata={
            "title": doc.metadata.get("title", ""),
            "author": doc.metadata.get("author", ""),
            "creator": doc.metadata.get("creator", ""),
        },
    )

    all_text_parts: list[str] = []

    for page_idx in range(doc.page_count):
        page = doc[page_idx]
        rect = page.rect

        # Extract text blocks with positions
        blocks: list[TextBlock] = []
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

        page_text_parts: list[str] = []

        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:  # text block only
                continue

            for line in block.get("lines", []):
                line_text = ""
                line_bbox = line.get("bbox", (0, 0, 0, 0))

                for span in line.get("spans", []):
                    line_text += span.get("text", "")

                if line_text.strip():
                    blocks.append(TextBlock(
                        text=line_text.strip(),
                        page=page_idx,
                        x0=line_bbox[0],
                        y0=line_bbox[1],
                        x1=line_bbox[2],
                        y1=line_bbox[3],
                    ))
                    page_text_parts.append(line_text.strip())

        page_text = "\n".join(page_text_parts)
        all_text_parts.append(page_text)

        # Render page as PNG
        png_bytes = None
        if render_pages:
            zoom = dpi / 72.0
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            png_bytes = pix.tobytes("png")

        pdf_data.pages.append(PageData(
            page_number=page_idx,
            width=rect.width,
            height=rect.height,
            text=page_text,
            blocks=blocks,
            png_bytes=png_bytes,
        ))

    pdf_data.full_text = "\n\n".join(all_text_parts)
    doc.close()

    logger.info(
        "pdf_extracted",
        pages=pdf_data.page_count,
        text_length=len(pdf_data.full_text),
        total_blocks=sum(len(p.blocks) for p in pdf_data.pages),
    )
    return pdf_data


def find_text_bbox(pages: list[PageData], search_text: str) -> TextBlock | None:
    """Find the bounding box of a specific text in the PDF.

    Used for bbox binding — mapping extracted field values to their
    visual position in the original document.
    """
    if not search_text or not search_text.strip():
        return None

    search_lower = search_text.strip().lower()

    for page in pages:
        for block in page.blocks:
            if search_lower in block.text.lower():
                return block

    # Fuzzy match — try partial
    for page in pages:
        for block in page.blocks:
            # Check if any significant part matches
            words = search_lower.split()
            if len(words) >= 2:
                if all(w in block.text.lower() for w in words[:3]):
                    return block

    return None


def bind_bboxes(
    pages: list[PageData],
    fields: dict[str, str | None],
) -> dict[str, dict | None]:
    """Bind extracted field values to bounding boxes in the PDF.

    Args:
        pages: PDF page data with text blocks
        fields: {field_name: field_value} from extraction

    Returns:
        {field_name: {page, x, y, w, h} | None}
    """
    result: dict[str, dict | None] = {}

    for field_name, field_value in fields.items():
        if not field_value:
            result[field_name] = None
            continue

        block = find_text_bbox(pages, field_value)
        if block:
            result[field_name] = {
                "page": block.page,
                "x": block.x0,
                "y": block.y0,
                "w": block.width,
                "h": block.height,
            }
        else:
            result[field_name] = None

    return result
