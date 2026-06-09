"""Tests for the secretary morning-briefing formatter (pure function)."""

from __future__ import annotations

from app.tasks.proactive import _format_briefing


def test_empty_stats_produce_no_briefing():
    assert _format_briefing({}) == ""
    assert _format_briefing({k: 0 for k in (
        "overdue_payments", "pending_approvals", "open_anomalies",
        "payments_due_soon", "documents_needs_review", "quarantine_count",
        "unread_emails",
    )}) == ""


def test_urgent_items_go_red():
    text = _format_briefing({
        "overdue_payments": 2,
        "pending_approvals": 3,
        "open_anomalies": 1,
    })
    assert "🔴" in text
    assert "просроченные платежи: 2" in text
    assert "ждут согласования: 3" in text
    assert "открытые аномалии: 1" in text
    # No yellow/green sections when those buckets are empty
    assert "🟡" not in text
    assert "🟢" not in text


def test_today_and_info_buckets():
    text = _format_briefing({
        "payments_due_soon": 4,
        "documents_needs_review": 5,
        "quarantine_count": 1,
        "unread_emails": 7,
    })
    assert "🟡 На сегодня:" in text
    assert "оплаты в ближайшие 3 дня: 4" in text
    assert "документы на проверке: 5" in text
    assert "🟢 К сведению: непрочитанные письма: 7" in text
    assert "🔴" not in text


def test_custom_opener_is_used():
    text = _format_briefing({"overdue_payments": 1}, opener="Привет! Вот что важно:")
    assert text.splitlines()[0] == "Привет! Вот что важно:"
    assert "🔴" in text


def test_all_buckets_ordered_red_yellow_green():
    text = _format_briefing({
        "overdue_payments": 1,
        "payments_due_soon": 1,
        "unread_emails": 1,
    })
    lines = text.splitlines()
    assert lines[1].startswith("🔴")
    assert lines[2].startswith("🟡")
    assert lines[3].startswith("🟢")


# ── Duplicate-invoice alert formatter ─────────────────────────────────────────

from app.tasks.proactive import _format_duplicate_alert  # noqa: E402


def test_duplicate_alert_mentions_number_and_reason():
    text = _format_duplicate_alert("INV-42", 15000.0, "RUB", "duplicate_hash_and_number")
    assert "INV-42" in text
    assert "🔁" in text
    assert "совпадает и файл, и номер" in text
    assert "отклонить" in text.lower()


def test_duplicate_alert_without_amount():
    text = _format_duplicate_alert("INV-7", None, "RUB", "duplicate_supplier_number")
    assert "INV-7" in text
    assert "RUB" not in text  # no amount → no currency
    assert "поставщик и номер" in text


def test_duplicate_alert_unknown_status_falls_back():
    text = _format_duplicate_alert("INV-1", 100.0, "USD", "weird_status")
    assert "признаки дубликата" in text
    assert "100" in text and "USD" in text
