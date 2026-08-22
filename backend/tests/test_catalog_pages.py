"""Page-wise catalog ingestion: the cost gate, identity, and page geometry.

These pin the decisions that make a 948-page catalog parseable at all: which
pages deserve an LLM call, how a position stays the same position across a
re-run, and how PDF points become raster pixels (the coordinate system that
every crop depends on).
"""

from __future__ import annotations

import uuid

from app.domain.catalog_images import (
    ImageCandidate,
    WordBox,
    find_code_box,
    furniture_signatures,
    match_entries_to_images,
    pdf_bbox_to_raster,
)
from app.domain.catalog_pages import (
    crop_image_path,
    entry_content_hash,
    page_image_path,
    page_product_verdict,
)

RASTER = (1241, 1670)


# ── cost gate ───────────────────────────────────────────────────────────────


def test_product_page_is_parsed():
    verdict = page_product_verdict(
        "Фреза концевая MT190-016C04 Ø12 мм — 3450 руб\n"
        "Сверло спиральное DR-6.5 HSS — 480 руб"
    )
    assert verdict.parse is True


def test_table_of_contents_is_skipped():
    """A 948-page catalog opens with ~30 pages of contents; each would cost an
    LLM call and yield nothing."""
    text = "\n".join(
        [
            "Свёрла ................ 12",
            "Фрезы ................. 45",
            "Метчики ............... 78",
            "Пластины сменные ...... 90",
        ]
    )
    verdict = page_product_verdict(text)
    assert verdict.parse is False
    assert verdict.skip_reason == "toc"


def test_cover_and_marketing_pages_are_skipped():
    assert page_product_verdict("КАТАЛОГ 2026").skip_reason == "blank"
    marketing = (
        "Наша компания более двадцати лет поставляет качественный инструмент "
        "предприятиям России, обеспечивая сервис и техническую поддержку."
    )
    assert page_product_verdict(marketing).skip_reason == "no_product_signals"


def test_gate_does_not_need_prices_to_accept_a_page():
    """Technical catalogs list articles and sizes without a single price."""
    verdict = page_product_verdict(
        "2873-10  0-10mm/0-0.4\"  0.01mm\n2873-101  0-10mm/0-0.4\"  0.001mm"
    )
    assert verdict.parse is True


# ── identity / idempotency ──────────────────────────────────────────────────


def test_same_position_keeps_one_hash_across_runs():
    document_id = uuid.uuid4()
    first = entry_content_hash(document_id, 300, "MT190-016", "Фреза концевая")
    again = entry_content_hash(document_id, 300, "mt190 016", "Фреза концевая")
    assert first == again, "re-parsing a page must not create a second position"


def test_different_page_is_a_different_position():
    document_id = uuid.uuid4()
    assert entry_content_hash(document_id, 300, "MT190-016", None) != entry_content_hash(
        document_id, 301, "MT190-016", None
    )


# ── storage layout ──────────────────────────────────────────────────────────


def test_page_and_crop_paths_live_next_to_the_catalog_file():
    storage = "tool-catalogs/s1/ab/abcdef/Каталог.pdf"
    assert page_image_path(storage, 7) == "tool-catalogs/s1/ab/abcdef/pages/0007.webp"
    assert page_image_path(storage, 7, thumb=True).endswith("0007_thumb.webp")
    assert crop_image_path(storage, 7, "r0") == "tool-catalogs/s1/ab/abcdef/crops/0007_r0.webp"


# ── geometry ────────────────────────────────────────────────────────────────


def test_pdf_points_convert_to_raster_pixels():
    """Mixing PDF points with raster pixels shifts every crop silently."""
    assert pdf_bbox_to_raster((0, 0, 72, 72), 150) == (0, 0, 150, 150)
    assert pdf_bbox_to_raster((100, 200, 150, 260), 72) == (100, 200, 150, 260)


def test_article_split_across_words_is_still_located():
    words = [WordBox("MT190", (100, 100, 160, 120)), WordBox("-016C04", (162, 100, 240, 120))]
    assert find_code_box(words, "MT190-016C04") == (100, 100, 240, 120)


def test_picture_next_to_the_article_wins_over_a_distant_one():
    words = [WordBox("MT190-016", (100, 520, 260, 540))]
    near = ImageCandidate(key="near", bbox=(90, 300, 320, 500), source="raster")
    far = ImageCandidate(key="far", bbox=(900, 1400, 1150, 1600), source="raster")
    matches = match_entries_to_images([{"part_number": "MT190-016"}], words, [near, far], RASTER)
    assert matches[0].kind == "crop"
    assert matches[0].candidate is near


def test_page_without_any_usable_picture_falls_back_to_the_page_preview():
    """The user asked for a picture on every position — a page preview, marked
    as such, beats an empty cell."""
    icon = ImageCandidate(key="i0", bbox=(10, 10, 55, 55), source="inline")  # too small
    matches = match_entries_to_images([{"part_number": "NOT-ON-THIS-PAGE"}], [], [icon], RASTER)
    assert matches[0].kind == "page"
    assert matches[0].candidate is None


def test_repeated_logo_is_not_a_product_picture():
    pages = [[{"signature": "logo", "k": "r0"}, {"signature": f"p{i}", "k": "r1"}] for i in range(20)]
    furniture = furniture_signatures(pages)
    assert "logo" in furniture
    assert "p3" not in furniture


def test_furniture_filter_needs_enough_pages_to_judge():
    """On a three-page price list every picture looks 'repeated'."""
    pages = [[{"signature": "logo"}] for _ in range(3)]
    assert furniture_signatures(pages) == set()


def test_table_page_shares_its_one_illustration():
    """Catalogs build articles from table cells ("1A1" + "150" + "20"), so the
    composed code is nowhere on the page. Measured live: 1 crop out of 68
    positions. One dominant illustration on the page belongs to all of them."""
    dominant = ImageCandidate(key="big", bbox=(100, 400, 900, 1200), source="raster")
    noise = ImageCandidate(key="tiny", bbox=(10, 10, 60, 60), source="inline")
    matches = match_entries_to_images(
        [{"part_number": "1A1-150x20x10"}, {"part_number": "1A1-75x8x8"}],
        [],
        [dominant, noise],
        (3544, 2500),
    )
    assert [m.kind for m in matches] == ["crop", "crop"]
    assert all(m.shared for m in matches)
    assert all(m.candidate is dominant for m in matches)
    assert all(m.score < 0.55 for m in matches), "a shared picture is not a per-item photo"


def test_two_comparable_pictures_are_not_guessed_between():
    left = ImageCandidate(key="a", bbox=(100, 400, 900, 1200), source="raster")
    right = ImageCandidate(key="b", bbox=(1000, 400, 1800, 1200), source="raster")
    matches = match_entries_to_images([{"part_number": "X"}], [], [left, right], (3544, 2500))
    assert matches[0].kind == "page", "attaching the wrong product picture is worse"


def test_variants_table_gets_the_family_picture():
    """30 sizes under 6 illustrations is a variants table — the biggest picture
    belongs to all of them, shared and with a lower confidence."""
    six = [ImageCandidate(key=str(i), bbox=(300 * i, 400, 300 * i + 700, 1100), source="raster")
           for i in range(6)]
    matches = match_entries_to_images(
        [{"part_number": f"1A1-{i}"} for i in range(30)], [], six, (3544, 2500)
    )
    assert all(m.kind == "crop" and m.shared for m in matches)
    assert matches[0].diagnostics["reason"] == "variants_table"


def test_one_picture_per_position_is_assigned_in_reading_order():
    three = [ImageCandidate(key="c", bbox=(100, 1200, 800, 1900), source="raster"),
             ImageCandidate(key="a", bbox=(100, 100, 800, 800), source="raster"),
             ImageCandidate(key="b", bbox=(900, 100, 1600, 800), source="raster")]
    matches = match_entries_to_images(
        [{"part_number": "X1"}, {"part_number": "X2"}, {"part_number": "X3"}],
        [],
        three,
        (3544, 2500),
    )
    assert [m.candidate.key for m in matches] == ["a", "b", "c"], "top-to-bottom, left-to-right"


def test_a_spread_with_more_pictures_than_positions_stays_on_the_page_preview():
    six = [ImageCandidate(key=str(i), bbox=(300 * i, 400, 300 * i + 700, 1100), source="raster")
           for i in range(6)]
    matches = match_entries_to_images(
        [{"part_number": "X"}, {"part_number": "Y"}], [], six, (3544, 2500)
    )
    assert all(m.kind == "page" for m in matches), "guessing which picture is which would lie"


# ── batch embedding ─────────────────────────────────────────────────────────


@__import__("pytest").mark.asyncio
async def test_embed_texts_falls_back_to_one_by_one_when_batch_unsupported():
    """A provider that ignores input_texts must not silently produce vectors of
    the wrong length — the caller would upsert garbage into Qdrant."""
    from unittest.mock import AsyncMock, patch

    from app.ai.embeddings import EmbeddingProfile, embed_texts

    profile = EmbeddingProfile(
        model_key="test_model",
        provider_model="test",
        collection_name="test",
        dimension=4,
        distance_metric="cosine",
        normalize=True,
    )

    class _NoBatch:
        embeddings = None

    with (
        patch("app.ai.embeddings.AIRouter.run", new=AsyncMock(return_value=_NoBatch())),
        patch("app.ai.embeddings.embed_text", new=AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])),
    ):
        vectors = await embed_texts(["a", "b", "c"], profile)

    assert len(vectors) == 3
    assert all(len(vector) == 4 for vector in vectors)
