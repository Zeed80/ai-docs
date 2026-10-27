"""Strata's container lifecycle: install, update, uninstall, side downloads.

The server container (strata_manager) used to be created once by hand with
``docker compose --profile strata up``, and a new Strata release meant
editing the pinned commit in compose and rebuilding on the host. Now the
panel does it over the Docker socket:

* **Update check** — the newest GitHub release (cached for hours) against the
  version of the image the server runs (its OCI version label).
* **Download** — from 0.2.1 Strata publishes ready images
  (ghcr.io/niko1221/strata:<version>-<flavor>). A one-shot ``docker:cli``
  container pulls the release's image and tags it ``ai-workspace/strata:<version>``.
  It is a container of its own, so a backend restart does not cut the pull;
  its labels say what it fetches and what to do with the result, so the job
  survives a restart too.
* **Apply** — the server container is recreated on the new image with its
  own settings (env, ports, volume, network aliases, GPU), keeping whether it
  was running. Model files live on the volume and are not touched.
* **Install / uninstall** — the same spec from scratch (the backend's own
  compose project, network and the strata_data volume), and the reverse.
* **Side download** — another one-shot container runs ``strata install`` for a
  variant from its own env file while the server keeps serving; switching to
  it afterwards is a restart, not an hour of download.
* **Memory limit** — the RAM reserve (strata_manager.ram_plan) is the server
  container's cgroup limit; a container whose limit differs is recreated
  before it starts.

The server's command is ENTRYPOINT_SCRIPT below; infra/docker-compose.yml
carries the same text for the compose path (test_strata_lifecycle keeps them
equal).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import tarfile
import time

import httpx
import structlog

from app.ai import gpu_runtime
from app.ai.providers import strata_manager
from app.ai.providers.strata_manager import StrataError
from app.config import settings

logger = structlog.get_logger()

REPO = "Niko1221/Strata"
IMAGE_REPO = "ai-workspace/strata"
CURRENT_TAG = "current"
BUILDER_IMAGE = "docker:29-cli"

# Ready-made images, from 0.2.1 on: <version>-cuda13 needs NVIDIA driver 580+,
# <version>-cuda12 575+ (docs/DOCKER.md, "Which image for which card").
IMAGE_SOURCE = "ghcr.io/niko1221/strata"
MIN_VERSION = "0.2.1"

# The 0.2 image starts through tini + /opt/strata/entrypoint.sh (`strata launch`).
# ENTRYPOINT_SCRIPT drives that layout; an image started otherwise (0.1.x:
# ./docker-entrypoint.sh + setup.py, or whatever a later release changes to) is
# neither fetched nor applied — the server it was put on would exit at once.
SUPPORTED_ENTRYPOINT = ["/usr/bin/tini", "--", "/opt/strata/entrypoint.sh"]
SERVER_ENTRYPOINT = ["/usr/bin/tini", "--", "/bin/sh", "-c"]
# docs/DOCKER.md: --shm-size 2g --stop-timeout 120 (the engine drains, then
# releases tens of GB of pinned RAM).
SHM_BYTES = 2 * 1024**3
STOP_TIMEOUT_S = 120
OCI_VERSION_LABEL = "org.opencontainers.image.version"
UNSUPPORTED_LAYOUT = (
    "Strata {version} запускается иначе, чем ожидает панель; установленная версия остаётся"
)
TOO_OLD = "Готовые образы Strata выходят с {min}; {version} панель не ставит"
# Settings of the 0.1.x image the 0.2 entrypoint warns about and ignores.
LEGACY_ENV = ("HOST", "LOW_RAM", "RESIDENT_BUDGET_GIB", "REINSTALL", "KV_STREAMING", "CONFIG")


def release_image(version: str) -> str:
    return f"{IMAGE_SOURCE}:{version}-{settings.strata_image_flavor}"


NO_IMAGE_YET = (
    "Вышла Strata {version}, но готового образа {flavor} к ней пока нет — "
    "обновление станет доступно, когда авторы его опубликуют"
)
NEWER_WITHOUT_IMAGE = "Strata {version} вышла без готового образа; доступна {image_version}"
_PUBLISHED_TAG = re.compile(r"^(\d+(?:\.\d+)+)-(.+)$")


async def published_versions(client: httpx.AsyncClient, flavor: str) -> list[str]:
    """Versions with a ready image for ``flavor`` in ghcr, oldest first.

    A GitHub release is not an image: 0.2.4-0.2.6 came out with no image
    while the newest tag was 0.2.3, and the panel pulled 0.2.6-cuda13 —
    "manifest unknown" (live, 2026-10-26).
    """
    repo = IMAGE_SOURCE.removeprefix("ghcr.io/")
    t = await client.get("https://ghcr.io/token", params={"scope": f"repository:{repo}:pull"})
    t.raise_for_status()
    r = await client.get(
        f"https://ghcr.io/v2/{repo}/tags/list",
        params={"n": 1000},
        headers={"Authorization": f"Bearer {t.json()['token']}"},
    )
    r.raise_for_status()
    found = []
    for tag in r.json().get("tags") or []:
        m = _PUBLISHED_TAG.match(tag)
        if m and m.group(2) == flavor:
            found.append(m.group(1))
    return sorted(found, key=version_tuple)


def newest_image_version(published: list[str], release: str) -> str | None:
    """The newest published version not newer than the release."""
    ok = [v for v in published if version_tuple(v) <= version_tuple(release)]
    return max(ok, key=version_tuple) if ok else None


def release_supported(version: str | None) -> bool:
    return bool(version) and version_tuple(version) >= version_tuple(MIN_VERSION)


LABEL_ROLE = "aiw.strata.role"  # server | builder | installer
LABEL_VERSION = "aiw.strata.version"
LABEL_COMMIT = "aiw.strata.commit"
LABEL_TARGET_IMAGE = "aiw.strata.target_image"
# builder: apply (recreate the server) | create (install) | prepare (build only:
# the image waits, and "Обновить" later switches to it without compiling).
LABEL_THEN = "aiw.strata.then"
LABEL_VARIANT = "aiw.strata.variant"

_RELEASE_KEY = "strata:latest_release"
_RELEASE_TTL_S = 6 * 3600
_RELEASE_ERROR_TTL_S = 1800
_JOB_ERROR_KEY = "strata:job_error"
_APPLY_LOCK_KEY = "strata:apply_lock"
_UNSET = object()

ENTRYPOINT_SCRIPT = r"""# The backend (uid/gid 10001, appuser) writes aiw.env, aiw-runtime.json
# and the run configs and deletes quants on this volume, while
# everything here is created as root: give what it touches to its group
# (setgid dirs + umask 002 keep new files writable for it too).
# Without it, saving Strata settings in the panel failed with a 500.
# Only that: Strata keeps its runtime directory, connections and API key
# owner-only and refuses to start otherwise ("runtime directory must be
# owner-only (mode 0700)" - live, on the first restart after 0.2.1).
gid="${STRATA_SHARED_GID:-10001}"
mkdir -p /data/Strata-data /data/install
chgrp "$gid" /data /data/aiw*.env /data/aiw-runtime.json /data/config /data/config/strata-*.json 2>/dev/null
chmod g+rw /data/aiw*.env /data/aiw-runtime.json /data/config/strata-*.json 2>/dev/null
chmod g+rwxs /data /data/config 2>/dev/null
for d in /data/install /data/Strata-data; do
  chgrp -R "$gid" "$d" 2>/dev/null
  chmod -R g+rwX "$d" 2>/dev/null
  find "$d" -type d -exec chmod g+s {} + 2>/dev/null
done
chmod -R go-rwx /data/state /data/home /data/config/strata 2>/dev/null
umask 002
# The server reads /data/aiw.env; a side download (strata_lifecycle's
# installer container) names its own file, so it never changes what the
# server starts with.
envfile="${AIW_ENV_FILE:-/data/aiw.env}"
set -a
[ -f "$envfile" ] && . "$envfile"
set +a
# REINSTALL is one-shot: drop it so the next restart just serves.
[ -f "$envfile" ] && sed -i '/^REINSTALL=/d' "$envfile"
# The 0.1.x image kept models, MTP and packs in /data; 0.2 keeps them in
# /data/Strata-data (`strata launch --data-dir`). Same disk: a rename, and a
# link back so the older image still finds them.
for d in models mtp packs; do
  if [ -d "/data/$d" ] && [ ! -L "/data/$d" ] && [ ! -e "/data/Strata-data/$d" ]; then
    mv "/data/$d" "/data/Strata-data/$d" && ln -s "Strata-data/$d" "/data/$d"
  fi
done
PY=/opt/strata/terminal/python/bin/python3
FAMILY="${FAMILY:-qwen}"
MODEL="${MODEL:-IQ2_XS}"
case "$FAMILY" in qwen) prefix="" ;; *) prefix="${FAMILY}-" ;; esac
tag="${prefix}$(printf '%s' "$MODEL" | tr 'A-Z' 'a-z')"
cfg="/data/install/strata-$tag.json"
case "$(printf '%s' "${VISION:-yes}" | tr 'A-Z' 'a-z')" in no|off|false|0) vis=no ;; *) vis=yes ;; esac
# setup.py the way `strata launch` runs it (strata_cli/launch/runner.py:
# ensure_model, setup_args), so the launch after it finds the model
# "already installed with these choices" and serves it as it is.
set -- --yes --no-start --family "$FAMILY" --model "$MODEL"
[ -n "${CONTEXT:-}" ] && set -- "$@" --context "$CONTEXT"
set -- "$@" --vision "$vis" --kv "${KV:-int8}"
case "$FAMILY" in qwen|coder) set -- "$@" --experimental-speed-projection off ;; esac
set -- "$@" --data-dir /data/Strata-data
[ -n "${LOW_RAM:-}" ] && [ "$LOW_RAM" != auto ] && set -- "$@" --low-ram "$LOW_RAM"
[ -n "${RESIDENT_BUDGET_GIB:-}" ] && set -- "$@" --resident-budget-gib "$RESIDENT_BUDGET_GIB"
setup_pass() {
  ( cd /opt/strata/terminal && env -u PYTHONHOME -u VIRTUAL_ENV STRATA_CONTROL_INSTALL=1 \
      STRATA_INSTALL_DIR=/data/install STRATA_NEED_ENGINE=1 PYTHONNOUSERSITE=1 \
      PYTHONPATH=/opt/strata/terminal "$PY" setup.py "$@" )
}
if [ "${INSTALL_ONLY:-0}" = "1" ]; then
  # Download/setup only, then exit. The GPU stays with Ollama meanwhile.
  setup_pass "$@"
  exit $?
fi
# A setup pass first when the variant has no run config yet (or REINSTALL,
# or a RAM mode `strata launch` cannot ask for): the residency keys below
# must be in the config before the first serve, not after a restart.
[ "${REINSTALL:-0}" = "1" ] && rm -f "$cfg"
if [ ! -f "$cfg" ] || { [ -n "${LOW_RAM:-}" ] && [ "$LOW_RAM" != auto ]; } || [ -n "${RESIDENT_BUDGET_GIB:-}" ]; then
  setup_pass "$@" || exit 1
fi
# Runtime settings from the backend's /data/aiw-runtime.json (same rules
# as strata_manager.merge_run_config): "config_keys" are run-config keys
# (idle unload, ComfyUI hand-off, parallel), "config_args" engine flags
# (conversation cache); null removes. Setup keeps both on later passes
# (keys it does not own; hand-added args as "user_args").
"$PY" - "$cfg" <<'PY'
import json, pathlib, sys
cfg = pathlib.Path(sys.argv[1])
rt = pathlib.Path("/data/aiw-runtime.json")
if cfg.exists() and rt.exists():
    body = json.loads(rt.read_text())
    keys = body.get("config_keys") or {}
    flags = body.get("config_args") or {}
    c = json.loads(cfg.read_text())
    for k, v in keys.items():
        if v is None:
            c.pop(k, None)
        else:
            c[k] = v
    argv = list(c.get("args") or [])
    for flag, v in flags.items():
        if flag in argv:
            i = argv.index(flag)
            del argv[i:i + 2]
        if v is not None:
            argv += [flag, v]
    if "args" in c or argv:
        c["args"] = argv
    cfg.write_text(json.dumps(c, indent=1))
    print(f"[aiw] runtime settings applied to {cfg.name}: {sorted(keys) + sorted(flags)}")
PY
unset LOW_RAM RESIDENT_BUDGET_GIB REINSTALL HOST INSTALL_ONLY AIW_ENV_FILE
exec /opt/strata/entrypoint.sh run
"""


# ── Docker plumbing ─────────────────────────────────────────────────────────


async def _get(path: str, /, **params) -> httpx.Response:
    async with strata_manager._docker() as client:
        return await client.get(path, params=params or None)


async def _post(path: str, /, *, json_body: dict | None = None, timeout: float = 30.0, **params):
    async with strata_manager._docker() as client:
        return await client.post(path, params=params or None, json=json_body, timeout=timeout)


async def _delete(path: str, /, **params) -> httpx.Response:
    async with strata_manager._docker() as client:
        return await client.delete(path, params=params or None)


def _ok(r: httpx.Response, what: str, *codes: int) -> httpx.Response:
    if r.status_code not in (codes or (200, 201, 204)):
        raise StrataError(f"docker {what}: HTTP {r.status_code} {r.text[:300]}")
    return r


async def _inspect(container_id: str) -> dict:
    return _ok(await _get(f"/containers/{container_id}/json"), "inspect").json()


async def _image(ref: str) -> dict | None:
    r = await _get(f"/images/{ref}/json")
    return r.json() if r.status_code == 200 else None


async def _by_role(role: str) -> dict | None:
    if not os.path.exists(strata_manager._DOCKER_SOCK):
        return None
    filters = json.dumps({"label": [f"{LABEL_ROLE}={role}"]})
    r = await _get("/containers/json", all="true", filters=filters)
    if r.status_code != 200:
        return None
    rows = r.json()
    return rows[0] if rows else None


async def _remove(container_id: str) -> None:
    r = await _delete(f"/containers/{container_id}", force="true")
    if r.status_code not in (204, 404):
        raise StrataError(f"docker rm: HTTP {r.status_code} {r.text[:200]}")


async def _self() -> dict:
    """The backend's own container: its compose project, networks and the Strata volume."""
    name = os.environ.get("HOSTNAME") or ""
    r = await _get(f"/containers/{name}/json") if name else None
    if r is None or r.status_code != 200:
        raise StrataError(
            "Бэкенд не нашёл свой контейнер через Docker — установить Strata отсюда не выйдет"
        )
    return r.json()


def _user_env(container_env: list[str], image_env: list[str]) -> list[str]:
    """The container's own env: what the image defines (PATH, CUDA_*) belongs to the image.

    Carrying the old image's values over would pin them on the new image —
    a CUDA upgrade between releases would start with the old paths.
    """
    inherited = set(image_env or [])
    # NVIDIA_* decide which GPUs the runtime hands over: the service sets them
    # (to the CUDA image's own values today), so they stay explicit.
    return [e for e in container_env or [] if e not in inherited or e.startswith("NVIDIA_")]


def wanted_memory_bytes() -> int:
    """The server's cgroup limit for the current RAM reserve (0: none)."""
    desired = strata_manager.read_desired()
    plan = strata_manager.ram_plan(desired["id"], desired["context"])
    limit = plan.get("limit_gb")
    return int(limit * 2**30) if limit else 0


# ── Versions ────────────────────────────────────────────────────────────────


def version_tuple(version: str | None) -> tuple[int, ...]:
    out = []
    for part in (version or "").lstrip("v").split("."):
        if not part.isdigit():
            break
        out.append(int(part))
    return tuple(out)


def _redis():
    from app.utils.redis_client import get_sync_redis

    return get_sync_redis()


async def latest_release(*, force: bool = False) -> dict:
    """The newest Strata release on GitHub: {version, tag, commit, published_at, notes, url}.

    Cached for hours (GitHub allows 60 anonymous calls an hour); a failed
    check is cached for half an hour as {"error": ...}.
    """
    if not force:
        try:
            raw = _redis().get(_RELEASE_KEY)
            if raw:
                return json.loads(raw)
        except Exception:  # noqa: BLE001 — no cache: ask GitHub
            pass
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ai-workspace-strata"}
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            r = await client.get(f"https://api.github.com/repos/{REPO}/releases/latest")
            r.raise_for_status()
            rel = r.json()
            tag = rel["tag_name"]
            c = await client.get(f"https://api.github.com/repos/{REPO}/commits/{tag}")
            c.raise_for_status()
            out = {
                "version": tag.lstrip("v"),
                "tag": tag,
                "commit": c.json()["sha"],
                "published_at": rel.get("published_at"),
                "name": rel.get("name") or tag,
                "notes": (rel.get("body") or "")[:4000],
                "url": rel.get("html_url"),
                "checked_at": int(time.time()),
            }
            flavor = settings.strata_image_flavor
            image_version = newest_image_version(
                await published_versions(client, flavor), out["version"]
            )
            out["flavor"] = flavor
            out["image_version"] = image_version
            out["image_commit"] = out["commit"]
            if image_version and image_version != out["version"]:
                ic = await client.get(
                    f"https://api.github.com/repos/{REPO}/commits/v{image_version}"
                )
                out["image_commit"] = ic.json().get("sha") if ic.status_code == 200 else None
        out["supported"] = release_supported(out.get("image_version"))
        ttl = _RELEASE_TTL_S
    except Exception as exc:  # noqa: BLE001
        out = {"error": f"GitHub недоступен: {str(exc)[:200]}", "checked_at": int(time.time())}
        ttl = _RELEASE_ERROR_TTL_S
    try:
        _redis().set(_RELEASE_KEY, json.dumps(out), ex=ttl)
    except Exception:  # noqa: BLE001
        pass
    return out


async def _build_json_version(container_id: str) -> str | None:
    """The engine version from /opt/strata/engine/BUILD.json (works on a stopped container)."""
    r = await _get(f"/containers/{container_id}/archive", path="/opt/strata/engine/BUILD.json")
    if r.status_code != 200:
        return None
    try:
        with tarfile.open(fileobj=io.BytesIO(r.content)) as tar:
            member = tar.getmembers()[0]
            data = tar.extractfile(member).read()  # type: ignore[union-attr]
        return json.loads(data).get("version")
    except Exception:  # noqa: BLE001
        return None


async def server_version(container: dict | None = None) -> dict | None:
    """{version, commit, image, image_id} of the image the server container runs."""
    container = container or await strata_manager._find_container()
    if not container:
        return None
    image_id = container.get("ImageID") or ""
    info = await _image(image_id) if image_id else None
    labels = (info or {}).get("Config", {}).get("Labels") or {}
    version = labels.get(LABEL_VERSION) or labels.get(OCI_VERSION_LABEL)
    commit = labels.get(LABEL_COMMIT)
    if not version:
        cache_key = f"strata:image_version:{image_id}"
        try:
            version = (_redis().get(cache_key) or b"").decode() or None
        except Exception:  # noqa: BLE001
            version = None
        if not version:
            version = await _build_json_version(container["Id"])
            if version:
                try:
                    _redis().set(cache_key, version, ex=30 * 86400)
                except Exception:  # noqa: BLE001
                    pass
    image = container.get("Image") or ""
    if not commit and ":" in image:
        tag = image.rsplit(":", 1)[1]  # ai-workspace/strata:a0116b2 (built), :0.2.1 (pulled)
        commit = tag if tag != version else None
    return {"version": version, "commit": commit, "image": image, "image_id": image_id}


async def update_info() -> dict:
    """What the panel shows: current version, newest release, whether to offer the update."""
    container = await strata_manager._find_container()
    current = await server_version(container) if container else None
    latest = await latest_release()
    job = await job_status()
    if job and job["stage"] in ("building", "built"):
        watch()  # the background loop applies a finished build; a GET never waits for it
    target = latest.get("image_version")
    newer_release = bool(
        current
        and current.get("version")
        and latest.get("version")
        and version_tuple(latest["version"]) > version_tuple(current["version"])
    )
    available = bool(
        newer_release
        and target
        and latest.get("supported") is not False
        and version_tuple(target) > version_tuple(current["version"])
    )
    blocked = None
    if newer_release and not available:
        blocked = (
            NO_IMAGE_YET.format(version=latest["version"], flavor=latest.get("flavor") or "")
            if not target or release_supported(target)
            else TOO_OLD.format(min=MIN_VERSION, version=target)
        )
    elif available and target != latest.get("version"):
        blocked = NEWER_WITHOUT_IMAGE.format(version=latest["version"], image_version=target)
    return {
        "current": current,
        "latest": latest,
        "update_available": available,
        "update_blocked": blocked,
        "job": job,
    }


# ── Build job ───────────────────────────────────────────────────────────────


def _project_name(me: dict | None) -> str:
    labels = ((me or {}).get("Config") or {}).get("Labels") or {}
    return labels.get("com.docker.compose.project") or "infra"


async def _ensure_builder_image() -> None:
    if await _image(BUILDER_IMAGE):
        return
    name, tag = BUILDER_IMAGE.split(":", 1)
    async with strata_manager._docker() as client:
        r = await client.post(
            "/images/create", params={"fromImage": name, "tag": tag}, timeout=900.0
        )
    if r.status_code != 200 or not await _image(BUILDER_IMAGE):
        raise StrataError(
            f"Не удалось скачать {BUILDER_IMAGE}: HTTP {r.status_code} {r.text[:200]}"
        )


def target_image(version: str) -> str:
    return f"{IMAGE_REPO}:{version}"


async def start_build(*, version: str, commit: str, then: str) -> dict:
    """Fetch Strata ``version``'s ready image in the background; ``then``: apply | create | prepare."""
    if not release_supported(version):
        raise StrataError(TOO_OLD.format(min=MIN_VERSION, version=version))
    current = await _by_role("builder")
    if current and current.get("State") == "running":
        raise StrataError("Образ Strata уже скачивается")
    if current:
        await _remove(current["Id"])
    _clear_job_error()
    image = target_image(version)
    if await _image(image):
        # Fetched before (prepared ahead, or an earlier attempt whose apply
        # failed): go straight to the next step.
        if then != "prepare":
            await _finish(image, then)
        return {"image": image, "built": True}
    await _ensure_builder_image()
    try:
        project = _project_name(await _self())
    except StrataError:
        project = "infra"
    source = release_image(version)
    # Pull, keep it under our own name (uninstall and :current work on
    # IMAGE_REPO), drop the registry tag (the image itself stays).
    script = 'docker pull "$0" && docker tag "$0" "$1" && docker rmi "$0" >/dev/null'
    body = {
        "Image": BUILDER_IMAGE,
        "Cmd": ["sh", "-c", script, source, image],
        "Labels": {
            LABEL_ROLE: "builder",
            LABEL_VERSION: version,
            LABEL_COMMIT: commit,
            LABEL_TARGET_IMAGE: image,
            LABEL_THEN: then,
        },
        "HostConfig": {"Binds": ["/var/run/docker.sock:/var/run/docker.sock"]},
    }
    created = _ok(
        await _post("/containers/create", json_body=body, name=f"{project}-strata-builder"),
        "create builder",
    ).json()
    _ok(await _post(f"/containers/{created['Id']}/start"), "start builder", 204, 304)
    logger.info("strata_pull_started", version=version, image=source, then=then)
    watch()
    return {"image": image, "built": False}


def _clear_job_error() -> None:
    try:
        _redis().delete(_JOB_ERROR_KEY)
    except Exception:  # noqa: BLE001
        pass


def _job_error() -> str | None:
    try:
        raw = _redis().get(_JOB_ERROR_KEY)
        return raw.decode() if isinstance(raw, bytes) else raw
    except Exception:  # noqa: BLE001
        return None


async def job_status() -> dict | None:
    """The build job, from its container: building | built | failed (+ log tail)."""
    c = await _by_role("builder")
    if not c:
        return None
    labels = c.get("Labels") or {}
    info = await _inspect(c["Id"])
    state = info.get("State") or {}
    if state.get("Running"):
        stage = "building"
    elif state.get("ExitCode") == 0 and state.get("FinishedAt", "").startswith("0001"):
        stage = "building"  # created, not started yet
    elif state.get("ExitCode") == 0:
        stage = "built"
    else:
        stage = "failed"
    error = _job_error()
    if stage == "built" and error:
        stage = "apply_failed"
    try:
        logs = await strata_manager.log_tail(30 if stage != "building" else 12, c["Id"])
    except Exception:  # noqa: BLE001
        logs = []
    return {
        "stage": stage,
        "version": labels.get(LABEL_VERSION),
        "commit": labels.get(LABEL_COMMIT),
        "image": labels.get(LABEL_TARGET_IMAGE),
        "then": labels.get(LABEL_THEN),
        "started_at": state.get("StartedAt"),
        "exit_code": state.get("ExitCode"),
        "error": error,
        "log_tail": logs,
    }


async def advance_job() -> dict | None:
    """A finished build is applied once (the next step its labels name); returns the job."""
    job = await job_status()
    if not job or job["stage"] != "built":
        return job
    if job["then"] == "prepare":
        builder = await _by_role("builder")
        if builder:
            await _remove(builder["Id"])
        logger.info("strata_image_prepared", image=job["image"])
        return None
    try:
        got = _redis().set(_APPLY_LOCK_KEY, "1", nx=True, ex=900)
    except Exception:  # noqa: BLE001
        got = True
    if not got:
        return job
    try:
        await _finish(job["image"], job["then"] or "apply")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)[:400]
        logger.warning("strata_apply_failed", image=job["image"], error=msg)
        try:
            _redis().set(_JOB_ERROR_KEY, msg)
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            _redis().delete(_APPLY_LOCK_KEY)
        except Exception:  # noqa: BLE001
            pass
    return await job_status()


async def retry_apply() -> dict | None:
    _clear_job_error()
    return await advance_job()


async def dismiss_job() -> None:
    c = await _by_role("builder")
    if c and c.get("State") == "running":
        await _post(f"/containers/{c['Id']}/stop", t="5")
    if c:
        await _remove(c["Id"])
    _clear_job_error()


async def _finish(image: str, then: str) -> None:
    """Point :current at ``image`` and put the server on it (create it for an install)."""
    built = await _image(image)
    entrypoint = ((built or {}).get("Config") or {}).get("Entrypoint")
    if built is not None and entrypoint != SUPPORTED_ENTRYPOINT:
        labels = (built.get("Config") or {}).get("Labels") or {}
        version = labels.get(OCI_VERSION_LABEL) or labels.get(LABEL_VERSION) or image
        raise StrataError(UNSUPPORTED_LAYOUT.format(version=version))
    _ok(
        await _post(f"/images/{image}/tag", repo=IMAGE_REPO, tag=CURRENT_TAG),
        "tag",
        201,
        200,
    )
    if await strata_manager._find_container() is None:
        await create_server(image)
    else:
        await recreate_server(image=image)
    builder = await _by_role("builder")
    if builder:
        await _remove(builder["Id"])
    _clear_job_error()
    logger.info("strata_image_applied", image=image, then=then)


_watcher: asyncio.Task | None = None


def watch() -> None:
    """Finish a running build in the background, even with nobody on the page."""
    global _watcher
    if _watcher and not _watcher.done():
        return

    async def _loop() -> None:
        while True:
            try:
                job = await job_status()
                if not job or job["stage"] != "building":
                    await advance_job()
                    return
            except Exception as exc:  # noqa: BLE001
                logger.warning("strata_build_watch_failed", error=str(exc)[:200])
            await asyncio.sleep(20)

    try:
        _watcher = asyncio.get_running_loop().create_task(_loop())
    except RuntimeError:
        _watcher = None


async def resume() -> None:
    """At startup: a build started before the restart is watched again."""
    try:
        if await _by_role("builder"):
            watch()
    except Exception as exc:  # noqa: BLE001
        logger.warning("strata_build_resume_failed", error=str(exc)[:200])


async def start_update() -> dict:
    """Build the newest release and put the server on it."""
    latest = await latest_release(force=True)
    if latest.get("error"):
        raise StrataError(latest["error"])
    container = await strata_manager._find_container()
    if container is None:
        raise StrataError(strata_manager.NOT_INSTALLED)
    target = latest.get("image_version")
    if not target:
        raise StrataError(
            NO_IMAGE_YET.format(version=latest["version"], flavor=latest.get("flavor") or "")
        )
    current = await server_version(container)
    if current and version_tuple(current.get("version")) >= version_tuple(target):
        if version_tuple(target) < version_tuple(latest["version"]):
            raise StrataError(
                NO_IMAGE_YET.format(version=latest["version"], flavor=latest.get("flavor") or "")
            )
        raise StrataError(f"Уже установлена последняя версия {current.get('version')}")
    return await start_build(version=target, commit=latest.get("image_commit") or "", then="apply")


async def install_server() -> dict:
    """From scratch: the image (build the newest release if there is none) and the container."""
    if await strata_manager._find_container():
        raise StrataError("Strata уже установлена")
    existing = await _image(f"{IMAGE_REPO}:{CURRENT_TAG}")
    if existing:
        await create_server(f"{IMAGE_REPO}:{CURRENT_TAG}")
        return {"built": True}
    latest = await latest_release(force=True)
    if latest.get("error"):
        raise StrataError(latest["error"])
    target = latest.get("image_version")
    if not target:
        raise StrataError(
            NO_IMAGE_YET.format(version=latest["version"], flavor=latest.get("flavor") or "")
        )
    return await start_build(version=target, commit=latest.get("image_commit") or "", then="create")


# ── The server container ────────────────────────────────────────────────────


def _memory(host: dict, memory: int) -> dict:
    out = dict(host)
    out["Memory"] = memory
    # No swap beside the limit: experts swapped out are slower than the SSD
    # path, and the reserve is about leaving RAM to the rest of the server.
    out["MemorySwap"] = memory if memory else 0
    return out


async def _spec_from(info: dict, *, image: str, memory: int) -> dict:
    cfg = info.get("Config") or {}
    old_image = await _image(info.get("Image") or "") or {}
    new_image = await _image(image)
    if not new_image:
        raise StrataError(f"Образ {image} не найден")
    labels = {
        k: v
        for k, v in (cfg.get("Labels") or {}).items()
        if k.startswith("com.docker.compose.") or k.startswith("aiw.")
    }
    labels["com.docker.compose.image"] = new_image["Id"]
    labels[LABEL_ROLE] = "server"
    short = (info.get("Id") or "")[:12]
    networks = {
        name: {"Aliases": [a for a in (ep.get("Aliases") or []) if a != short]}
        for name, ep in ((info.get("NetworkSettings") or {}).get("Networks") or {}).items()
    }
    env = _user_env(cfg.get("Env") or [], (old_image.get("Config") or {}).get("Env") or [])
    host = _memory(info.get("HostConfig") or {}, memory)
    host["ShmSize"] = max(int(host.get("ShmSize") or 0), SHM_BYTES)
    return {
        "Image": image,
        "Env": [e for e in env if e.split("=", 1)[0] not in LEGACY_ENV],
        "Labels": labels,
        "ExposedPorts": cfg.get("ExposedPorts") or {"8080/tcp": {}},
        "Entrypoint": SERVER_ENTRYPOINT,
        "Cmd": [ENTRYPOINT_SCRIPT],
        "StopTimeout": max(int(cfg.get("StopTimeout") or 0), STOP_TIMEOUT_S),
        "HostConfig": host,
        "NetworkingConfig": {"EndpointsConfig": networks},
    }


async def recreate_server(
    *, image: str | None = None, memory=_UNSET, start: bool | None = None
) -> None:
    """The server container again, on ``image`` and/or with a new memory limit.

    Its own settings are kept; ``start`` None keeps whether it was running.
    The old one is renamed aside first and removed only once the new one is
    created (and started), so a failure puts it back as it was.
    """
    c = await strata_manager._find_container()
    if c is None:
        raise StrataError(strata_manager.NOT_INSTALLED)
    info = await _inspect(c["Id"])
    was_running = bool((info.get("State") or {}).get("Running"))
    mem = wanted_memory_bytes() if memory is _UNSET else int(memory)
    spec = await _spec_from(info, image=image or info["Config"]["Image"], memory=mem)
    name = info["Name"].lstrip("/")
    run = was_running if start is None else start
    if was_running:
        await strata_manager._container_action("stop", timeout=60)
    retired = f"{name}-retired-{int(time.time())}"
    _ok(await _post(f"/containers/{c['Id']}/rename", name=retired), "rename", 204)
    new_id = None
    try:
        new_id = _ok(await _post("/containers/create", json_body=spec, name=name), "create").json()[
            "Id"
        ]
        if run:
            strata_manager.apply_runtime_keys()
            _ok(await _post(f"/containers/{new_id}/start"), "start", 204, 304)
    except Exception:
        if new_id:
            await _remove(new_id)
        await _post(f"/containers/{c['Id']}/rename", name=name)
        if was_running:
            await _post(f"/containers/{c['Id']}/start")
        raise
    await _remove(c["Id"])
    logger.info("strata_server_recreated", image=spec["Image"], memory=mem, running=run)


async def create_server(image: str) -> None:
    """A new server container (stopped): the compose service's settings, from the backend's own."""
    me = await _self()
    project = _project_name(me)
    mounts = me.get("Mounts") or []
    volume = next(
        (
            m.get("Name")
            for m in mounts
            if m.get("Destination") == settings.strata_data_dir and m.get("Name")
        ),
        None,
    )
    if not volume:
        raise StrataError(
            f"У бэкенда нет тома Strata ({settings.strata_data_dir}). Пересоберите стек — том "
            "strata_data подключается к бэкенду в infra/docker-compose.yml."
        )
    nets = list(((me.get("NetworkSettings") or {}).get("Networks") or {}).keys())
    network = next((n for n in nets if n.endswith("_app")), nets[0] if nets else None)
    me_labels = (me.get("Config") or {}).get("Labels") or {}
    name = f"{project}-strata-1"
    labels = {
        "com.docker.compose.project": project,
        "com.docker.compose.service": settings.strata_service_name,
        "com.docker.compose.container-number": "1",
        "com.docker.compose.oneoff": "False",
        LABEL_ROLE: "server",
    }
    for key in (
        "com.docker.compose.project.config_files",
        "com.docker.compose.project.working_dir",
    ):
        if key in me_labels:
            labels[key] = me_labels[key]
    key = settings.strata_api_key or ""
    # Without a key the server answers anyone who reaches the port: keep it local.
    bind = settings.strata_bind if key else "127.0.0.1"
    env = [
        "NVIDIA_VISIBLE_DEVICES=all",
        "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
        f"FAMILY={strata_manager.DEFAULT_FAMILY}",
        f"MODEL={strata_manager.DEFAULT_QUANT}",
        f"CONTEXT={strata_manager.DEFAULT_CONTEXT}",
        "VISION=yes",
        "PORT=8080",
        f"API_KEY={key}",
    ]
    host = {
        "Binds": [f"{volume}:/data"],
        "Runtime": "nvidia",
        "Ulimits": [{"Name": "memlock", "Soft": -1, "Hard": -1}],
        "ShmSize": SHM_BYTES,
        "PortBindings": {
            "8080/tcp": [{"HostIp": bind, "HostPort": str(settings.strata_host_port)}]
        },
        "ExtraHosts": ["host-gateway:host-gateway"],
        "RestartPolicy": {"Name": "no"},
    }
    spec = {
        "Image": image,
        "Env": env,
        "Labels": labels,
        "ExposedPorts": {"8080/tcp": {}},
        "Entrypoint": SERVER_ENTRYPOINT,
        "Cmd": [ENTRYPOINT_SCRIPT],
        "StopTimeout": STOP_TIMEOUT_S,
        "HostConfig": _memory(host, wanted_memory_bytes()),
        "NetworkingConfig": {
            "EndpointsConfig": {network: {"Aliases": [settings.strata_service_name, name]}}
            if network
            else {}
        },
    }
    _ok(await _post("/containers/create", json_body=spec, name=name), "create")
    gpu_runtime.set_owner(gpu_runtime.OLLAMA)
    logger.info("strata_server_created", image=image, name=name, volume=volume)


async def _memory_matches(container_id: str) -> bool:
    info = await _inspect(container_id)
    return int((info.get("HostConfig") or {}).get("Memory") or 0) == wanted_memory_bytes()


async def start_server() -> None:
    """Start the server; a container with a stale memory limit is recreated first."""
    c = await strata_manager._find_container()
    if c is None:
        raise StrataError(strata_manager.NOT_INSTALLED)
    if not await _memory_matches(c["Id"]):
        await recreate_server(start=True)
        return
    await strata_manager._container_action("start")


async def restart_server() -> None:
    c = await strata_manager._find_container()
    if c is None:
        raise StrataError(strata_manager.NOT_INSTALLED)
    if not await _memory_matches(c["Id"]):
        await recreate_server(start=True)
        return
    await strata_manager._container_action("restart", timeout=60)


async def apply_memory_limit() -> bool:
    """After the reserve changed: recreate now if needed (keeps it running). True if recreated."""
    c = await strata_manager._find_container()
    if c is None or await _memory_matches(c["Id"]):
        return False
    await recreate_server()
    return True


# ── Side download ───────────────────────────────────────────────────────────


async def run_installer(vid: str, env_file: str) -> None:
    """Strata's setup for ``vid`` in a one-shot container beside the server."""
    server = await strata_manager._find_container()
    if server is None:
        raise StrataError(strata_manager.NOT_INSTALLED)
    old = await _by_role("installer")
    if old:
        if old.get("State") == "running":
            raise StrataError("Скачивание уже идёт")
        await _remove(old["Id"])
    info = await _inspect(server["Id"])
    cfg = info.get("Config") or {}
    image_env = ((await _image(info.get("Image") or "")) or {}).get("Config", {}).get("Env") or []
    hc = info.get("HostConfig") or {}
    nets = list(((info.get("NetworkSettings") or {}).get("Networks") or {}).keys())
    name = info["Name"].lstrip("/").rsplit("-", 1)[0] + "-installer"
    spec = {
        "Image": cfg.get("Image"),
        "Env": [
            *[
                e
                for e in _user_env(cfg.get("Env") or [], image_env)
                if not e.startswith("AIW_ENV_FILE=")
            ],
            f"AIW_ENV_FILE={env_file}",
        ],
        "Labels": {LABEL_ROLE: "installer", LABEL_VARIANT: vid},
        "Entrypoint": SERVER_ENTRYPOINT,
        "Cmd": [ENTRYPOINT_SCRIPT],
        "HostConfig": {
            "Binds": hc.get("Binds"),
            "Mounts": hc.get("Mounts"),
            # setup reads the card (nvidia-smi) to size the run config.
            "Runtime": hc.get("Runtime"),
            "DeviceRequests": hc.get("DeviceRequests"),
            "NetworkMode": nets[0] if nets else "bridge",
        },
    }
    created = _ok(
        await _post("/containers/create", json_body=spec, name=name), "create installer"
    ).json()
    _ok(await _post(f"/containers/{created['Id']}/start"), "start installer", 204, 304)


async def installer_status() -> dict | None:
    """The side download: {variant, stage: downloading|done|failed, log_tail}.

    A finished one is removed here: its result is the variant's run config on
    the volume, which the quant list shows as downloaded.
    """
    c = await _by_role("installer")
    if not c:
        return None
    vid = (c.get("Labels") or {}).get(LABEL_VARIANT)
    info = await _inspect(c["Id"])
    state = info.get("State") or {}
    if state.get("Running"):
        stage = "downloading"
    elif state.get("ExitCode") == 0 and vid in strata_manager.installed_quants():
        await _remove(c["Id"])
        return None
    else:
        stage = "failed"
    try:
        logs = await strata_manager.log_tail(14 if stage == "downloading" else 30, c["Id"])
    except Exception:  # noqa: BLE001
        logs = []
    return {
        "variant": vid,
        "stage": stage,
        "exit_code": state.get("ExitCode"),
        "size_on_disk_gb": strata_manager._size_on_disk_gb(vid)
        if vid in strata_manager.QUANTS
        else None,
        "disk_need_gb": strata_manager.disk_need_gb(vid) if vid in strata_manager.QUANTS else None,
        "log_tail": logs,
    }


async def installer_variant() -> str | None:
    """The variant a side download is fetching right now (None: none running)."""
    try:
        c = await _by_role("installer")
    except Exception:  # noqa: BLE001
        return None
    if not c or c.get("State") != "running":
        return None
    return (c.get("Labels") or {}).get(LABEL_VARIANT)


async def stop_installer() -> None:
    c = await _by_role("installer")
    if not c:
        return
    if c.get("State") == "running":
        await _post(f"/containers/{c['Id']}/stop", t="10")
    await _remove(c["Id"])


# ── Uninstall ───────────────────────────────────────────────────────────────

_DATA_ITEMS = ("Strata-data", "install", "state", "home", "models", "packs", "mtp", "config")
_DATA_FILES = (
    strata_manager.ENV_FILE_NAME,
    strata_manager.INSTALL_ENV_FILE_NAME,
    strata_manager.RUNTIME_FILE_NAME,
)


async def uninstall(*, wipe_models: bool) -> dict:
    """Remove the containers and images; with ``wipe_models`` also the downloaded models.

    The caller has already given the GPU back to Ollama (and the slots).
    The volume itself stays (the backend mounts it); without ``wipe_models``
    a later install finds the models and starts without a download.
    """
    for role in ("installer", "builder"):
        c = await _by_role(role)
        if c:
            await _remove(c["Id"])
    server = await strata_manager._find_container()
    if server:
        if server.get("State") == "running":
            await strata_manager._container_action("stop", timeout=60)
        await _remove(server["Id"])
    gpu_runtime.set_owner(gpu_runtime.OLLAMA)
    removed_images = []
    filters = json.dumps({"reference": [IMAGE_REPO]})
    r = await _get("/images/json", filters=filters)
    for img in r.json() if r.status_code == 200 else []:
        d = await _delete(f"/images/{img['Id']}", force="true")
        if d.status_code == 200:
            removed_images += img.get("RepoTags") or [img["Id"][:19]]
    freed = 0.0
    if wipe_models:
        root = strata_manager.data_dir()
        for item in _DATA_ITEMS:
            path = root / item
            if path.is_symlink():  # /data/models -> Strata-data/models after the 0.2 move
                path.unlink()
                continue
            size = strata_manager._dir_size_gb(path) or 0.0
            try:
                shutil.rmtree(path)
                freed += size
            except FileNotFoundError:
                pass
            except PermissionError as exc:
                raise StrataError(f"Нет прав на удаление {path}") from exc
        for name in _DATA_FILES:
            (root / name).unlink(missing_ok=True)
    try:
        strata_manager.set_switch_revision(None)
        strata_manager.set_switch_tasks({})
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        "strata_uninstalled", images=removed_images, wipe_models=wipe_models, freed_gb=freed
    )
    return {"removed_images": removed_images, "freed_gb": round(freed, 1)}
