"""Ни одна задача с пользовательским контентом не остаётся без решения.

Задача, через которую проходит содержимое документа, письма или чертежа,
обязана быть в одном из двух списков:

* `CONFIDENTIAL_TASKS` — жёсткий запрет: облачную модель нельзя ни назначить,
  ни вызвать, роутер принудительно ставит local_only;
* `CLOUD_OPT_IN_TASKS` — облако разрешено CLAUDE.md (planner/auditor, письма),
  но слот всё равно локален по умолчанию и включается защищённым действием.

Смысл теста — не в самих списках, а в том, что появление новой задачи требует
осознанного решения. Задача, забытая в обоих списках, получает облако молча:
это ровно тот класс дефекта, при котором тело делового письма уходит наружу и
никто об этом не узнаёт.
"""

from __future__ import annotations

import pytest

from app.ai.schemas import AITask
from app.ai.task_routing import CLOUD_OPT_IN_TASKS, CONFIDENTIAL_TASKS, get_routing_for

# Задачи, которые пользовательского контента не видят: они работают с кодом,
# метаданными или служебными строками.
_NO_USER_CONTENT = {
    AITask.CODE_GENERATION,
}


def _content_bearing() -> set[AITask]:
    return set(AITask) - _NO_USER_CONTENT


def test_every_content_bearing_task_has_a_decision():
    undecided = _content_bearing() - CONFIDENTIAL_TASKS - CLOUD_OPT_IN_TASKS
    assert not undecided, (
        "у этих задач нет решения по облаку — они получат его молча: "
        + ", ".join(sorted(t.value for t in undecided))
    )


def test_the_two_lists_do_not_overlap():
    """Задача либо запрещена жёстко, либо включается по решению — не оба."""
    assert not (CONFIDENTIAL_TASKS & CLOUD_OPT_IN_TASKS)


@pytest.mark.parametrize(
    "task",
    [AITask.CLASSIFICATION, AITask.LONG_CONTEXT_SUMMARIZATION, AITask.ENGINEERING_REASONING],
)
def test_document_reading_tasks_are_hard_locked(task):
    """Эти три читают документ целиком, и разрешения на облако им не давали."""
    assert task in CONFIDENTIAL_TASKS
    routing = get_routing_for(task)
    assert routing.local_only is True
    assert routing.allow_cloud is False


@pytest.mark.parametrize("task", sorted(CLOUD_OPT_IN_TASKS, key=lambda t: t.value))
def test_opt_in_tasks_are_local_until_someone_decides_otherwise(task):
    """Разрешение облака — действие, а не состояние по умолчанию."""
    routing = get_routing_for(task)
    assert routing.local_only is True, f"{task.value} должна быть локальной по умолчанию"
