"""Tests for the deterministic (no extra LLM call) prompt-grounding helper."""

from __future__ import annotations

from app.ai.techdraw_context import build_context_block


def test_empty_description_returns_empty():
    assert build_context_block("") == ""
    assert build_context_block("   ") == ""


def test_no_recognizable_hints_returns_empty():
    assert build_context_block("нарисуй эскиз установки детали на станке") == ""


def test_material_hint_detected():
    block = build_context_block("вал из стали 45 гост 1050-2013")
    assert "Сталь 45 ГОСТ 1050-2013" in block


def test_thread_hint_detected():
    block = build_context_block("вал с резьбой M20 на конце")
    assert "M20" in block and "1.5" in block  # coarse pitch for M20 is 2.5, fine includes 1.5/2.0


def test_thread_hint_ignores_unknown_diameter():
    block = build_context_block("резьба M13 неизвестного диаметра")
    assert "M13" not in block


def test_tolerance_hint_requires_marker_and_diameter():
    block = build_context_block("вал Ø25 с допуском h7")
    assert "квалитета h7" in block
    assert "мкм" in block


def test_tolerance_hint_absent_without_marker():
    block = build_context_block("вал Ø25 длиной 60")
    assert "мкм" not in block


def test_combines_multiple_hints():
    block = build_context_block("вал из стали 40Х с резьбой M24x2 и допуском h6 на Ø45")
    assert "Сталь 40Х" in block
    assert "M24" in block
    assert "мкм" in block
