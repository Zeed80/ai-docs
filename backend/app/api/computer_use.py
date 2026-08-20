"""Least-privilege broker for browser, filesystem, shell, and desktop evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AliasChoices, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.degradation import log_degraded
from app.auth.acting import get_effective_user
from app.auth.models import UserInfo
from app.db.models import ComputerUseAction, ComputerUseGrant, WorkOrder
from app.db.session import get_db

router = APIRouter()


class ComputerActionIn(BaseModel):
    action: str = Field(pattern="^(browser_fetch|desktop_snapshot|desktop_start|desktop_click|desktop_type|desktop_read|desktop_close|file_read|file_write|shell)$")
    work_order_id: uuid.UUID
    step_id: uuid.UUID | None = None
    target: str = Field(min_length=1, max_length=4096)
    body: dict[str, Any] = Field(
        default_factory=dict, validation_alias=AliasChoices("body", "arguments")
    )


def _safe_path(target: str, roots: list[str]) -> Path:
    candidate = Path(target).resolve()
    allowed = [Path(root).resolve() for root in roots]
    if not any(candidate == root or root in candidate.parents for root in allowed):
        raise HTTPException(status_code=403, detail="Target is outside granted roots")
    return candidate


def _host_allowed(url: str, hosts: list[str]) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    # Ф2.A (AGENT_AUTONOMY_ROADMAP.md): "*" is an explicit wildcard sentinel,
    # not the empty-list default — an empty allowed_hosts still allows
    # nothing (unchanged). A grant needs "*" spelled out to opt in, which
    # only a manager can do (grant_computer_use requires that role) — the
    # same conscious human act as any other grant, just scoped wide instead
    # of to named hosts. Exists because exploratory discovery (Ф2/web_discover
    # below) can't enumerate which hosts it'll need before it has searched —
    # unlike browser_fetch's normal use (a known site), the host list isn't
    # knowable ahead of time.
    if any(h == "*" for h in hosts):
        return True
    return any(
        host == allowed.casefold() or host.endswith("." + allowed.casefold())
        for allowed in hosts
    )


async def _perform(action: str, target: str, body: dict, grant: ComputerUseGrant) -> tuple[dict, dict]:
    if action in {"browser_fetch", "desktop_snapshot"}:
        if not _host_allowed(target, list(grant.allowed_hosts or [])):
            raise HTTPException(status_code=403, detail="Host is outside granted allowlist")
        from app.api.web_search import WebFetchRequest, fetch_page

        response = await fetch_page(
            WebFetchRequest(
                url=target,
                screenshot=action == "desktop_snapshot" or bool(body.get("screenshot")),
                max_chars=min(int(body.get("max_chars", 20000)), 50000),
                wait_ms=min(int(body.get("wait_ms", 0)), 15000),
                ocr=bool(body.get("ocr", False)),
            )
        )
        result = response.model_dump()
        screenshot = result.pop("screenshot_b64", None)
        evidence = {
            "final_url": response.final_url,
            "status": response.status,
            "text_sha256": hashlib.sha256(response.text.encode()).hexdigest(),
            "screenshot_sha256": hashlib.sha256(screenshot.encode()).hexdigest() if screenshot else None,
        }
        if screenshot:
            result["screenshot_b64"] = screenshot
        return result, evidence
    if action.startswith("desktop_"):
        from app.ai.web_search_config import get_config

        if action == "desktop_start" and not _host_allowed(target, list(grant.allowed_hosts or [])):
            raise HTTPException(status_code=403, detail="Host is outside granted allowlist")
        endpoint = "/desktop/start" if action == "desktop_start" else "/desktop/action"
        payload = (
            {"url": target, "allowed_hosts": list(grant.allowed_hosts or [])}
            if action == "desktop_start"
            else {
                "session_id": target,
                "action": action.removeprefix("desktop_"),
                "selector": body.get("selector"),
                "text": body.get("text"),
                "wait_ms": min(int(body.get("wait_ms", 0)), 15000),
            }
        )
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(get_config().browser_url.rstrip("/") + endpoint, json=payload)
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("error") or "Desktop action failed")
        screenshot = result.get("screenshot_b64")
        evidence = {
            "url": result.get("url"),
            "screenshot_sha256": hashlib.sha256(screenshot.encode()).hexdigest() if screenshot else None,
        }
        return result, evidence
    if action == "file_read":
        path = _safe_path(target, list(grant.allowed_roots or []))
        data = await asyncio.to_thread(path.read_bytes)
        limit = min(int(body.get("max_bytes", 1_000_000)), 5_000_000)
        if len(data) > limit:
            raise HTTPException(status_code=413, detail="File exceeds granted read limit")
        digest = hashlib.sha256(data).hexdigest()
        return {"content": data.decode("utf-8", errors="replace"), "size": len(data)}, {"sha256": digest}
    if action == "file_write":
        path = _safe_path(target, list(grant.allowed_roots or []))
        content = str(body.get("content") or "").encode()
        if len(content) > 5_000_000:
            raise HTTPException(status_code=413, detail="File exceeds broker write limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, content)
        digest = hashlib.sha256(content).hexdigest()
        return {"path": str(path), "size": len(content), "sha256": digest}, {"sha256": digest}
    argv = shlex.split(target)
    if not argv or argv[0] not in set(grant.allowed_commands or []):
        raise HTTPException(status_code=403, detail="Command is outside granted allowlist")
    cwd = _safe_path(str(body.get("cwd") or (grant.allowed_roots or [""])[0]), list(grant.allowed_roots or []))
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env={"PATH": os.environ.get("PATH", "")},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=min(int(body.get("timeout", 60)), 120))
    except TimeoutError:
        process.kill()
        await process.wait()
        raise HTTPException(status_code=408, detail="Brokered command timed out") from None
    result = {"exit_code": process.returncode, "stdout": stdout.decode(errors="replace")[:200000], "stderr": stderr.decode(errors="replace")[:200000]}
    return result, {"output_sha256": hashlib.sha256(stdout + stderr).hexdigest()}


@router.post("/execute")
async def execute_computer_action(
    body: ComputerActionIn,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_effective_user),
) -> dict:
    """Ф2.A (AGENT_AUTONOMY_ROADMAP.md): found and fixed while wiring
    web_discover — this used to depend on get_current_user, which resolves
    the agent's own X-API-Key call to sub="agent-service" (see
    app.auth.jwt._verify_api_key), not the human the WorkOrder belongs to.
    Every check below compares against order.owner_key/grant.granted_to,
    which are the human's sub — so any computer_use call routed through the
    capability proxy (_execute_capability -> dispatch_capability -> here,
    the only path a WorkStep actually uses) 404'd on a WorkOrder it didn't
    author. get_effective_user (app.auth.acting) is the same de-escalation
    dependency already used elsewhere for exactly this agent-acts-for-human
    case: X-Acting-User relayed by _agent_headers()/_proxy resolves back to
    the human, an agent-service call with no acting header stays the bare
    service account (which owns nothing, so still correctly 404s).
    """
    now = datetime.now(UTC)
    order = await db.get(WorkOrder, body.work_order_id)
    if order is None or order.owner_key != user.sub:
        raise HTTPException(status_code=404, detail="Work order not found")
    grant = (
        await db.execute(
            select(ComputerUseGrant)
            .where(
                ComputerUseGrant.work_order_id == order.id,
                ComputerUseGrant.granted_to == user.sub,
                ComputerUseGrant.revoked_at.is_(None),
                ComputerUseGrant.expires_at > now,
                ComputerUseGrant.used_actions < ComputerUseGrant.max_actions,
            )
            .order_by(ComputerUseGrant.created_at.desc())
            .with_for_update()
        )
    ).scalar_one_or_none()
    if grant is None or body.action not in set(grant.actions or []):
        raise HTTPException(status_code=423, detail="A matching active computer-use grant is required")
    digest = hashlib.sha256(json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    audit = ComputerUseAction(
        work_order_id=order.id,
        grant_id=grant.id,
        step_id=body.step_id,
        action=body.action,
        target=body.target,
        arguments=body.body,
        action_digest=digest,
        status="running",
        started_at=now,
    )
    grant.used_actions += 1
    db.add(audit)
    await db.commit()
    try:
        result, evidence = await _perform(body.action, body.target, body.body, grant)
    except Exception as exc:
        await db.refresh(audit, with_for_update=True)
        audit.status = "failed"
        audit.error = {"type": type(exc).__name__, "message": str(exc)}
        audit.finished_at = datetime.now(UTC)
        await db.commit()
        raise
    await db.refresh(audit, with_for_update=True)
    audit.status = "succeeded"
    audit.result = result
    audit.evidence = evidence
    audit.finished_at = datetime.now(UTC)
    await db.commit()
    return {"action_id": str(audit.id), "result": result, "evidence": evidence}


# ── web_discover — Ф2.A (AGENT_AUTONOMY_ROADMAP.md) ─────────────────────────
#
# Exploratory discovery (search, then read what the search turns up) scoped
# by the same ComputerUseGrant as browser_fetch, rather than a parallel
# security model: search itself needs no host check (SearXNG is an internal
# trusted service returning candidate URLs, not the target of the request),
# but every URL it returns is checked against grant.allowed_hosts before
# being fetched, and each fetch consumes one unit of max_actions — same
# budget, same audit trail (one ComputerUseAction row per fetch) as calling
# browser_fetch that many times by hand would produce. Reuses
# app.api.web_search's execute_web_search/fetch_page (the same functions
# execute_web_research composes) rather than reimplementing search/fetch.


class WebDiscoverIn(BaseModel):
    work_order_id: uuid.UUID
    step_id: uuid.UUID | None = None
    queries: list[str] = Field(min_length=1, max_length=5)
    max_sources: int = Field(10, ge=1, le=30)
    recency_days: int | None = Field(default=None, ge=1, le=3650)


class WebDiscoverSource(BaseModel):
    url: str
    title: str | None = None
    snippet: str | None = None
    status: int | None = None
    text: str = ""
    is_pdf: bool = False
    truncated: bool = False
    diagnostics: list[str] = Field(default_factory=list)


class WebDiscoverSkipped(BaseModel):
    url: str
    reason: str  # host_not_allowed | budget_exhausted


class WebDiscoverOut(BaseModel):
    queries: list[str]
    fetched: list[WebDiscoverSource]
    skipped: list[WebDiscoverSkipped]
    search_diagnostics: list[str] = Field(default_factory=list)


async def _discover_one(
    db: AsyncSession,
    *,
    url: str,
    order: WorkOrder,
    grant: ComputerUseGrant,
    step_id: uuid.UUID | None,
    queries: list[str],
    snippet: str | None,
) -> WebDiscoverSource:
    """Fetch one URL already confirmed host-allowed and within budget, with
    the same audit-then-execute-then-settle shape as execute_computer_action
    — just looped, since one web_discover call can fetch many URLs."""
    from app.api.web_search import WebFetchRequest, fetch_page

    digest = hashlib.sha256(
        json.dumps({"action": "web_discover", "target": url, "queries": queries}, sort_keys=True).encode()
    ).hexdigest()
    audit = ComputerUseAction(
        work_order_id=order.id,
        grant_id=grant.id,
        step_id=step_id,
        action="web_discover",
        target=url,
        arguments={"queries": queries},
        action_digest=digest,
        status="running",
        started_at=datetime.now(UTC),
    )
    grant.used_actions += 1
    db.add(audit)
    await db.commit()
    try:
        page = await fetch_page(WebFetchRequest(url=url, screenshot=False, max_chars=20000))
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        await db.refresh(audit, with_for_update=True)
        audit.status = "failed"
        audit.error = {"type": "HTTPException", "message": str(detail)}
        audit.finished_at = datetime.now(UTC)
        await db.commit()
        # Ф5 (AGENT_AUTONOMY_ROADMAP.md): self-learning connector library —
        # only demotes an EXISTING connector for this domain (never creates
        # one from a failure), see record_connector_failure's own docstring.
        try:
            from app.ai.connectors import record_connector_failure
            await record_connector_failure(db, url=url)
        except Exception as learn_exc:
            log_degraded("computer_use.web_discover.connector_failure", learn_exc)
        reason = str(detail.get("error_code") or detail.get("message") or detail)
        return WebDiscoverSource(url=url, snippet=snippet, diagnostics=[reason])
    except Exception as exc:  # noqa: BLE001 - one bad URL must not fail the whole batch
        await db.refresh(audit, with_for_update=True)
        audit.status = "failed"
        audit.error = {"type": type(exc).__name__, "message": str(exc)[:500]}
        audit.finished_at = datetime.now(UTC)
        await db.commit()
        try:
            from app.ai.connectors import record_connector_failure
            await record_connector_failure(db, url=url)
        except Exception as learn_exc:
            log_degraded("computer_use.web_discover.connector_failure", learn_exc)
        return WebDiscoverSource(url=url, snippet=snippet, diagnostics=[f"read_error:{str(exc)[:120]}"])

    is_pdf = any("pdf" in d for d in page.diagnostics)
    await db.refresh(audit, with_for_update=True)
    audit.status = "succeeded"
    audit.result = {"title": page.title, "status": page.status, "truncated": page.truncated}
    audit.evidence = {"text_sha256": hashlib.sha256(page.text.encode()).hexdigest()}
    audit.finished_at = datetime.now(UTC)
    await db.commit()
    # Ф5 (AGENT_AUTONOMY_ROADMAP.md): a fetch that actually returned usable
    # content is the self-learning signal — draft/reinforce a connector for
    # this domain so a future exploratory cycle can try it directly instead
    # of discovering from scratch. Only real content counts (see
    # _MIN_USEFUL_TEXT_LENGTH) — a 200 with an empty/error body must not
    # teach the connector library the domain "works".
    from app.ai.connectors import _MIN_USEFUL_TEXT_LENGTH

    if page.status == 200 and len(page.text.strip()) >= _MIN_USEFUL_TEXT_LENGTH:
        try:
            from app.ai.connectors import record_connector_success
            await record_connector_success(db, url=url, queries=queries)
        except Exception as learn_exc:
            log_degraded("computer_use.web_discover.connector_success", learn_exc)
    return WebDiscoverSource(
        url=url,
        title=page.title,
        snippet=snippet,
        status=page.status,
        text=page.text,
        is_pdf=is_pdf,
        truncated=page.truncated,
        diagnostics=page.diagnostics,
    )


@router.post("/web-discover", response_model=WebDiscoverOut)
async def web_discover(
    body: WebDiscoverIn,
    db: AsyncSession = Depends(get_db),
    user: UserInfo = Depends(get_effective_user),
) -> WebDiscoverOut:
    """Never raises for an individual query/URL failure (matches
    execute_web_research's resilience contract — an exploratory step can't
    let one bad URL or one failed search angle kill the whole call); does
    raise 404/423 for order/grant problems, same as execute_computer_action.
    """
    from app.api.web_search import WebSearchRequest, execute_web_search

    now = datetime.now(UTC)
    order = await db.get(WorkOrder, body.work_order_id)
    if order is None or order.owner_key != user.sub:
        raise HTTPException(status_code=404, detail="Work order not found")
    grant = (
        await db.execute(
            select(ComputerUseGrant)
            .where(
                ComputerUseGrant.work_order_id == order.id,
                ComputerUseGrant.granted_to == user.sub,
                ComputerUseGrant.revoked_at.is_(None),
                ComputerUseGrant.expires_at > now,
                ComputerUseGrant.used_actions < ComputerUseGrant.max_actions,
            )
            .order_by(ComputerUseGrant.created_at.desc())
            .with_for_update()
        )
    ).scalar_one_or_none()
    if grant is None or "browser_fetch" not in set(grant.actions or []):
        raise HTTPException(status_code=423, detail="A matching active computer-use grant is required")

    search_diagnostics: list[str] = []
    candidate_urls: list[str] = []
    snippet_by_url: dict[str, str] = {}
    for q in body.queries:
        try:
            found = await execute_web_search(
                WebSearchRequest(
                    query=q, limit=body.max_sources, recency_days=body.recency_days, intent="research"
                )
            )
            for r in found.results:
                candidate_urls.append(r.url)
                if r.snippet and r.url not in snippet_by_url:
                    snippet_by_url[r.url] = r.snippet
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            search_diagnostics.append(f"search_error:{q[:60]}:{detail.get('message') or detail}")
        except Exception as exc:  # noqa: BLE001
            search_diagnostics.append(f"search_error:{q[:60]}:{str(exc)[:80]}")

    seen: set[str] = set()
    urls: list[str] = []
    for u in candidate_urls:
        key = u.split("#", 1)[0].rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        urls.append(u)
        if len(urls) >= body.max_sources:
            break

    fetched: list[WebDiscoverSource] = []
    skipped: list[WebDiscoverSkipped] = []
    for u in urls:
        if not _host_allowed(u, list(grant.allowed_hosts or [])):
            skipped.append(WebDiscoverSkipped(url=u, reason="host_not_allowed"))
            continue
        await db.refresh(grant, with_for_update=True)
        if grant.used_actions >= grant.max_actions:
            skipped.append(WebDiscoverSkipped(url=u, reason="budget_exhausted"))
            continue
        fetched.append(
            await _discover_one(
                db,
                url=u,
                order=order,
                grant=grant,
                step_id=body.step_id,
                queries=list(body.queries),
                snippet=snippet_by_url.get(u),
            )
        )

    return WebDiscoverOut(
        queries=list(body.queries), fetched=fetched, skipped=skipped, search_diagnostics=search_diagnostics
    )
