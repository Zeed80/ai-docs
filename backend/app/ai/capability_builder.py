"""CapabilityBuilder — agent that drafts new skill modules for human review.

Flow:
  1. Orchestrator detects a missing capability (task X can't be done with existing skills)
  2. build_capability(gap) is called
  3. CapabilityBuilder sends gap + code templates to the builder model (code_generation route)
  4. The model writes a Python skill module
  5. Module is saved to backend/app/ai/generated_skills/{skill_name}.py as a DRAFT

Drafts are never registered or imported here: activation goes exclusively
through the capability-proposal flow (sandbox validation → human decision →
promote). Generated code must not run inside the backend process without an
explicit approval — see policy_engine PROTECTED_SETTINGS and the agent
security model.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

_GENERATED_ROOT = Path(__file__).resolve().parent / "generated_skills"

# Template shown to the code-generation model so it knows the conventions.
_CODE_TEMPLATE = '''"""
{description}

SKILL_META stores metadata used by the registry and UI.
execute(args) is the only required public function.
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

SKILL_META = {{
    "name": "{skill_name}",
    "description": "{description}",
    "created_at": "{ts}",
    "source": "agent_generated",
}}


async def execute(args: dict[str, Any]) -> dict[str, Any]:
    """
    Implement the skill logic here.
    args contains the parameters passed from the orchestrator.
    Return a dict that can be shown to the user or used in workspace blocks.
    """
    raise NotImplementedError("Skill body not yet implemented")
'''

_SYSTEM_PROMPT = """Ты — AgentDeveloper, лучший Python-разработчик команды ИИ.
Твоя задача: написать РАБОЧИЙ Python-модуль для нового агентского скилла.

## Правила кода
- Файл = один модуль Python с `async def execute(args: dict) -> dict`
- Переменная `SKILL_META` = dict с ключами name, description, created_at, source
- Импортируй только стандартную библиотеку или пакеты из backend (fastapi, sqlalchemy, httpx, pydantic)
- Для доступа к БД используй: `from app.db.session import async_session_factory`
- Для HTTP-запросов к backend API: `import httpx; async with httpx.AsyncClient() as c: c.post("http://localhost:8000/api/...")`
- Возвращай структурированный dict с ключами: status, message, data (опционально), canvas (опционально)
- НЕ возвращай HTML, только JSON-сериализуемые данные
- Если нужны данные из БД, используй SQLAlchemy async session
- Если нужны данные из внешних API, используй httpx AsyncClient
- Код должен быть production-ready: обработка ошибок, логирование через structlog

## Табличный API (для скриптов-отчётов по таблицам)
Не собирай таблицы вручную — пользуйся готовыми endpoint'ами (httpx, base
`http://localhost:8000`):
- `POST /api/workspace/agent/spec-table` body={canvas_id, spec:{source, columns,
  filters, sort}} — таблица из БД целиком (источники: invoices, invoice_items,
  suppliers, warehouse, documents, payments, anomalies, emails, drawings,
  vector_search, graph_query). Справочник: `GET /api/workspace/agent/spec-table/catalog`.
- `POST /api/workspace/agent/generated/sql-table` body={task, limit} — свободный
  валидированный SELECT (только чтение).
- Листы (редактируемый Excel, без записи в боевую БД): `POST /api/workspace/sheets/create`
  body={title, columns:[{key,header,type,formula?}], rows}; затем
  `/api/workspace/sheets/{id}/patch-cells|add-row|add-column|set-formula`.
  Формулы: =A1*B1, =SUM(A1:A10), =ROUND(quantity*price,2).
- `POST /api/tables/query` — данные раздела (фильтры/сортировка/поиск).
- Запись в боевые таблицы — ТОЛЬКО через типизированные endpoint'ы с approval,
  напрямую SQL-UPDATE/DELETE не делай.

## Структура ответа
Верни ТОЛЬКО Python-код модуля. Никаких markdown-блоков, никаких объяснений — ТОЛЬКО код.
Код должен быть полностью работоспособным при `await execute({})`.
"""


@dataclass
class CapabilityBuildResult:
    skill_name: str
    skill_path: str | None
    registry_updated: bool
    errors: list[str] = field(default_factory=list)
    code: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.skill_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "skill_path": self.skill_path,
            "registry_updated": self.registry_updated,
            "ok": self.ok,
            "errors": self.errors,
        }


async def build_capability(
    gap_description: str,
    skill_name: str | None = None,
    *,
    context_skills: list[str] | None = None,
) -> CapabilityBuildResult:
    """Generate, write, and register a new skill from a capability gap description.

    Args:
        gap_description: What the user tried to do but couldn't — orchestrator fills this.
        skill_name: Optional explicit skill name (e.g. "reports.monthly_kpi").
                    If None, the model chooses the name.
        context_skills: Names of similar existing skills to use as examples.
    """
    from app.ai.router import AIRouter
    from app.ai.schemas import AIRequest, AITask, ChatMessage

    ai_router = AIRouter()
    skill_name = skill_name or _derive_skill_name(gap_description)
    ts = datetime.now(timezone.utc).isoformat()

    context = _build_context(gap_description, skill_name, context_skills or [], ts)

    try:
        response = await ai_router.run(
            AIRequest(
                task=AITask.CODE_GENERATION,
                messages=[
                    ChatMessage(role="system", content=_SYSTEM_PROMPT),
                    ChatMessage(role="user", content=context),
                ],
                confidential=False,
                allow_cloud=True,
            )
        )
        code = _extract_code(str(response.content))
    except Exception as exc:
        logger.error("capability_builder_llm_failed", gap=gap_description[:200], error=str(exc))
        code = _fallback_stub(skill_name, gap_description, ts)

    # Self-refinement loop: critique → revise up to 3 rounds
    try:
        from app.ai.self_refine import refine_code

        async def _gen(prompt: str, system: str | None) -> str:
            r = await ai_router.run(
                AIRequest(
                    task=AITask.CODE_GENERATION,
                    messages=[
                        *(
                            [ChatMessage(role="system", content=system)]
                            if system else
                            [ChatMessage(role="system", content=_SYSTEM_PROMPT)]
                        ),
                        ChatMessage(role="user", content=prompt),
                    ],
                    confidential=False,
                    allow_cloud=True,
                )
            )
            return _extract_code(str(r.content))

        refine_result = await refine_code(code, gap_description, _gen, max_rounds=2)
        if refine_result.final_score > 6.0:
            code = refine_result.final_response
            logger.info(
                "capability_builder_refined",
                skill=skill_name,
                rounds=refine_result.rounds_used,
                score=refine_result.final_score,
            )
    except Exception as exc:
        logger.warning("capability_builder_refine_skipped", skill=skill_name, error=str(exc))

    errors: list[str] = []
    skill_path: str | None = None

    # Validate the generated code compiles
    try:
        compile(code, f"<{skill_name}>", "exec")
    except SyntaxError as exc:
        errors.append(f"SyntaxError in generated code: {exc}")
        code = _fallback_stub(skill_name, gap_description, ts)

    # Write to generated_skills/
    try:
        _GENERATED_ROOT.mkdir(parents=True, exist_ok=True)
        safe_name = _sanitize_skill_filename(skill_name)
        skill_file = _GENERATED_ROOT / f"{safe_name}.py"
        skill_file.write_text(code, encoding="utf-8")
        skill_path = str(skill_file)
        logger.info("capability_builder_skill_written", skill=skill_name, path=str(skill_file))
    except Exception as exc:
        errors.append(f"Failed to write skill file: {exc}")

    # The draft stops here. Registration and module import are owned by the
    # capability-proposal flow (sandbox → human decide → promote); importing
    # agent-written code into this process without approval is an RCE vector.
    return CapabilityBuildResult(
        skill_name=skill_name,
        skill_path=skill_path,
        registry_updated=False,
        errors=errors,
        code=code,
    )


def _build_context(
    gap: str,
    skill_name: str,
    context_skills: list[str],
    ts: str,
) -> str:
    template = _CODE_TEMPLATE.format(
        description=gap[:300],
        skill_name=skill_name,
        ts=ts,
    )
    existing = _load_existing_examples(context_skills)
    parts = [
        f"## Задача\nСоздай новый скилл **{skill_name}** для следующей задачи:\n\n{gap}",
        f"## Шаблон модуля\n```python\n{template}\n```",
    ]
    if existing:
        parts.append(f"## Примеры существующих скиллов\n{existing}")
    parts.append(
        "## Что нужно сделать\n"
        "Напиши ПОЛНЫЙ Python-модуль. Замени `raise NotImplementedError` на реальную реализацию. "
        "Верни ТОЛЬКО Python-код без markdown и объяснений."
    )
    return "\n\n".join(parts)


def _load_existing_examples(skill_names: list[str]) -> str:
    if not skill_names:
        return ""
    # Load a compact excerpt from workspace.py as a reference pattern
    example_path = Path(__file__).resolve().parent.parent / "api" / "workspace.py"
    if not example_path.exists():
        return ""
    text = example_path.read_text(encoding="utf-8")
    # Return first 80 lines as pattern reference
    lines = text.splitlines()[:80]
    return f"```python\n# Pattern from workspace.py\n" + "\n".join(lines) + "\n```"


def _extract_code(raw: str) -> str:
    """Strip markdown fences from LLM response if present."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        start = 1
        end = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip() == "```":
                end = i
                break
        return "\n".join(lines[start:end]).strip()
    return raw


def _fallback_stub(skill_name: str, description: str, ts: str) -> str:
    return textwrap.dedent(f'''\
        """
        {description[:200]}
        Auto-generated fallback stub — replace execute() with real logic.
        """

        from __future__ import annotations
        from typing import Any

        SKILL_META = {{
            "name": "{skill_name}",
            "description": "{description[:120]}",
            "created_at": "{ts}",
            "source": "agent_generated_stub",
        }}


        async def execute(args: dict[str, Any]) -> dict[str, Any]:
            return {{
                "status": "stub",
                "message": "Скилл создан, требует реализации",
                "skill": "{skill_name}",
                "args_received": args,
            }}
    ''')


def _derive_skill_name(gap: str) -> str:
    """Derive a snake_case skill name from the gap description."""
    lower = gap.lower()[:80]
    # Extract meaningful words
    words = re.findall(r"[а-яёa-z]+", lower)
    # Take up to 3 meaningful words
    stopwords = {"для", "из", "по", "в", "с", "на", "к", "и", "или", "the", "a", "for", "from"}
    clean = [w for w in words if w not in stopwords and len(w) > 2][:3]
    if not clean:
        return "agent_generated_skill"
    return "agent." + "_".join(clean[:3])


def _sanitize_skill_filename(skill_name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", skill_name).strip("_")


# NOTE: legacy `_register_skill` (append to _registry.yml), `_hot_reload_module`
# and the reload-signal helpers were removed: the legacy registry is frozen
# (read-only) and generated code is only activated through the proposal flow.
