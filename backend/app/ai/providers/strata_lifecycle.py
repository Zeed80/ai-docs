"""Strata's container lifecycle: install, update, uninstall, side downloads.

The server container (strata_manager) used to be created once by hand with
``docker compose --profile strata up``, and a new Strata release meant
editing the pinned commit in compose and rebuilding on the host. Now the
panel does it over the Docker socket:

* **Update check** — the newest GitHub release (cached for hours) against the
  version of the image the server runs (its ``aiw.strata.version`` label, or
  the engine's own BUILD.json for an image built before the labels).
* **Build** — a one-shot ``docker:cli`` container runs ``docker build`` of the
  release's commit (Strata's Dockerfile needs BuildKit, which the Engine
  API's /build does not give). It is a container of its own, so a backend
  restart does not cut the half-hour CUDA compile; its labels say what it
  builds and what to do with the result, so the job survives a restart too.
* **Apply** — the server container is recreated on the new image with its
  own settings (env, ports, volume, network aliases, GPU), keeping whether it
  was running. Model files live on the volume and are not touched.
* **Install / uninstall** — the same spec from scratch (the backend's own
  compose project, network and the strata_data volume), and the reverse.
* **Side download** — another one-shot container runs Strata's setup for a
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
GIT_URL = f"https://github.com/{REPO}.git"
IMAGE_REPO = "ai-workspace/strata"
CURRENT_TAG = "current"
BUILDER_IMAGE = "docker:29-cli"

# The Terminal stage of Strata's root Dockerfile runs packaging/package_terminal.py,
# which asks `git rev-parse HEAD` for the archive's COMMIT file. A build from the
# git URL has neither .git nor git in that stage: v0.2.1 crashes on the missing
# binary, later sources read STRATA_BUILD_COMMIT first. The build therefore gets
# the Dockerfile patched to pass the commit both ways (env + a one-command git shim
# on PATH for that RUN only); a Dockerfile without the step is built unchanged.
PACKAGE_TERMINAL_RUN = "RUN python3 /src/packaging/package_terminal.py"
_GIT_SHIM = (
    "RUN mkdir -p /opt/aiw-git"
    " && printf '#!/bin/sh\\necho \"$STRATA_BUILD_COMMIT\"\\n' > /opt/aiw-git/git"
    " && chmod +x /opt/aiw-git/git\n"
)


def patch_dockerfile(text: str) -> str | None:
    """Strata's Dockerfile with the commit passed into package_terminal.py; None if the step is gone."""
    if text.count(PACKAGE_TERMINAL_RUN) != 1:
        return None
    head = "ARG STRATA_COMMIT=\nENV STRATA_BUILD_COMMIT=${STRATA_COMMIT}\n" + _GIT_SHIM
    run = "RUN PATH=/opt/aiw-git:$PATH python3 /src/packaging/package_terminal.py"
    return text.replace(PACKAGE_TERMINAL_RUN, head + run)


async def _fetch_dockerfile(commit: str) -> str | None:
    url = f"https://raw.githubusercontent.com/{REPO}/{commit}/Dockerfile"
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "ai-workspace-strata"})
            r.raise_for_status()
            return r.text
    except httpx.HTTPError as exc:
        logger.warning("strata_dockerfile_fetch_failed", commit=commit, error=str(exc))
        return None


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
# everything here is created as root: give the volume to its group
# (setgid dirs + umask 002 keep new files writable for it too).
# Without it, saving Strata settings in the panel failed with a 500.
chgrp -R "${STRATA_SHARED_GID:-10001}" /data 2>/dev/null
chmod -R g+rwX /data 2>/dev/null
find /data -type d -exec chmod g+s {} + 2>/dev/null
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
case "${FAMILY:-qwen}" in qwen) prefix="" ;; *) prefix="${FAMILY}-" ;; esac
tag="${prefix}$(printf '%s' "${MODEL:-IQ2_XS}" | tr 'A-Z' 'a-z')"
# Setup only: Strata's entrypoint minus its final "start the server" line.
sed 's|^exec .venv/bin/python setup.py "\$@"$|exit 0|' docker-entrypoint.sh > /tmp/install-only.sh
grep -q '^exit 0$' /tmp/install-only.sh || { echo "install-only: entrypoint changed"; exit 1; }
if [ "${INSTALL_ONLY:-0}" = "1" ]; then
  # Download/setup only, then exit. The GPU stays with Ollama meanwhile.
  exec sh /tmp/install-only.sh
fi
# A setup pass (new quant, or REINSTALL after a context/images change)
# rewrites the run config, so it runs first and on its own: the
# residency keys below must land in the config it writes, or Strata
# stops unloading when idle after a context change.
if [ "${REINSTALL:-0}" = "1" ] || [ ! -f "/data/config/strata-$tag.json" ]; then
  sh /tmp/install-only.sh || exit 1
fi
export REINSTALL=0
# Several quants installed: setup.py starts "the newest strata-*.json"
# and, with more than one in /opt/strata, ASKS which — no TTY, so the
# start failed (live, after IQ2_XS joined IQ3_S). Keep only the chosen
# quant's link; the canonical copies live in /data/config.
for f in /opt/strata/strata-*.json; do
  [ -e "$f" ] || [ -L "$f" ] || continue
  [ "$(basename "$f")" = "strata-$tag.json" ] && continue
  rm -f "$f"
done
# A setup pass leaves the chosen quant's config as a regular FILE in
# /opt/strata (copied to /data/config at its end), and Strata starts
# from that file: later edits to /data/config (idle unload, parallel)
# never reached it. Make it a link to the canonical copy.
cur="/opt/strata/strata-$tag.json"
if [ -f "/data/config/strata-$tag.json" ] && [ ! -L "$cur" ]; then
  rm -f "$cur" && ln -s "/data/config/strata-$tag.json" "$cur"
fi
# Runtime settings from the backend's /data/aiw-runtime.json (same rules
# as strata_manager.merge_run_config): "config_keys" are run-config keys
# (idle unload, ComfyUI hand-off, parallel), "config_args" engine flags
# (conversation cache); null removes.
.venv/bin/python - "$tag" <<'PY'
import json, pathlib, sys
cfg = pathlib.Path(f"/data/config/strata-{sys.argv[1]}.json")
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
exec ./docker-entrypoint.sh
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
    version = labels.get(LABEL_VERSION)
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
        commit = image.rsplit(":", 1)[1]  # ai-workspace/strata:a0116b2
    return {"version": version, "commit": commit, "image": image, "image_id": image_id}


async def update_info() -> dict:
    """What the panel shows: current version, newest release, whether to offer the update."""
    container = await strata_manager._find_container()
    current = await server_version(container) if container else None
    latest = await latest_release()
    job = await job_status()
    if job and job["stage"] in ("building", "built"):
        watch()  # the background loop applies a finished build; a GET never waits for it
    available = bool(
        current
        and current.get("version")
        and latest.get("version")
        and version_tuple(latest["version"]) > version_tuple(current["version"])
    )
    return {
        "current": current,
        "latest": latest,
        "update_available": available,
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


def target_image(commit: str) -> str:
    return f"{IMAGE_REPO}:{commit[:7]}"


async def start_build(*, version: str, commit: str, then: str) -> dict:
    """Start building Strata ``version`` (``commit``); ``then``: apply | create."""
    current = await _by_role("builder")
    if current and current.get("State") == "running":
        raise StrataError("Сборка Strata уже идёт")
    if current:
        await _remove(current["Id"])
    _clear_job_error()
    image = target_image(commit)
    if await _image(image):
        # Built before (prepared ahead, or an earlier attempt whose apply
        # failed): nothing to compile, go straight to the next step.
        if then != "prepare":
            await _finish(image, then)
        return {"image": image, "built": True}
    await _ensure_builder_image()
    try:
        project = _project_name(await _self())
    except StrataError:
        project = "infra"
    cmd = [
        "docker", "build", "--progress=plain",
        "-t", image,
        "--build-arg", f"CUDA_ARCHITECTURES={settings.strata_cuda_architectures}",
        "--build-arg", "BUILD_VISION=1",
        "--build-arg", f"STRATA_COMMIT={commit}",
        "--label", f"{LABEL_VERSION}={version}",
        "--label", f"{LABEL_COMMIT}={commit}",
    ]  # fmt: skip
    env: list[str] = []
    original = await _fetch_dockerfile(commit)
    dockerfile = patch_dockerfile(original) if original else None
    if dockerfile:
        # Dockerfile from stdin, sources still from the git URL at that commit.
        env.append(f"STRATA_DOCKERFILE={dockerfile}")
        script = 'printf "%s" "$STRATA_DOCKERFILE" | ' + " ".join(cmd) + ' -f - "$0"'
        cmd = ["sh", "-c", script, f"{GIT_URL}#{commit}"]
    else:
        cmd.append(f"{GIT_URL}#{commit}")
    body = {
        "Image": BUILDER_IMAGE,
        "Cmd": cmd,
        "Env": env,
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
    logger.info("strata_build_started", version=version, commit=commit, then=then)
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
    current = await server_version(container)
    if current and version_tuple(current.get("version")) >= version_tuple(latest["version"]):
        raise StrataError(f"Уже установлена последняя версия {current.get('version')}")
    return await start_build(version=latest["version"], commit=latest["commit"], then="apply")


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
    return await start_build(version=latest["version"], commit=latest["commit"], then="create")


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
    return {
        "Image": image,
        "Env": _user_env(cfg.get("Env") or [], (old_image.get("Config") or {}).get("Env") or []),
        "Labels": labels,
        "ExposedPorts": cfg.get("ExposedPorts") or {"8080/tcp": {}},
        "Entrypoint": ["/bin/sh", "-c"],
        "Cmd": [ENTRYPOINT_SCRIPT],
        "StopTimeout": cfg.get("StopTimeout") or 60,
        "HostConfig": _memory(info.get("HostConfig") or {}, memory),
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
        "HOST=0.0.0.0",
        "PORT=8080",
        f"API_KEY={key}",
        "LOW_RAM=auto",
    ]
    host = {
        "Binds": [f"{volume}:/data"],
        "Runtime": "nvidia",
        "Ulimits": [{"Name": "memlock", "Soft": -1, "Hard": -1}],
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
        "Entrypoint": ["/bin/sh", "-c"],
        "Cmd": [ENTRYPOINT_SCRIPT],
        "StopTimeout": 60,
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
        "Entrypoint": ["/bin/sh", "-c"],
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

_DATA_ITEMS = ("models", "packs", "mtp", "config")
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
