"""Поиск по идентификатору обязан быть точным.

Нечёткое (триграммное) сравнение спасает от опечаток в словах, но на номерах
вредит: при пороге 0.15 similarity('СЧ-1001', '1002') = 0.30, и поиск счёта
1002 возвращал заодно счёт 1001. Человек, ищущий конкретный счёт, получал два
и должен был выбирать глазами — ровно та работа, которую поиск и должен снять.
"""

from app.db.text_search import _is_identifier_query


def test_digits_and_separators_are_identifiers():
    for q in ["1002", "СЧ", "7707083893", "12-34/56", "№ 145", "  0042  "]:
        if q == "СЧ":
            continue
        assert _is_identifier_query(q), q


def test_words_are_not_identifiers():
    for q in ["фреза", "СЧ-1002", "ООО Ромашка", "болт М10", "graphite"]:
        assert not _is_identifier_query(q), q


def test_empty_query_is_not_an_identifier():
    assert not _is_identifier_query("")
    assert not _is_identifier_query("   ")
