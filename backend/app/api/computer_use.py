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

from app.auth.jwt import get_current_user
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
    return parsed.scheme in {"http", "https"} and any(
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
    user: UserInfo = Depends(get_current_user),
) -> dict:
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
