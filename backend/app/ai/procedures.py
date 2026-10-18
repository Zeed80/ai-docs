"""Procedures: domain know-how the agent reads on demand.

Each ``aiagent/procedures/<name>.md`` opens with a front matter holding its
``name`` and a one-line ``description``. Only the index (name — description)
sits in the system prompt; the text arrives as a tool result when the model
asks for it, so the head of every request stays the same and the prompt cache
keeps it. Procedures are protected data like the prompt: they change only
through a reviewed edit, never by the agent itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.ai.degradation import log_degraded
from app.ai.gateway_config import _AIAGENT_ROOT

PROCEDURES_DIR = _AIAGENT_ROOT / "procedures"

_FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class Procedure:
    name: str
    description: str
    text: str


def _parse(path: Path) -> Procedure | None:
    raw = path.read_text(encoding="utf-8")
    match = _FRONT_MATTER_RE.match(raw)
    if not match:
        return None
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        log_degraded("procedures.front_matter", exc, path=str(path))
        return None
    if not isinstance(meta, dict):
        return None
    name = str(meta.get("name") or "").strip()
    description = " ".join(str(meta.get("description") or "").split())
    if name != path.stem or not _NAME_RE.match(name) or not description:
        return None
    return Procedure(name=name, description=description, text=raw[match.end() :].strip())


def list_procedures(directory: Path | None = None) -> list[Procedure]:
    root = directory or PROCEDURES_DIR
    if not root.is_dir():
        return []
    found = (_parse(path) for path in sorted(root.glob("*.md")))
    return [proc for proc in found if proc is not None]


def read_procedure(name: str, directory: Path | None = None) -> Procedure | None:
    if not _NAME_RE.match(name or ""):
        return None
    for proc in list_procedures(directory):
        if proc.name == name:
            return proc
    return None


def procedures_index(directory: Path | None = None) -> str:
    """The prompt section naming every procedure; empty when there are none."""
    procs = list_procedures(directory)
    if not procs:
        return ""
    lines = [
        "## Процедуры",
        "",
        "Знания и порядок работы по отдельным темам. Если задача по теме из "
        "списка, сначала прочитай процедуру: `procedures` `action=read`, `name`.",
        "",
    ]
    lines += [f"- `{proc.name}` — {proc.description}" for proc in procs]
    return "\n".join(lines)
