"""Проверяльщики: узкий вопрос к листу вокруг гипотезы модели (план, Ф1).

Модель читает лист и выдвигает гипотезы с местом на листе; проверяльщик
отвечает на узкий вопрос в маленькой области — подтверждено, опровергнуто
или не измеримо — и возвращает то, что измерил. Он НЕ заменяет прочитанное:
вердикт становится свидетельством в графе и меняет заверенность утверждения
(`proposed → corroborated / contradicted`), а расходящееся измерение — это
конкурирующая гипотеза, решение по которой принимает согласование.

Пакет называется `verifiers`, потому что `cad_recognize.verify` уже занят.
"""

from app.ai.cad_recognize.verifiers.contract import Hypothesis, Verdict
from app.ai.cad_recognize.verifiers.registry import register, registered_kinds, verify
from app.ai.cad_recognize.verifiers.view_frame import ViewFrame

__all__ = ["Hypothesis", "Verdict", "ViewFrame", "register", "registered_kinds", "verify"]

# Регистрация встроенных проверяльщиков — импортом модулей.
from app.ai.cad_recognize.verifiers import (
    bolt_circle,  # noqa: E402,F401
    dimension_line,  # noqa: E402,F401
    plate_hole,  # noqa: E402,F401
)
