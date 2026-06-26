"""Phase 3 — learning from user corrections (deterministic core).

A correction is parsed by the same NL→ops machinery as table edits, remembered
against the request that produced the wrong result, and replayed on an identical
later request — so the agent stops repeating the mistake.
"""

from __future__ import annotations

import pytest

from app.ai import corrections


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def setex(self, key, ttl, val):
        self.store[key] = val

    def get(self, key):
        return self.store.get(key)


# ── is_correction ──────────────────────────────────────────────────────────────

def test_is_correction_detects_corrective_phrasing():
    assert corrections.is_correction("нет, группируй по дате")
    assert corrections.is_correction("это не то, я просил среднюю цену")
    assert corrections.is_correction("исправь: сортируй по убыванию")
    assert corrections.is_correction("сгруппируй по поставщику, а не по дате")


def test_is_correction_false_for_new_request():
    assert not corrections.is_correction("выведи все фрезы по поставщику")
    assert not corrections.is_correction("добавь резцы")
    assert not corrections.is_correction("")


# ── correction → ops (deterministic parse) ─────────────────────────────────────

def test_correction_to_ops_grouping():
    ops = corrections.correction_to_ops("invoice_items", "группируй по дате")
    by = {o.op: o for o in ops}
    assert "set_group_by" in by and by["set_group_by"].field == "invoice_date"


def test_correction_to_ops_sort():
    ops = corrections.correction_to_ops("invoice_items", "сортируй по сумме по убыванию")
    sort_ops = [o for o in ops if o.op == "set_sort"]
    assert sort_ops and sort_ops[0].field == "amount" and sort_ops[0].dir == "desc"


def test_correction_to_ops_empty_for_noise():
    assert corrections.correction_to_ops("invoice_items", "ну такое, не очень") == []


# ── record + replay roundtrip ──────────────────────────────────────────────────

def test_record_then_replay_same_request(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(corrections, "_redis", lambda: fake)

    prev = "выведи все фрезы по поставщику"
    learned = corrections.record_correction(prev, "invoice_items", "нет, группируй по дате")
    assert any(o.op == "set_group_by" and o.field == "invoice_date" for o in learned)

    # Same request later → learned ops replayed.
    replay = corrections.learned_ops_for(prev, "invoice_items")
    assert any(o.op == "set_group_by" and o.field == "invoice_date" for o in replay)

    # A DIFFERENT request must not pick up this correction.
    assert corrections.learned_ops_for("покажи склад", "invoice_items") == []


def test_record_correction_with_no_ops_stores_nothing(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(corrections, "_redis", lambda: fake)
    out = corrections.record_correction("запрос", "invoice_items", "спасибо, отлично")
    assert out == []
    assert fake.store == {}
