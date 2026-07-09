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


def _node_inputs(node: Any) -> dict[str, Any]:
    if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
        return node["inputs"]
    return {}


def _generic_text_input_names() -> tuple[str, ...]:
    return (
        "prompt",
        "text",
        "string",
        "text_l",
        "text_g",
        "clip_l",
        "clip_g",
        "t5xxl",
        "text_positive",
        "text_negative",
        "positive_text",
        "negative_text",
    )


def _looks_like_text_class(class_type: str) -> bool:
    cls = class_type.lower()
    return (
        "textencode" in cls
        or "text_encode" in cls
        or "cliptext" in cls
        or "prompt" in cls
        or cls.startswith("text")
        or cls.endswith("text")
    )


def _looks_like_text_node(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    cls = str(node.get("class_type") or "").lower()
    inputs = _node_inputs(node)
    return (
        _looks_like_text_class(cls)
        or any(k in inputs for k in _generic_text_input_names())
    )


def _looks_like_short_negative_prompt(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > 260:
        return False
    negative_tokens = (
        "worst",
        "bad",
        "blur",
        "blurry",
        "artifact",
        "artifacts",
        "low quality",
        "deformed",
        "noise",
        "noisy",
        "shadow",
        "shadows",
        "лишн",
        "размыт",
        "артефакт",
        "шум",
        "тень",
    )
    return any(token in stripped for token in negative_tokens)


def _looks_negative_text_node(node: Any) -> bool:
    inputs = _node_inputs(node)
    text = " ".join(
        str(inputs.get(name) or "")
        for name in _generic_text_input_names()
        if isinstance(inputs.get(name), str)
    ).lower()
    cls = str(node.get("class_type") or "").lower() if isinstance(node, dict) else ""
    haystack = f"{cls} {text}"
    if any(token in haystack for token in ("negative", "negative_prompt", "neg prompt", "негатив")):
        return True
    if any(token in cls for token in ("negative", "negprompt")):
        return True
    return _looks_like_short_negative_prompt(text)


def _fallback_text_input(node: Any) -> str:
    inputs = _node_inputs(node)
    for name in _preferred_text_inputs(node):
        if name in inputs:
            return name
    return _preferred_text_inputs(node)[0]


def _preferred_text_inputs(node: Any) -> tuple[str, ...]:
    """Return likely real text input names for this ComfyUI text node.

    Old imported visual workflows in this product sometimes stored Qwen Image
    Edit text nodes with ``inputs.text`` because the visual widget parser did
    not know the node's real API input is ``prompt``. At runtime we normalize
    both old and new workflows by writing to the actual input for known node
    families and cleaning stale aliases that would otherwise leave the template
    prompt active.
    """
    cls = str(node.get("class_type") or "").lower() if isinstance(node, dict) else ""
    if "qwen" in cls and "text" in cls:
        return ("prompt", "text", "string")
    if "cliptextencode" in cls:
        return ("text", "prompt", "string")
    inputs = _node_inputs(node)
    existing = tuple(
        name
        for name in _generic_text_input_names()
        if name in inputs and not isinstance(inputs.get(name), (list, dict))
    )
    if existing:
        return existing + tuple(name for name in _generic_text_input_names() if name not in existing)
    return _generic_text_input_names()


def _set_text_value(node: Any, value: Any) -> str:
    inputs = _node_inputs(node)
    input_name = _preferred_text_inputs(node)[0]
    inputs[input_name] = value
    # Drop stale aliases created by earlier import heuristics for known text
    # node families. This keeps the API graph from carrying both an old
    # template prompt and the user's replacement under different names.
    cls = str(node.get("class_type") or "").lower() if isinstance(node, dict) else ""
    if "qwen" in cls and "text" in cls and input_name == "prompt":
        inputs.pop("text", None)
        inputs.pop("string", None)
    elif "cliptextencode" in cls and input_name == "text":
        inputs.pop("prompt", None)
        inputs.pop("string", None)
    return input_name


def _fallback_inject_text(
    graph: dict[str, Any],
    key: str,
    value: Any,
    negative_node_ids: set[str],
    skip_ids: set[str] | None = None,
) -> None:
    """Inject prompt/negative into likely text nodes when a custom map is incomplete."""
    if value is None:
        return
    skip_ids = skip_ids or set()
    text_nodes = [
        (node_id, node) for node_id, node in graph.items()
        if _looks_like_text_node(node) and str(node_id) not in skip_ids
    ]
    if key == "negative":
        candidates = [item for item in text_nodes if _looks_negative_text_node(item[1])]
        if not candidates and len(text_nodes) > 1:
            candidates = [text_nodes[1]]
    else:
        candidates = [
            item for item in text_nodes
            if str(item[0]) not in negative_node_ids and not _looks_negative_text_node(item[1])
        ]
        if not candidates and text_nodes:
            candidates = [text_nodes[0]]
    if not candidates:
        return
    node_id, node = candidates[0]
    input_name = _set_text_value(node, value)
    logger.info("comfyui_prompt_fallback_injected", key=key, node=str(node_id), input=input_name)


def _targets_for_key(
    inject_map: dict[str, Any],
    key: str,
) -> list[dict[str, Any]]:
    target = (inject_map or {}).get(key)
    if not target:
        return []
    targets = target if isinstance(target, list) else [target]
    return [t for t in targets if isinstance(t, dict)]


def _is_link_value(value: Any) -> bool:
    return isinstance(value, list) and len(value) >= 1 and not isinstance(value[0], (list, dict))


def _downstream_role_score(graph: dict[str, Any], source_id: str, role: str) -> int:
    queue: list[tuple[str, int]] = [(source_id, 0)]
    visited: set[str] = set()
    score = 0
    while queue:
        node_id, depth = queue.pop(0)
        if node_id in visited or depth > 5:
            continue
        visited.add(node_id)
        for target_id, node in graph.items():
            for input_name, value in _node_inputs(node).items():
                if not _is_link_value(value) or str(value[0]) != node_id:
                    continue
                lowered = str(input_name).lower()
                if lowered == role or role in lowered:
                    score += max(20, 120 - depth * 20)
                if str(target_id) not in visited:
                    queue.append((str(target_id), depth + 1))
    return score


def _negative_text_node_ids(
    graph: dict[str, Any],
    inject_map: dict[str, Any],
) -> set[str]:
    out: set[str] = set()
    text_nodes = [(node_id, node) for node_id, node in graph.items() if _looks_like_text_node(node)]
    text_node_ids = {str(node_id) for node_id, _node in text_nodes}
    # An explicit map entry must be honored even for a single-text-node graph
    # (e.g. one CLIPTextEncode with a distinct negative-conditioning sibling
    # node that isn't itself detected as a "text" node) — only the graph-wide
    # scoring heuristic below requires >=2 text nodes to compare against.
    for target in _targets_for_key(inject_map, "negative"):
        node_id = str(target.get("node", ""))
        if not node_id or node_id not in text_node_ids:
            continue
        if len(text_nodes) > 1:
            positive_score = _downstream_role_score(graph, node_id, "positive")
            negative_score = _downstream_role_score(graph, node_id, "negative")
            if positive_score > negative_score:
                logger.warning("comfyui_negative_map_ignored_positive_node", node=node_id)
                continue
        out.add(node_id)
    if len(text_nodes) <= 1:
        return out
    for node_id, node in text_nodes:
        node_id_str = str(node_id)
        positive_score = _downstream_role_score(graph, node_id_str, "positive")
        negative_score = _downstream_role_score(graph, node_id_str, "negative")
        if negative_score > positive_score:
            out.add(node_id_str)
        elif positive_score == negative_score and _looks_negative_text_node(node):
            out.add(node_id_str)
    # Common ComfyUI shape: positive text encode followed by negative text encode.
    # Imported workflows often lose that mapping; keep the second text node from
    # being overwritten by the user's positive prompt.
    if not out and len(text_nodes) > 1:
        out.add(str(text_nodes[1][0]))
    return out


def _replace_text_nodes(
    graph: dict[str, Any],
    key: str,
    value: Any,
    negative_node_ids: set[str],
    skip_ids: set[str] | None = None,
) -> int:
    if value is None:
        return 0
    skip_ids = skip_ids or set()
    changed = 0
    for node_id, node in graph.items():
        if str(node_id) in skip_ids:
            continue
        if not _looks_like_text_node(node):
            continue
        is_negative = str(node_id) in negative_node_ids
        if key == "prompt" and is_negative:
            continue
        if key == "negative" and not is_negative:
            continue
        _set_text_value(node, value)
        changed += 1
    if changed:
        logger.info("comfyui_text_nodes_replaced", key=key, count=changed)
    return changed


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
    ``seed``/``width``/``height``/...) to ``{"node": <id>, "input": <name>}``, or
    to a LIST of such targets when one value must land in several nodes (e.g.
    FLUX.2 t2i feeds width/height to BOTH EmptyFlux2LatentImage and
    Flux2Scheduler). Only keys present (and non-None) in ``values`` are injected,
    so a template keeps its defaults for everything the caller doesn't override.
    """
    graph = copy.deepcopy(graph_template)
    explicit_prompt_nodes: set[str] = set()
    explicit_negative_nodes: set[str] = set()
    for key, target in (inject_map or {}).items():
        if key not in values or values[key] is None:
            continue
        targets = target if isinstance(target, list) else [target]
        for tgt in targets:
            node_id = str(tgt.get("node", ""))
            input_name = tgt.get("input", "")
            node = graph.get(node_id)
            if not node_id or not input_name or not isinstance(node, dict):
                logger.warning("comfyui_inject_skip", key=key, node=node_id, input=input_name)
                continue
            if key in {"prompt", "negative"}:
                if not _looks_like_text_node(node):
                    # Old imported workflows sometimes point prompt/negative at
                    # sampler conditioning inputs or at the wrong node; that
                    # mapping can't be honored directly, so fall through to the
                    # graph-aware pass below instead.
                    logger.warning(
                        "comfyui_text_inject_skip_non_text_node",
                        key=key,
                        node=node_id,
                        input=input_name,
                        class_type=node.get("class_type"),
                    )
                    continue
                # A map entry that correctly names a real text node is an
                # explicit, human-configured choice — honor it by writing to
                # this node (via _set_text_value, which still normalizes
                # known node families and drops stale aliases) instead of
                # silently discarding it for the graph-wide heuristic below.
                _set_text_value(node, values[key])
                if key == "prompt":
                    explicit_prompt_nodes.add(node_id)
                else:
                    explicit_negative_nodes.add(node_id)
                continue
            node.setdefault("inputs", {})[input_name] = values[key]
    # An explicitly mapped prompt node always wins over the heuristic's
    # ambiguous tie-break (e.g. "assume the 2nd text node is negative") —
    # otherwise a correctly-configured prompt target could be reclassified
    # as the negative node and get skipped by the replace pass below.
    negative_node_ids = (_negative_text_node_ids(graph, inject_map) | explicit_negative_nodes) - explicit_prompt_nodes
    prompt_changed = _replace_text_nodes(
        graph, "prompt", values.get("prompt"), negative_node_ids,
        skip_ids=explicit_prompt_nodes | explicit_negative_nodes,
    )
    if prompt_changed == 0 and not explicit_prompt_nodes:
        _fallback_inject_text(
            graph, "prompt", values.get("prompt"), negative_node_ids,
            skip_ids=explicit_negative_nodes,
        )
    if values.get("negative") is not None:
        negative_changed = _replace_text_nodes(
            graph, "negative", values.get("negative"), negative_node_ids,
            skip_ids=explicit_negative_nodes,
        )
        if negative_changed == 0 and not explicit_negative_nodes:
            _fallback_inject_text(
                graph, "negative", values.get("negative"), negative_node_ids,
                skip_ids=explicit_prompt_nodes,
            )
            if not negative_node_ids:
                text_node_count = sum(1 for n in graph.values() if _looks_like_text_node(n))
                logger.warning(
                    "comfyui_negative_prompt_no_target",
                    reason="single_text_node" if text_node_count <= 1 else "no_negative_node_detected",
                )
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

    async def interrupt(self) -> bool:
        """Best-effort stop for the currently executing ComfyUI prompt."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(f"{self.base_url}/interrupt", headers=self._headers())
                return resp.status_code < 400
        except Exception as exc:  # noqa: BLE001
            logger.warning("comfyui_interrupt_failed", base_url=self.base_url, error=str(exc))
            return False

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
