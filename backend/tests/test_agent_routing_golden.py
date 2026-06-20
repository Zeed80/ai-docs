"""Golden-set live routing test — covers the agent's routing brain end-to-end.

Sends realistic Russian queries spanning every domain (invoices, documents,
suppliers, warehouse, email, anomalies, analytics, memory/graph), every intent
(smalltalk / flow_status / count / answer_self / analytical_table / table_edit /
document_op / specialist) and the substring traps that the old keyword cascade
got wrong, then asserts the LLM router classifies them correctly.

This is the Phase-0 golden-set promised by the refactor plan. It runs against
the live `fast` model via Ollama and is SKIPPED when Ollama is unreachable, so
the unit suite stays hermetic.
"""

import json
import os
import urllib.error
import urllib.request

import pytest

from app.ai.turn_router import (
    TurnDecision,
    build_router_system,
    build_router_user,
    coerce_channel,
    validate_recommended,
)
from app.api.capability_router import capability_action_map
from app.ai.capability_manifest import load_capability_manifest

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
ROUTER_MODEL = os.environ.get("ROUTER_TEST_MODEL", "gemma4:e2b")
# Allowed accuracy floor — local fast models are not perfect; we assert the
# router is decisively better than the old substring cascade, not flawless.
PASS_THRESHOLD = float(os.environ.get("ROUTER_TEST_THRESHOLD", "0.80"))


def _ollama_up() -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _ollama_up(), reason="Ollama unreachable — live routing golden-set skipped"
)


# Each case: (text, has_open_table, expectations).
# expectations keys: intent (allowed set), not_intent (forbidden set), channel.
GOLDEN: list[tuple[str, bool, dict]] = [
    # — smalltalk —
    ("спасибо, отлично!", False, {"intent": {"smalltalk"}, "channel": "chat"}),
    ("привет, как дела?", False, {"intent": {"smalltalk"}}),
    # — flow-status / count —
    ("что сейчас в работе?", False, {"intent": {"flow_status"}}),
    ("сколько счетов на проверке?", False, {"intent": {"flow_status", "count"}}),
    ("сколько всего поставщиков в базе?", False, {"intent": {"count", "analytical_table", "flow_status"}}),
    # — analytical tables (workspace) —
    ("покажи все счета за май", False, {"intent": {"analytical_table"}, "channel": "workspace"}),
    ("выведи таблицу поставщиков с оборотом", False, {"intent": {"analytical_table"}, "channel": "workspace"}),
    ("разложи затраты по месяцам за этот год", False, {"channel": "workspace"}),
    ("сравни цены на фрезы у разных поставщиков", False, {"channel": "workspace"}),
    ("какие позиции дороже 1000 рублей", False, {"channel": "workspace"}),
    ("топ-10 поставщиков по сумме", False, {"intent": {"analytical_table"}, "channel": "workspace"}),
    # — table edits (open table) —
    ("добавь столбец с НДС перед суммой", True, {"intent": {"table_edit"}}),
    ("отсортируй по убыванию суммы", True, {"intent": {"table_edit"}}),
    ("оставь только поставщика Ромашка", True, {"intent": {"table_edit"}}),
    # — document / invoice ops —
    ("утверди счёт INV-2024-001", False, {"not_intent": {"smalltalk", "flow_status"}}),
    ("покажи детали счёта от Хоффманн", False, {"not_intent": {"smalltalk"}}),
    ("классифицируй последний загруженный документ", False, {"not_intent": {"smalltalk", "count"}}),
    # — email —
    ("составь письмо поставщику с запросом КП", False, {"not_intent": {"count", "smalltalk", "flow_status"}}),
    # — anomalies —
    ("какие аномалии обнаружены на этой неделе", False, {"not_intent": {"smalltalk"}}),
    # — memory / graph (grounding=memory) —
    ("что связано с поставщиком Берёзка?", False, {"not_intent": {"smalltalk", "count"}}),
    ("покажи историю цен на болты М10", False, {"not_intent": {"smalltalk"}}),
    # — SUBSTRING TRAPS (the whole point of the refactor) —
    ("расскажи про расчёт себестоимости фрезеровки", False, {"not_intent": {"count", "flow_status"}}),
    ("объясни, как устроен учёт материалов", False, {"not_intent": {"count", "flow_status"}}),
    ("поставщик Москва не отвечает на письма", False, {"not_intent": {"count"}}),
    ("сколько стоит обучение сотрудника не важно", False, {"not_intent": {"table_edit"}}),
    ("и отправь это письмо поставщику", True, {"not_intent": {"table_edit"}}),
    ("а также уточни сроки поставки", True, {"not_intent": {"table_edit"}}),
]


def _route_once(content: str, has_open_table: bool, system: str, schema: dict) -> TurnDecision:
    user = build_router_user(content, has_open_spec_table=has_open_table)
    payload = {
        "model": ROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "format": schema,
        "stream": False,
        "options": {"temperature": 0},
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.load(r)
    raw = resp["message"]["content"]
    return TurnDecision.model_validate_json(raw)


def test_routing_golden_set():
    amap = capability_action_map()
    descs = {c.name: c.description for c in load_capability_manifest().capabilities}
    system = build_router_system(amap, descs)
    schema = TurnDecision.model_json_schema()

    failures: list[str] = []
    for content, open_table, exp in GOLDEN:
        try:
            d = _route_once(content, open_table, system, schema)
        except Exception as e:  # noqa: BLE001
            failures.append(f"[ERR] {content!r}: {type(e).__name__}: {e}")
            continue
        d = coerce_channel(
            d.model_copy(update={"recommended": validate_recommended(d.recommended, amap)})
        )
        why = []
        if "intent" in exp and d.intent not in exp["intent"]:
            why.append(f"intent={d.intent} not in {exp['intent']}")
        if "not_intent" in exp and d.intent in exp["not_intent"]:
            why.append(f"intent={d.intent} must not be in {exp['not_intent']}")
        if "channel" in exp and d.output_channel != exp["channel"]:
            why.append(f"channel={d.output_channel} != {exp['channel']}")
        # Every recommended tool must be routable (catalog-valid).
        for r in d.recommended:
            if r.capability not in amap:
                why.append(f"unroutable capability {r.capability!r}")
        if why:
            failures.append(f"[XX] {content!r}: " + "; ".join(why))

    accuracy = 1.0 - len(failures) / len(GOLDEN)
    report = (
        f"\nRouting golden-set accuracy: {accuracy:.0%} "
        f"({len(GOLDEN) - len(failures)}/{len(GOLDEN)}) on {ROUTER_MODEL}\n"
        + "\n".join(failures)
    )
    assert accuracy >= PASS_THRESHOLD, report
