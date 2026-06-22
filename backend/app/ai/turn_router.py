"""LLM-first turn router — replaces the keyword-substring routing cascade.

A single structured-output generation classifies the user's turn into a typed
``TurnDecision`` (intent, role, output channel, grounding, recommended tools).
The orchestrator then dispatches by *meaning* instead of substring membership
(`marker in text`), which silently misfired on "расчёт"→"счет", "сравн", "стоит",
etc. and could hijack the user's intent.

Design notes
------------
* The router is the planner — it does NOT add an LLM call on top of the existing
  one. For turns that previously hit a 0-LLM keyword fast-path it is +1 cheap
  ``fast``-model call; for previously-planned turns it replaces the planner.
* Two-tier fallback (never to keyword heuristics): on low confidence / invalid
  schema / timeout the caller escalates to a larger model with the same schema,
  and only then to a safe structural default.
* The ``recommended`` tools and ``action`` values are validated against the
  single source of truth — ``capability_router._DISPATCH`` via
  ``capability_action_map()`` — so the router can never recommend an
  unroutable tool.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, Field, model_validator

# Kept in sync with orchestrator.WorkerRole and aiagent/config/gateway.yml roles.
# Defined here (not imported from orchestrator) to avoid a circular import.
RouterRole = Literal[
    "data_analyst",
    "invoice_specialist",
    "warehouse_specialist",
    "procurement_specialist",
    "accountant",
    "engineer",
    "technologist",
    "memory_researcher",
]

TurnIntent = Literal[
    "smalltalk",        # greeting / chit-chat — no tools, no data
    "flow_status",      # "что в работе?", queue/pipeline status — secretary answers
    "count",            # deterministic "сколько …" count question
    "answer_self",      # factual answer the worker gives after grounding
    "analytical_table", # build/show a table or analytical breakdown on the desktop
    "table_edit",       # edit an already-open spec table ("добавь столбец…")
    "document_op",      # operate on a specific document/invoice (get/classify/…)
    "specialist",       # delegate to a role specialist for multi-step work
    "capability_gap",   # no tool can satisfy this — surface a gap
]

GroundingMode = Literal[
    "none",        # no retrieval needed (smalltalk, flow-status, table from SQL)
    "structured",  # answer from structured DB (spec_table / list / count)
    "rag",         # needs document retrieval (hybrid search over content)
    "memory",      # needs knowledge-graph / memory facts
]

OutputChannel = Literal["chat", "workspace"]


class RecommendedTool(BaseModel):
    capability: str
    action: str = ""


class TurnDecision(BaseModel):
    """Typed routing decision for one user turn."""

    intent: TurnIntent = "specialist"
    role: RouterRole = "data_analyst"
    output_channel: OutputChannel = "chat"
    grounding: GroundingMode = "none"
    recommended: list[RecommendedTool] = Field(default_factory=list)
    # Named entities extracted from the turn (supplier_name, date_1, number_1…).
    # Used as the primary source for recipe parameter slots; the regex extractor
    # is the fallback. Keep keys stable so recipes resolve consistently.
    entities: dict[str, str] = Field(default_factory=dict)
    goal: str = ""
    confidence: float = 0.0

    @model_validator(mode="before")
    @classmethod
    def _coerce_enums(cls, data):
        """Sanitize imperfect model output to valid enum members.

        Local models occasionally emit ``role: null``, ``role: "email"`` (a
        capability name) or an off-by-one intent. Rather than rejecting the whole
        decision (→ fall back to safe default), coerce each enum field to a valid
        value with a safe default. The JSON-schema still advertises the enum, so
        well-behaved models stay constrained; this only rescues the rest.
        """
        if not isinstance(data, dict):
            return data
        out = dict(data)

        def _coerce(field: str, allowed: tuple[str, ...], default: str) -> None:
            val = out.get(field)
            out[field] = val if isinstance(val, str) and val in allowed else default

        _coerce("intent", get_args(TurnIntent), "specialist")
        _coerce("role", get_args(RouterRole), "data_analyst")
        _coerce("output_channel", get_args(OutputChannel), "chat")
        _coerce("grounding", get_args(GroundingMode), "none")

        # recommended: accept list of dicts or bare capability strings; drop junk.
        rec = out.get("recommended")
        norm_rec = []
        if isinstance(rec, list):
            for item in rec:
                if isinstance(item, str):
                    norm_rec.append({"capability": item, "action": ""})
                elif isinstance(item, dict) and item.get("capability"):
                    norm_rec.append({
                        "capability": str(item["capability"]),
                        "action": str(item.get("action") or ""),
                    })
                elif getattr(item, "capability", None):
                    # Already a RecommendedTool (direct construction) — keep it.
                    norm_rec.append({
                        "capability": str(item.capability),
                        "action": str(getattr(item, "action", "") or ""),
                    })
        out["recommended"] = norm_rec

        # entities: keep only string→string pairs (models sometimes emit numbers).
        ent = out.get("entities")
        out["entities"] = (
            {str(k): str(v) for k, v in ent.items() if v is not None}
            if isinstance(ent, dict)
            else {}
        )
        return out

    @property
    def is_workspace(self) -> bool:
        return self.output_channel == "workspace"


# Intents whose natural home is the desktop (structural/analytical results).
_WORKSPACE_INTENTS = {"analytical_table", "table_edit"}


def safe_default_decision(content: str = "") -> TurnDecision:
    """Structural fallback when the LLM router is unavailable/untrusted.

    Deliberately keyword-free: delegate to a specialist on the chat channel and
    let the worker (with the full enum-constrained catalog) decide. The audit
    contour catches an ungrounded/empty answer downstream.
    """
    return TurnDecision(
        intent="specialist",
        role="data_analyst",
        output_channel="chat",
        grounding="none",
        recommended=[],
        goal=content[:200],
        confidence=0.0,
    )


def validate_recommended(
    recommended: list[RecommendedTool],
    action_map: dict[str, list[str]],
) -> list[RecommendedTool]:
    """Drop any recommended tool/action not present in the dispatch catalog."""
    valid: list[RecommendedTool] = []
    for tool in recommended:
        actions = action_map.get(tool.capability)
        if actions is None:
            continue
        if tool.action and tool.action not in actions:
            # Capability is real but the action is hallucinated — keep the
            # capability, drop the bad action (worker still sees the enum).
            valid.append(RecommendedTool(capability=tool.capability, action=""))
        else:
            valid.append(tool)
    return valid


def coerce_channel(decision: TurnDecision) -> TurnDecision:
    """Enforce the product rule: structural/analytical intents default to the
    desktop even if the model picked 'chat' (Phase 5 — desktop by result type)."""
    if decision.intent in _WORKSPACE_INTENTS and decision.output_channel != "workspace":
        decision = decision.model_copy(update={"output_channel": "workspace"})
    return decision


def build_router_system(action_map: dict[str, list[str]], catalog_descriptions: dict[str, str]) -> str:
    """System prompt: enumerate the real catalog + the typed decision contract."""
    from app.ai.agent_config import get_builtin_agent_config

    agent_name = get_builtin_agent_config().agent_name
    lines = [
        f"Ты — маршрутизатор ходов ИИ-секретаря «{agent_name}» промышленного предприятия.",
        "Определи НАМЕРЕНИЕ пользователя и верни строго структурированное решение.",
        "Не реагируй на отдельные слова-триггеры — оценивай смысл всей фразы.",
        "",
        "Поля решения:",
        "- intent: тип хода (см. ниже).",
        "- role: роль-специалист, если intent=specialist/analytical_table/document_op.",
        "- output_channel: 'workspace' если результат — таблица/список/аналитика/"
        "сравнение/группировка (по умолчанию для них); 'chat' для коротких текстовых ответов.",
        "- grounding: 'structured' (данные из БД: таблицы, счета, подсчёты), "
        "'rag' (поиск по содержимому документов), 'memory' (граф знаний/связи/история), "
        "'none' (приветствие, статус потока).",
        "- recommended: 1-3 инструмента {capability, action} ТОЛЬКО из каталога ниже.",
        "- entities: именованные сущности из запроса (supplier_name, date_1, number_1, "
        "quoted_1) — если явно названы; иначе пустой объект.",
        "- goal: краткая формулировка задачи.",
        "- confidence: 0..1 — насколько ты уверен в классификации.",
        "",
        "Значения intent:",
        "- smalltalk: приветствие, благодарность, болтовня — без инструментов.",
        "- flow_status: «что в работе», «сколько в очереди», статус обработки.",
        "- count: детерминированный вопрос «сколько …».",
        "- answer_self: фактический вопрос, ответ после получения данных.",
        "- analytical_table: построить/показать таблицу, список, сводку, сравнение.",
        "- table_edit: правка уже открытой таблицы («добавь столбец», «отсортируй», «оставь только»).",
        "- document_op: операция с конкретным документом/счётом.",
        "- specialist: многошаговая работа специалиста.",
        "- capability_gap: ни один инструмент не подходит.",
        "",
        "Каталог инструментов (capability: actions):",
    ]
    for cap in sorted(action_map):
        desc = (catalog_descriptions.get(cap) or "").strip().replace("\n", " ")
        actions = ", ".join(action_map[cap])
        head = f"- {cap}"
        if desc:
            head += f" — {desc[:160]}"
        lines.append(head)
        lines.append(f"    actions: {actions}")
    return "\n".join(lines)


def build_router_user(content: str, *, has_open_spec_table: bool, history_summary: str = "") -> str:
    """User-message context for the router."""
    parts = []
    if history_summary:
        parts.append(f"Недавний контекст диалога:\n{history_summary}")
    parts.append(f"Открыта ли сейчас таблица на Рабочем столе: {'да' if has_open_spec_table else 'нет'}.")
    parts.append(
        "Если открыта таблица и пользователь явно её правит — intent=table_edit. "
        "Фраза, начинающаяся с «и…»/«добавь…», НЕ обязательно правка таблицы: "
        "оцени смысл (это может быть новое действие или вопрос)."
    )
    parts.append(f"Сообщение пользователя:\n{content}")
    return "\n\n".join(parts)


def _looks_defaulted(d: "TurnDecision | None") -> bool:
    """True when a decision is the all-defaults shell produced by validating {}.

    Some local models (e.g. Qwopus) ignore the JSON-schema constraint and answer
    in YAML/markdown bullets; the router's JSON parse then fails and yields a
    defaulted TurnDecision (specialist/chat/0.0). Detect that so we can re-parse
    the raw text leniently instead of mis-routing.
    """
    return (
        d is None
        or (d.intent == "specialist" and d.confidence == 0.0 and not d.recommended)
    )


def lenient_parse_decision(text: str) -> dict | None:
    """Parse a router decision from imperfect model output (JSON or YAML bullets)."""
    if not text:
        return None
    import json as _json
    import re as _re

    cleaned = text.strip()
    # Strip <think>…</think> reasoning blocks (Qwopus/qwen3 emit them even when
    # the role's thinking toggle is off) before any JSON/YAML parse.
    cleaned = _re.sub(r"<\s*think(?:ing)?\s*>[\s\S]*?</\s*think(?:ing)?\s*>", "", cleaned, flags=_re.IGNORECASE)
    # Drop a dangling unclosed <think> preamble (model truncated mid-reasoning).
    cleaned = _re.sub(r"^[\s\S]*?</\s*think(?:ing)?\s*>", "", cleaned, flags=_re.IGNORECASE)
    cleaned = cleaned.strip()
    # Strip ```json / ``` fences.
    cleaned = _re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", cleaned).strip()

    # 1) Direct JSON.
    try:
        obj = _json.loads(cleaned)
        if isinstance(obj, dict) and "intent" in obj:
            return obj
    except Exception:
        pass
    # 2) Robust JSON extraction (greedy object + brace-repair) — handles a JSON
    #    decision wrapped in prose/preamble with nested objects (recommended /
    #    entities), which a non-greedy {…} scan would split mid-object.
    try:
        from app.ai.structured_output import extract_json_from_text

        extracted = extract_json_from_text(cleaned)
        if extracted:
            obj = _json.loads(extracted)
            if isinstance(obj, dict) and "intent" in obj:
                return obj
    except Exception:
        pass
    # 3) YAML bullets ("- key: value" / "key: value").
    try:
        import yaml as _yaml

        stripped = "\n".join(
            _re.sub(r"^\s*-\s+", "", line) for line in cleaned.splitlines()
        )
        obj = _yaml.safe_load(stripped)
        if isinstance(obj, list):  # list of single-key maps → merge
            merged: dict = {}
            for item in obj:
                if isinstance(item, dict):
                    merged.update(item)
            obj = merged
        if isinstance(obj, dict) and obj:
            return obj
    except Exception:
        pass
    return None


def _catalog() -> tuple[dict[str, list[str]], dict[str, str]]:
    """(action_map, descriptions) from the single source of truth."""
    from app.api.capability_router import capability_action_map
    from app.ai.capability_manifest import load_capability_manifest

    action_map = capability_action_map()
    descriptions: dict[str, str] = {}
    try:
        manifest = load_capability_manifest()
        descriptions = {c.name: c.description for c in manifest.capabilities}
    except Exception:
        pass
    return action_map, descriptions


async def route_turn(
    content: str,
    *,
    preferred_model: str | None,
    timeout: float,
    has_open_spec_table: bool = False,
    history_summary: str = "",
    thinking: bool | None = None,
) -> tuple[TurnDecision | None, str]:
    """Run one structured-output routing generation.

    Returns ``(decision, source)``. ``decision`` is ``None`` on
    timeout/error/invalid-schema so the caller can escalate (two-tier) or fall
    back to ``safe_default_decision`` — never to keyword heuristics.
    The returned decision has its ``recommended`` validated against the catalog
    and its channel coerced (structural intents → workspace).
    """
    import asyncio

    from app.ai.router import ai_router
    from app.ai.schemas import AIRequest, AITask, ChatMessage

    action_map, descriptions = _catalog()
    system = build_router_system(action_map, descriptions)
    user = build_router_user(
        content, has_open_spec_table=has_open_spec_table, history_summary=history_summary
    )
    try:
        response = await asyncio.wait_for(
            ai_router.run(
                AIRequest(
                    task=AITask.ORCHESTRATOR_PLANNING,
                    messages=[
                        ChatMessage(role="system", content=system),
                        ChatMessage(role="user", content=user),
                    ],
                    response_schema=TurnDecision,
                    confidential=False,
                    allow_cloud=False,  # router runs locally — confidential-safe
                    preferred_model=preferred_model,
                    thinking=thinking,  # per-assignment override (agent_fast slot)
                )
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return None, "timeout"
    except Exception:
        return None, "error"

    decision = response.data if isinstance(response.data, TurnDecision) else None

    # Rescue models that ignore JSON-schema and answer in YAML/markdown: the
    # router's parse then yields a defaulted shell. Re-parse the raw text.
    source = "model"
    if _looks_defaulted(decision):
        raw = getattr(response, "text", "") or ""
        parsed = lenient_parse_decision(raw)
        if parsed:
            try:
                relaxed = TurnDecision.model_validate(parsed)
                if not _looks_defaulted(relaxed):
                    decision, source = relaxed, "model_lenient"
            except Exception:
                pass

    if decision is None:
        return None, "invalid_schema"

    decision = decision.model_copy(
        update={"recommended": validate_recommended(decision.recommended, action_map)}
    )
    decision = coerce_channel(decision)
    return decision, source
