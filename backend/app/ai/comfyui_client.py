"""Async client for an external ComfyUI server (image generation / editing).

The drawings studio offloads heavy raster work (Qwen-Image-Edit, text→image,
inpainting, cleanup) to a ComfyUI node. This module is the low-level driver; the
Celery task ``tasks.image_generation`` orchestrates a full job on top of it.

Node resolution reuses the provider registry (DB → YAML → env) so the same node
configured in ``/settings/models`` / ``/settings/comfyui`` is used everywhere.
Because manufacturing drawings are confidential, the node MUST be local/on-prem;
``resolve_node`` refuses a non-local node (mirrors the AIRouter cloud guard).

Standard ComfyUI HTTP flow:
    POST /upload/image  → stored input filename
    POST /prompt        → prompt_id (queued)
    GET  /history/{id}  → outputs once finished (poll)
    GET  /view          → output image bytes
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.ai import provider_registry
from app.ai.schemas import ProviderKind

logger = structlog.get_logger()


class ComfyUIError(RuntimeError):
    """Raised for connection / workflow / confidentiality failures."""


class ComfyUITransientError(ComfyUIError):
    """Node unreachable / timed out right now — worth retrying (Celery autoretry).

    Distinct from a plain ``ComfyUIError`` (e.g. missing model, rejected graph),
    which is a final failure that retrying would not fix.
    """


@dataclass
class ComfyOutput:
    filename: str
    subfolder: str
    type: str  # "output" | "temp" | "input"


def resolve_node(preferred_instance: str | None = None) -> provider_registry.ResolvedProvider:
    """Resolve the ComfyUI node to call, enforcing on-prem-only.

    Confidential drawings must never leave the local network, so a node that is
    not flagged ``is_local`` is rejected.
    """
    node = provider_registry.select_instance(
        ProviderKind.COMFYUI, preferred_instance=preferred_instance
    )
    if not node.base_url:
        raise ComfyUIError("ComfyUI узел не настроен (нет base_url).")
    if not node.is_local:
        raise ComfyUIError(
            "ComfyUI узел не помечен как локальный (is_local). "
            "Генерация чертежей разрешена только on-prem."
        )
    return node


def build_workflow(
    graph_template: dict[str, Any],
    inject_map: dict[str, dict[str, str]],
    values: dict[str, Any],
) -> dict[str, Any]:
    """Produce a ready-to-queue API graph by injecting values into the template.

    ``inject_map`` maps a logical key (``prompt``/``negative``/``image``/``mask``/
    ``seed``/``width``/``height``/...) to ``{"node": <id>, "input": <name>}``.
    Only keys present (and non-None) in ``values`` are injected, so a template
    keeps its defaults for everything the caller doesn't override.
    """
    graph = copy.deepcopy(graph_template)
    for key, target in (inject_map or {}).items():
        if key not in values or values[key] is None:
            continue
        node_id = str(target.get("node", ""))
        input_name = target.get("input", "")
        node = graph.get(node_id)
        if not node_id or not input_name or not isinstance(node, dict):
            logger.warning("comfyui_inject_skip", key=key, node=node_id, input=input_name)
            continue
        node.setdefault("inputs", {})[input_name] = values[key]
    return graph


class ComfyUIClient:
    """Thin async wrapper over the ComfyUI HTTP API for one node."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.client_id = uuid.uuid4().hex

    @classmethod
    def from_registry(cls, preferred_instance: str | None = None) -> ComfyUIClient:
        node = resolve_node(preferred_instance)
        return cls(base_url=node.base_url, api_key=node.api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    f"{self.base_url}/system_stats", headers=self._headers()
                )
                resp.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("comfyui_health_failed", base_url=self.base_url, error=str(exc))
            return False

    async def object_info(self) -> dict[str, Any]:
        """Available nodes/models on the server (used by the workflow editor)."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{self.base_url}/object_info", headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def upload_image(
        self,
        content: bytes,
        name: str,
        image_type: str = "input",
        overwrite: bool = True,
    ) -> str:
        """Upload an input image; returns the server-side filename to reference."""
        files = {"image": (name, content, "application/octet-stream")}
        data = {"type": image_type, "overwrite": "true" if overwrite else "false"}
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{self.base_url}/upload/image",
                    files=files,
                    data=data,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                body = resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise ComfyUITransientError(f"ComfyUI недоступен при загрузке файла: {exc}") from exc
        # ComfyUI returns {"name": "...", "subfolder": "...", "type": "input"}
        sub = body.get("subfolder") or ""
        fname = body.get("name", name)
        return f"{sub}/{fname}" if sub else fname

    async def queue_workflow(self, graph: dict[str, Any]) -> str:
        """Queue an API-format graph; returns the prompt_id."""
        payload = {"prompt": graph, "client_id": self.client_id}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.base_url}/prompt", json=payload, headers=self._headers()
                )
                if resp.status_code >= 400:
                    raise ComfyUIError(
                        f"ComfyUI отклонил воркфлоу ({resp.status_code}): {resp.text[:500]}"
                    )
                body = resp.json()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise ComfyUITransientError(f"ComfyUI недоступен при постановке в очередь: {exc}") from exc
        prompt_id = body.get("prompt_id")
        if not prompt_id:
            raise ComfyUIError(f"ComfyUI не вернул prompt_id: {body}")
        return prompt_id

    async def wait_for_result(
        self,
        prompt_id: str,
        poll_interval: float = 1.5,
        timeout: float | None = None,
    ) -> list[ComfyOutput]:
        """Poll /history until the prompt finishes; returns produced images."""
        import asyncio

        deadline = (timeout if timeout is not None else self.timeout)
        elapsed = 0.0
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                while elapsed < deadline:
                    resp = await client.get(
                        f"{self.base_url}/history/{prompt_id}", headers=self._headers()
                    )
                    resp.raise_for_status()
                    hist = resp.json().get(prompt_id)
                    if hist:
                        status = (hist.get("status") or {})
                        if status.get("status_str") == "error":
                            raise ComfyUIError(f"ComfyUI workflow error: {status}")
                        outputs = self._extract_outputs(hist.get("outputs") or {})
                        if outputs:
                            return outputs
                        # Finished but produced no images.
                        if status.get("completed"):
                            raise ComfyUIError("ComfyUI завершил воркфлоу без изображений.")
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise ComfyUITransientError(f"ComfyUI недоступен во время ожидания результата: {exc}") from exc
        raise ComfyUIError(f"ComfyUI: превышено время ожидания ({deadline:.0f}s).")

    async def stream_progress(self, prompt_id: str, on_progress) -> None:
        """Best-effort live progress via ComfyUI's WebSocket. Calls
        ``on_progress({"value", "max", "node"})`` for sampling steps of OUR
        prompt and returns when it finishes/errors. Any failure is swallowed
        — progress is a nicety; ``wait_for_result`` (HTTP poll) remains the
        source of truth for completion. Binary preview frames are ignored."""
        import json as _json

        ws_url = (self.base_url.replace("https://", "wss://", 1)
                  .replace("http://", "ws://", 1)) + f"/ws?clientId={self.client_id}"
        headers = self._headers()
        try:
            import websockets

            async with websockets.connect(
                ws_url, additional_headers=headers or None, max_size=None,
                open_timeout=10, ping_interval=None,
            ) as ws:
                async for raw in ws:
                    if isinstance(raw, (bytes, bytearray)):
                        continue  # preview image frame
                    try:
                        msg = _json.loads(raw)
                    except Exception:  # noqa: BLE001
                        continue
                    mtype = msg.get("type")
                    data = msg.get("data") or {}
                    if data.get("prompt_id") and data.get("prompt_id") != prompt_id:
                        continue
                    if mtype == "progress":
                        on_progress({"value": data.get("value"),
                                     "max": data.get("max"),
                                     "node": data.get("node")})
                    elif mtype == "executing" and data.get("node") is None:
                        return  # our prompt finished
                    elif mtype in ("execution_success", "execution_error",
                                   "execution_interrupted"):
                        return
        except Exception as exc:  # noqa: BLE001
            logger.info("comfyui_ws_progress_unavailable", error=str(exc)[:120])

    @staticmethod
    def _extract_outputs(outputs: dict[str, Any]) -> list[ComfyOutput]:
        result: list[ComfyOutput] = []
        for node_out in outputs.values():
            for img in node_out.get("images", []) or []:
                if img.get("type") == "temp":
                    continue  # skip preview/temp frames
                result.append(
                    ComfyOutput(
                        filename=img.get("filename", ""),
                        subfolder=img.get("subfolder", "") or "",
                        type=img.get("type", "output"),
                    )
                )
        return result

    async def fetch_image(self, output: ComfyOutput) -> bytes:
        params = {
            "filename": output.filename,
            "subfolder": output.subfolder,
            "type": output.type,
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                f"{self.base_url}/view", params=params, headers=self._headers()
            )
            resp.raise_for_status()
            return resp.content
