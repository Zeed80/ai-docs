"""Shared pipeline step definitions — single source of truth."""

PIPELINE_STEP_DEFINITIONS: list[tuple[str, str]] = [
    ("store", "Файл сохранен"),
    ("memory_seed", "Первичная память"),
    ("classification", "Классификация"),
    ("extraction", "Распознавание"),
    ("sql_records", "Записи SQL"),
    ("memory_graph", "Память и граф"),
    ("embedding", "Векторизация"),
]
