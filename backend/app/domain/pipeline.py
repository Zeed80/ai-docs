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


# Supplier-catalog ingestion runs a different set of stages than document
# extraction, but the same DocumentProcessingJob storage and the same frontend
# renderer (which falls back to `label ?? key`), so no UI change is needed.
CATALOG_PIPELINE_STEP_DEFINITIONS: list[tuple[str, str]] = [
    ("store", "Файл сохранен"),
    ("unpack", "Распаковка архива"),
    ("parse", "Разбор каталога"),
    ("normalize", "Нормализация строк"),
    ("entries", "Позиции каталога"),
    ("canonical", "Сопоставление номенклатуры"),
    ("embedding", "Векторизация"),
    ("graph", "Память и граф"),
]
