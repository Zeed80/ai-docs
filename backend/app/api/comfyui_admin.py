"""ComfyUI node admin — status, lifecycle (start/stop/restart/update) and
auto-discovery of a running ComfyUI on this host or the local network.

The ComfyUI server can be managed-local (an optional compose service on this
host) or external (just an address). Lifecycle actions only apply to the local
container; for an external node only status/discovery are meaningful.

Admin-only. Container ops reuse the docker.sock pattern from ``local_models_api``;
image pull/update uses the docker SDK like ``maintenance``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.jwt import require_role
from app.auth.models import UserRole

router = APIRouter()
logger = structlog.get_logger()

_admin = [Depends(require_role(UserRole.admin))]

_COMFY_SERVICE = os.environ.get("COMFYUI_SERVICE_NAME", "comfyui")
_DEFAULT_PORT = 8188
_DOCKER_SOCK = "/var/run/docker.sock"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _resolved():
    from app.ai.comfyui_client import resolve_node

    return resolve_node()


async def _probe(base_url: str, timeout: float = 2.0) -> dict | None:
    """Return ComfyUI system_stats if the URL hosts a live ComfyUI, else None."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/system_stats")
            resp.raise_for_status()
            data = resp.json()
        # system_stats has a 'system' key on a real ComfyUI server.
        if isinstance(data, dict) and ("system" in data or "devices" in data):
            return data
    except Exception:  # noqa: BLE001
        return None
    return None


def _docker_transport() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=_DOCKER_SOCK), base_url="http://docker"
    )


async def _find_container(name_hint: str) -> dict | None:
    """Find a container by compose-service label or name/image substring."""
    filters = json.dumps({"label": [f"com.docker.compose.service={name_hint}"]})
    try:
        async with _docker_transport() as client:
            r = await client.get(
                "/containers/json", params={"filters": filters, "all": "true"}
            )
            r.raise_for_status()
            containers = r.json()
            if containers:
                return containers[0]
            # Fallback: any container whose image/name mentions comfyui.
            r2 = await client.get("/containers/json", params={"all": "true"})
            r2.raise_for_status()
            for c in r2.json():
                names = " ".join(c.get("Names", [])) + " " + str(c.get("Image", ""))
                if "comfyui" in names.lower():
                    return c
    except Exception as exc:  # noqa: BLE001
        logger.warning("comfyui_find_container_failed", error=str(exc))
    return None


# ── Status ───────────────────────────────────────────────────────────────────


@router.get("/status", dependencies=_admin)
async def status() -> dict:
    try:
        node = _resolved()
    except Exception as exc:  # noqa: BLE001
        return {"configured": False, "error": str(exc)}

    stats = await _probe(node.base_url, timeout=4.0)
    container = await _find_container(_COMFY_SERVICE)
    queue = None
    if stats:
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                qr = await client.get(f"{node.base_url}/queue")
                if qr.status_code == 200:
                    q = qr.json()
                    queue = {
                        "running": len(q.get("queue_running", [])),
                        "pending": len(q.get("queue_pending", [])),
                    }
        except Exception:  # noqa: BLE001
            pass
    return {
        "configured": True,
        "base_url": node.base_url,
        "is_local": node.is_local,
        "online": stats is not None,
        "managed_local": container is not None,
        "container_state": (container or {}).get("State"),
        "system": (stats or {}).get("system"),
        "devices": (stats or {}).get("devices"),
        "queue": queue,
    }


@router.get("/object-info", dependencies=_admin)
async def object_info() -> dict:
    """Proxy ComfyUI /object_info (available nodes/models) for the editor."""
    from app.ai.comfyui_client import ComfyUIClient

    try:
        client = ComfyUIClient.from_registry()
        return await client.object_info()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"ComfyUI недоступен: {exc}")


# ── Lifecycle ────────────────────────────────────────────────────────────────


async def _container_action(action: str) -> dict:
    container = await _find_container(_COMFY_SERVICE)
    if not container:
        raise HTTPException(
            404,
            "Локальный контейнер ComfyUI не найден. Поднимите сервис: "
            "docker compose --profile comfyui up -d",
        )
    cid = container["Id"]
    async with _docker_transport() as client:
        resp = await client.post(f"/containers/{cid}/{action}", params={"t": "10"})
        if resp.status_code not in (204, 304):
            raise HTTPException(502, f"Docker {action} failed: {resp.text}")
    return {"ok": True, "action": action}


@router.post("/server/{action}", dependencies=_admin)
async def server_action(action: str) -> dict:
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "action должен быть start|stop|restart")
    return await _container_action(action)


@router.post("/install", dependencies=_admin)
async def install() -> dict:
    """Bring up the optional local ComfyUI container if it exists (stopped)."""
    container = await _find_container(_COMFY_SERVICE)
    if not container:
        return {
            "ok": False,
            "needs_compose": True,
            "message": (
                "Сервис ComfyUI ещё не создан. Запустите его командой на сервере: "
                "docker compose --profile comfyui up -d — затем нажмите «Найти ComfyUI»."
            ),
        }
    if container.get("State") == "running":
        return {"ok": True, "already_running": True}
    return await _container_action("start")


@router.post("/update", dependencies=_admin)
async def update() -> dict:
    """Pull the latest ComfyUI image and restart the local container."""
    container = await _find_container(_COMFY_SERVICE)
    if not container:
        raise HTTPException(404, "Локальный контейнер ComfyUI не найден.")
    image = container.get("Image", "")
    try:
        import docker  # lazy: optional dependency

        dclient = docker.from_env()
        if image:
            dclient.images.pull(image)
        cobj = dclient.containers.get(container["Id"])
        cobj.restart(timeout=10)
    except ImportError:
        raise HTTPException(503, "docker SDK не установлен в backend-образе.")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Обновление не удалось: {exc}")
    return {"ok": True, "image": image}


# ── Discovery ────────────────────────────────────────────────────────────────


class DiscoverRequest(BaseModel):
    cidr: str | None = Field(default=None, description="Подсеть для сканирования, напр. 192.168.1.0/24")
    ports: list[int] = Field(default_factory=lambda: [_DEFAULT_PORT])
    scan_network: bool = False


def _local_cidr() -> str | None:
    """Best-effort /24 of the backend's primary interface."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
        return str(net)
    except Exception:  # noqa: BLE001
        return None


@router.post("/discover", dependencies=_admin)
async def discover(body: DiscoverRequest) -> dict:
    """Scan for a live ComfyUI on this host / docker / the local network."""
    ports = body.ports or [_DEFAULT_PORT]
    candidates: list[str] = []

    # The currently-configured node first (covers a host-deployed ComfyUI reached
    # via the host-gateway alias / an external address).
    try:
        candidates.append(_resolved().base_url)
    except Exception:  # noqa: BLE001
        pass

    # Always-cheap local candidates. ``host-gateway`` is the docker→host alias
    # this stack maps in extra_hosts; ``host.docker.internal`` covers other setups.
    local_hosts = [
        "127.0.0.1", "localhost", _COMFY_SERVICE,
        "host-gateway", "host.docker.internal",
    ]
    for host in local_hosts:
        for port in ports:
            candidates.append(f"http://{host}:{port}")

    # Docker containers that look like ComfyUI.
    container = await _find_container(_COMFY_SERVICE)
    if container:
        for port in ports:
            candidates.append(f"http://{_COMFY_SERVICE}:{port}")

    # Optional LAN scan (bounded to a /24).
    if body.scan_network:
        cidr = body.cidr or _local_cidr()
        if cidr:
            try:
                net = ipaddress.ip_network(cidr, strict=False)
                if net.num_addresses > 256:
                    raise HTTPException(400, "Слишком большой диапазон — используйте /24 или уже.")
                for ip in net.hosts():
                    for port in ports:
                        candidates.append(f"http://{ip}:{port}")
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(400, f"Некорректный CIDR: {exc}")

    # De-dup while preserving order.
    seen: set[str] = set()
    uniq = [c for c in candidates if not (c in seen or seen.add(c))]

    sem = asyncio.Semaphore(32)
    found: list[dict] = []

    async def _check(url: str) -> None:
        async with sem:
            stats = await _probe(url, timeout=1.5)
        if stats:
            found.append(
                {
                    "base_url": url,
                    "system": stats.get("system"),
                    "devices": stats.get("devices"),
                }
            )

    await asyncio.gather(*(_check(u) for u in uniq), return_exceptions=True)
    return {"found": found, "scanned": len(uniq)}


# ── Model management (catalog + download via ComfyUI-Manager) ─────────────────

# Curated, RTX-3090-24GB-friendly models for the studio. Each references a file
# in the ComfyUI-Manager model DB (URLs resolved live, so they stay correct).
# Sized to run alongside the agent's LLM on one 24 GB card — fp8 weights stream
# under ComfyUI --lowvram; the small upscaler is negligible. Heavy bf16 variants
# are intentionally omitted from "recommended".
_STUDIO_RECOMMENDED: list[dict] = [
    {
        "filename": "qwen_image_fp8_e4m3fn.safetensors",
        "label": "Qwen-Image (база, fp8) — генерация по тексту",
        "operation": "generate",
        "vram": "≈12 ГБ (fp8, поток через lowvram)",
        "why": "Официальная база Qwen-Image для text→image, экономнее AIO.",
    },
    {
        "filename": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
        "label": "Qwen2.5-VL 7B (текст-энкодер, fp8)",
        "operation": "edit",
        "vram": "≈8 ГБ (fp8)",
        "why": "Текстовый энкодер для Qwen-Image / Qwen-Image-Edit.",
    },
    {
        "filename": "qwen_image_vae.safetensors",
        "label": "Qwen-Image VAE",
        "operation": "edit",
        "vram": "<1 ГБ",
        "why": "VAE для всех Qwen-Image воркфлоу.",
    },
    {
        "filename": "4x-UltraSharp.pth",
        "label": "4x-UltraSharp (апскейлер)",
        "operation": "cleanup",
        "vram": "<1 ГБ",
        "why": "Резкий апскейл сканов/фото; включает апскейл-воркфлоу.",
    },
]


async def _node_get(path: str, timeout: float = 12.0):
    node = _resolved()
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(f"{node.base_url}{path}")
        r.raise_for_status()
        return r.json()


async def _node_post(path: str, json_body=None, timeout: float = 20.0) -> int:
    node = _resolved()
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{node.base_url}{path}", json=json_body)
        return r.status_code


async def _manager_model_db() -> dict[str, dict]:
    """filename → ComfyUI-Manager model entry (url/save_path/type/…)."""
    try:
        data = await _node_get("/externalmodel/getlist?mode=cache")
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict] = {}
    for m in data.get("models", []):
        fn = m.get("filename")
        if fn:
            out[fn] = m
    return out


@router.get("/models", dependencies=_admin)
async def list_models() -> dict:
    """Installed models (by category) + curated recommended downloads."""
    from app.ai.comfyui_client import ComfyUIClient
    from app.ai.comfyui_models import available_models

    installed: dict[str, list[str]] = {}
    try:
        client = ComfyUIClient.from_registry()
        object_info = await client.object_info()
        installed = {k: sorted(v) for k, v in available_models(object_info).items()}
    except Exception as exc:  # noqa: BLE001
        return {"online": False, "error": str(exc), "installed": {}, "recommended": []}

    all_installed = {f for files in installed.values() for f in files}
    db = await _manager_model_db()
    recommended = []
    for rec in _STUDIO_RECOMMENDED:
        fn = rec["filename"]
        entry = db.get(fn)
        # ComfyUI caches /object_info per folder, so a just-downloaded model may
        # not appear there yet. ComfyUI-Manager checks the file on disk directly,
        # so its ``installed`` flag is the authoritative, immediate source.
        mgr_installed = str((entry or {}).get("installed", "")).lower() == "true"
        recommended.append(
            {
                **rec,
                "installed": mgr_installed or fn in all_installed,
                "available_to_download": entry is not None,
                "size": (entry or {}).get("size"),
                "type": (entry or {}).get("type"),
            }
        )
    return {
        "online": True,
        "manager": True,
        "installed": installed,
        "recommended": recommended,
    }


class ModelInstallRequest(BaseModel):
    filename: str | None = None
    entry: dict | None = None  # a full ComfyUI-Manager model entry (optional)


@router.post("/models/install", dependencies=_admin)
async def install_model(body: ModelInstallRequest) -> dict:
    """Download a model into ComfyUI via the ComfyUI-Manager queue."""
    entry = body.entry
    if entry is None:
        if not body.filename:
            raise HTTPException(400, "Нужен filename или entry.")
        db = await _manager_model_db()
        entry = db.get(body.filename)
        if entry is None:
            raise HTTPException(
                404,
                f"Модель '{body.filename}' не найдена в каталоге ComfyUI-Manager. "
                "Добавьте её вручную в ComfyUI или укажите entry с url.",
            )
    try:
        code = await _node_post("/manager/queue/install_model", json_body=entry)
        if code >= 400:
            raise HTTPException(502, f"ComfyUI-Manager отклонил установку (HTTP {code}).")
        await _node_post("/manager/queue/start", json_body=None)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Не удалось запустить загрузку: {exc}")
    return {"ok": True, "queued": entry.get("filename")}


@router.get("/models/install-status", dependencies=_admin)
async def install_status() -> dict:
    """ComfyUI-Manager download queue progress."""
    try:
        return await _node_get("/manager/queue/status")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"ComfyUI-Manager недоступен: {exc}")
