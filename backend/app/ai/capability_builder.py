"""CapabilityBuilder — agent that writes new skills and registers them live.

Flow:
  1. Orchestrator detects a missing capability (task X can't be done with existing skills)
  2. build_capability(gap) is called
  3. CapabilityBuilder sends gap + code templates to Claude API (code_generation route)
  4. Claude writes a real Python skill module
  5. Module is saved to backend/app/ai/generated_skills/{skill_name}.py
  6. Skill entry is appended to aiagent/skills/_registry.yml
  7. agent_loop is signalled to reload its skill map
  8. Returns the new skill name for immediate use by orchestrator
"""

from __future__ import annotations

import importlib
import json
import re
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger()

_GENERATED_ROOT = Path(__file__).resolve().parent / "generated_skills"
_REGISTRY_PATH = (
    Path(__file__).resolve().parents[2]
    / "aiagent" / "skills" / "_registry.yml"
)

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

    # Register in _registry.yml
    registry_updated = False
    if not errors:
        try:
            registry_updated = _register_skill(skill_name, gap_description)
        except Exception as exc:
            errors.append(f"Failed to register skill: {exc}")

    # Hot-reload skill module
    if skill_path and not errors:
        try:
            _hot_reload_module(skill_name)
        except Exception as exc:
            logger.warning("capability_builder_hot_reload_failed", skill=skill_name, error=str(exc))

    # Signal agent_loop instances to reload their skill maps
    if registry_updated:
        _signal_agent_loop_reload()

    return CapabilityBuildResult(
        skill_name=skill_name,
        skill_path=skill_path,
        registry_updated=registry_updated,
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


def _register_skill(skill_name: str, description: str) -> bool:
    """Append skill entry to _registry.yml and gateway.yml exposed list."""
    if not _REGISTRY_PATH.exists():
        logger.warning("capability_builder_registry_missing", path=str(_REGISTRY_PATH))
        return False

    data = yaml.safe_load(_REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    skills: list[dict] = data.get("skills") or []

    # Check duplicate
    if any(s.get("name") == skill_name for s in skills):
        logger.info("capability_builder_skill_already_registered", skill=skill_name)
        return True

    safe_name = _sanitize_skill_filename(skill_name)
    entry: dict[str, Any] = {
        "name": skill_name,
        "description": f"Agent-generated: {description[:120]}",
        "category": "agent_generated",
        "method": "POST",
        "path": f"/api/agent/generated-skill/{safe_name}",
        "approval_required": False,
        "body_params": [
            {"name": "args", "type": "object", "required": False,
             "description": "Skill-specific arguments"},
        ],
    }
    skills.append(entry)
    data["skills"] = skills
    _REGISTRY_PATH.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    logger.info("capability_builder_skill_registered", skill=skill_name)
    return True


def _hot_reload_module(skill_name: str) -> None:
    safe = _sanitize_skill_filename(skill_name)
    module_name = f"app.ai.generated_skills.{safe}"
    if module_name in sys.modules:
        importlib.reload(sys.modules[module_name])


def _signal_agent_loop_reload() -> None:
    """Notify all active AgentSession instances to reload their skill maps."""
    try:
        from app.ai.agent_loop import reload_all_sessions
        reload_all_sessions()
    except Exception as exc:
        logger.warning("capability_builder_agent_loop_signal_failed", error=str(exc))
