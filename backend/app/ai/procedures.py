"""Procedures: domain know-how the agent reads on demand.

Each ``aiagent/procedures/<name>.md`` opens with a front matter holding its
``name`` and a one-line ``description``. Only the index (name — description)
sits in the system prompt; the text arrives as a tool result when the model
asks for it, so the head of every request stays the same and the prompt cache
keeps it. Procedures are protected data like the prompt: built-in ones change
only through a reviewed edit of the file; learned ones (``agent_procedures``)
are proposed by the agent or the owner and reach the index only once the owner
approves them (hermes-agent's self-written skills, with the owner as gate).
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
    learned: bool = False
    stale: bool = False


# Learned procedures live in the database; every process keeps the live ones
# here, reloaded at the start of a turn (refresh_learned), so building the
# prompt stays synchronous and the index changes only when the set does.
_learned: dict[str, Procedure] = {}

INDEX_LIMIT = 40
STALE_AFTER_DAYS = 14
ARCHIVE_AFTER_DAYS = 30
LIVE = ("active", "stale")


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


def builtin_procedures(directory: Path | None = None) -> list[Procedure]:
    root = directory or PROCEDURES_DIR
    if not root.is_dir():
        return []
    found = (_parse(path) for path in sorted(root.glob("*.md")))
    return [proc for proc in found if proc is not None]


def list_procedures(directory: Path | None = None) -> list[Procedure]:
    """Built-in procedures, with an approved learned version taking the place
    of a built-in one of the same name, then the learned ones (most used first
    as they were loaded)."""
    merged = {proc.name: proc for proc in builtin_procedures(directory)}
    learned = dict(_learned) if directory is None else {}
    for name, proc in learned.items():
        merged[name] = proc
    return list(merged.values())


def read_procedure(name: str, directory: Path | None = None) -> Procedure | None:
    if not _NAME_RE.match(name or ""):
        return None
    for proc in list_procedures(directory):
        if proc.name == name:
            return proc
    return None


def procedures_index(directory: Path | None = None) -> str:
    """The prompt section naming every procedure; empty when there are none."""
    procs = list_procedures(directory)[:INDEX_LIMIT]
    if not procs:
        return ""
    lines = [
        "## Процедуры",
        "",
        "Знания и порядок работы по отдельным темам. Если задача по теме из "
        "списка, сначала прочитай процедуру: `procedures` `action=read`, `name`.",
        "",
    ]
    lines += [
        f"- `{proc.name}` — {proc.description}" + (" (давно не читалась)" if proc.stale else "")
        for proc in procs
    ]
    lines += [
        "",
        "Тебя научили порядку работы («запомни, как…», «у нас всегда…») или ты его "
        "выяснила за многошаговую работу, а процедуры о нём нет — предложи её: "
        "`procedures` `action=propose` (`name`, `description`, `text`). В силу она "
        "вступит после решения владельца; в ответе скажи, что предложила.",
    ]
    return "\n".join(lines)


# ── learned procedures (agent_procedures) ─────────────────────────────────


def _as_procedure(row) -> Procedure:
    return Procedure(
        name=row.name,
        description=" ".join(row.description.split()),
        text=row.text.strip(),
        learned=True,
        stale=row.status == "stale",
    )


async def refresh_learned(db=None) -> bool:
    """Reload the live learned procedures; True when the set the prompt shows changed."""
    from sqlalchemy import select

    from app.db.models import AgentProcedure

    async def _load(session) -> dict[str, Procedure]:
        rows = (
            await session.scalars(
                select(AgentProcedure)
                .where(AgentProcedure.status.in_(LIVE))
                .order_by(AgentProcedure.uses.desc(), AgentProcedure.name)
            )
        ).all()
        return {row.name: _as_procedure(row) for row in rows}

    try:
        if db is not None:
            loaded = await _load(db)
        else:
            from app.db.session import _get_session_factory

            async with _get_session_factory()() as session:
                loaded = await _load(session)
    except Exception as exc:  # noqa: BLE001 — the turn goes on with what it had
        log_degraded("procedures.refresh_learned", exc)
        return False
    global _learned
    changed = [(p.name, p.description, p.stale) for p in loaded.values()] != [
        (p.name, p.description, p.stale) for p in _learned.values()
    ]
    _learned = loaded
    return changed


async def record_use(db, name: str) -> None:
    """A read keeps a learned procedure from going stale."""
    from datetime import UTC, datetime

    from sqlalchemy import update

    from app.db.models import AgentProcedure

    await db.execute(
        update(AgentProcedure)
        .where(AgentProcedure.name == name, AgentProcedure.status.in_(LIVE))
        .values(
            uses=AgentProcedure.uses + 1,
            last_used_at=datetime.now(UTC),
            # Read again: back in the index without the stale mark.
            status="active",
        )
    )


class ProcedureError(ValueError):
    pass


def _clean(name: str, description: str, text: str) -> tuple[str, str, str]:
    name = (name or "").strip().lower()
    description = " ".join((description or "").split())
    text = (text or "").strip()
    if not _NAME_RE.match(name) or len(name) > 80:
        raise ProcedureError("name: латиница в нижнем регистре, цифры и дефис, до 80 знаков")
    if not description or len(description) > 150:
        raise ProcedureError("description: одна строка до 150 знаков")
    if not text or len(text) > 20000:
        raise ProcedureError("text: от 1 до 20000 знаков")
    return name, description, text


async def propose(
    db,
    *,
    name: str,
    description: str,
    text: str,
    actor: str,
    origin: str = "agent",
    reason: str | None = None,
    work_order_id=None,
):
    """A new procedure or a new version of one, waiting for the owner."""
    from sqlalchemy import select

    from app.db.models import AgentProcedure

    name, description, text = _clean(name, description, text)
    current = read_procedure(name)
    if current is not None and current.text == text and current.description == description:
        raise ProcedureError("Такая процедура уже действует — менять нечего")
    pending = await db.scalar(
        select(AgentProcedure).where(
            AgentProcedure.name == name, AgentProcedure.status == "proposed"
        )
    )
    if pending is not None:
        # One open proposal per name: the newer text replaces the older one.
        pending.description, pending.text, pending.reason = description, text, reason
        pending.proposed_by = actor
        return pending
    row = AgentProcedure(
        name=name,
        description=description,
        text=text,
        status="proposed",
        origin=origin,
        reason=reason,
        source_work_order_id=work_order_id,
        proposed_by=actor,
    )
    db.add(row)
    await db.flush()
    return row


async def decide(db, row, *, approve: bool, actor: str, text=None, description=None):
    """The owner's decision; an approved version supersedes the live one."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.db.models import AgentProcedure

    if row.status != "proposed":
        raise ProcedureError("Решение принимается только по предложенной процедуре")
    now = datetime.now(UTC)
    row.decided_by, row.decided_at = actor, now
    if not approve:
        row.status = "rejected"
        return row
    if text is not None or description is not None:
        _, row.description, row.text = _clean(
            row.name,
            description if description is not None else row.description,
            text if text is not None else row.text,
        )
    live = (
        await db.scalars(
            select(AgentProcedure).where(
                AgentProcedure.name == row.name, AgentProcedure.status.in_(LIVE)
            )
        )
    ).all()
    for old in live:
        old.status = "superseded"
        row.pinned = row.pinned or old.pinned
    await db.flush()
    row.status = "active"
    row.last_used_at = now
    return row


async def restore(db, row, *, actor: str):
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.db.models import AgentProcedure

    if row.status != "archived":
        raise ProcedureError("Восстановить можно только процедуру из архива")
    taken = await db.scalar(
        select(AgentProcedure.id).where(
            AgentProcedure.name == row.name, AgentProcedure.status.in_(LIVE)
        )
    )
    if taken is not None:
        raise ProcedureError("Процедура с этим именем уже действует")
    row.status = "active"
    row.last_used_at = datetime.now(UTC)
    row.decided_by = actor
    return row


async def curate(db, now=None) -> dict[str, int]:
    """Unused 14 days → stale (still listed, marked), 30 days → archived.

    Pinned procedures and the built-in files are never touched; a read brings
    a stale one back (record_use). From hermes-agent's curator.
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func, or_, update

    from app.db.models import AgentProcedure

    now = now or datetime.now(UTC)
    last = func.coalesce(AgentProcedure.last_used_at, AgentProcedure.decided_at)
    archived = await db.execute(
        update(AgentProcedure)
        .where(
            AgentProcedure.status.in_(LIVE),
            AgentProcedure.pinned.is_(False),
            last < now - timedelta(days=ARCHIVE_AFTER_DAYS),
        )
        .values(status="archived")
    )
    stale = await db.execute(
        update(AgentProcedure)
        .where(
            AgentProcedure.status == "active",
            AgentProcedure.pinned.is_(False),
            or_(last < now - timedelta(days=STALE_AFTER_DAYS)),
        )
        .values(status="stale")
    )
    return {"stale": stale.rowcount or 0, "archived": archived.rowcount or 0}
