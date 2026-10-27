"""Strata install/update/uninstall, several model families, the RAM reserve."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.ai import gpu_runtime
from app.ai.providers import strata_lifecycle, strata_manager

# ── The container's command ─────────────────────────────────────────────────


def test_compose_command_is_the_backends_script():
    """The backend creates the container from ENTRYPOINT_SCRIPT on install/update;
    compose keeps its own copy for `docker compose --profile strata`. A fix in one
    copy only would make the container behave differently after the first update."""
    compose = yaml.safe_load(
        (Path(__file__).parents[2] / "infra" / "docker-compose.yml").read_text()
    )
    command = compose["services"]["strata"]["command"][0].replace("$$", "$")
    assert command == strata_lifecycle.ENTRYPOINT_SCRIPT


def test_side_download_reads_its_own_env_file():
    script = strata_lifecycle.ENTRYPOINT_SCRIPT
    assert 'envfile="${AIW_ENV_FILE:-/data/aiw.env}"' in script
    assert '. "$envfile"' in script


# ── RAM reserve → expert placement ──────────────────────────────────────────


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(gpu_runtime, "set_strata_model", lambda name: None)
    return tmp_path


@pytest.mark.parametrize(
    ("reserve", "mode", "low_ram"),
    [
        (0, "off", "auto"),  # setup's own choice: 42.9 + 10 ≤ 59
        (4, "off", "off"),  # 55 GB left still holds every expert
        (12, "resident", "resident"),  # 47 GB: the GPU takes ~16 GB of experts
        (32, "mmap", "mmap"),  # 27 GB: the rest only through the file cache
    ],
)
def test_reserve_decides_the_expert_placement(data, reserve, mode, low_ram):
    plan = strata_manager.ram_plan(
        "IQ3_XXS", 262144, reserve_gb=reserve, total_gb=59.0, vram_gb=24.0
    )
    assert (plan["mode"], plan["low_ram"]) == (mode, low_ram)
    assert plan["limit_gb"] == (round(59.0 - reserve, 1) if reserve else None)


def test_reserve_for_an_unsloth_file_is_its_ram_budget(data):
    plan = strata_manager.ram_plan(
        "unsloth-UD-IQ4_XS", 65536, reserve_gb=8, total_gb=59.0, vram_gb=24.0
    )
    assert plan["mode"] == "budget"
    assert plan["resident_budget_gib"] == 27  # 51 GB − 24 GB beside the budget


def test_qwen36_has_no_placement_settings_only_a_fit(data):
    plan = strata_manager.ram_plan("qwen36-Q8_0", 65536, reserve_gb=12, total_gb=59.0, vram_gb=24.0)
    assert plan["mode"] == "own" and plan["low_ram"] == "auto"
    assert plan["fits"] is False  # 52 GB wanted, 47 left
    assert strata_manager.ram_plan(
        "qwen36-UD-Q4_K_XL", 65536, reserve_gb=12, total_gb=59.0, vram_gb=24.0
    )["fits"]


def test_reserve_reaches_setup_through_the_env_file(data):
    plan = strata_manager.ram_plan("IQ3_XXS", 262144, reserve_gb=12, total_gb=59.0, vram_gb=24.0)
    strata_manager.write_desired(
        model="IQ3_XXS", context=262144, vision=True, reinstall=True, ram=plan
    )
    text = (data / "aiw.env").read_text()
    assert "LOW_RAM=resident" in text and "REINSTALL=1" in text


def test_reserve_bigger_than_the_server_allows_is_refused(data, monkeypatch):
    monkeypatch.setattr(strata_manager, "ram_total_gb", lambda: 40.0)
    with pytest.raises(strata_manager.StrataError, match="меньше 24 ГБ"):
        strata_manager.write_runtime(ram_reserve_gb=24)
    strata_manager.write_runtime(ram_reserve_gb=12)
    assert strata_manager.read_runtime()["ram_reserve_gb"] == 12


def _config(data: Path, vid: str, args: list[str]) -> None:
    (data / "config").mkdir(exist_ok=True)
    (data / "config" / f"strata-{vid.lower()}.json").write_text(json.dumps({"args": args}))


def test_setup_pass_only_when_the_installed_placement_differs(data):
    off = {"mode": "off", "low_ram": "off"}
    resident = {"mode": "resident", "low_ram": "resident"}
    auto_off = {"mode": "off", "low_ram": "auto"}
    _config(data, "IQ3_XXS", ["--pack", "/p", "--native", "x"])
    assert not strata_manager.ram_setup_needed("IQ3_XXS", off)
    assert strata_manager.ram_setup_needed("IQ3_XXS", resident)
    assert not strata_manager.ram_setup_needed("IQ3_XXS", auto_off)
    # Left in the low-RAM mode by an earlier reserve, the model now fits whole.
    _config(data, "IQ3_XXS", ["--pack", "/p", "--resident-experts"])
    assert strata_manager.ram_setup_needed("IQ3_XXS", auto_off)
    assert not strata_manager.ram_setup_needed("IQ3_XXS", resident)


# ── The served model guard ──────────────────────────────────────────────────


def test_a_call_for_a_family_strata_does_not_hold_is_refused(monkeypatch):
    monkeypatch.setattr(gpu_runtime, "current_owner", lambda: "strata")
    monkeypatch.setattr(gpu_runtime, "strata_model", lambda: "qwen3.6-35b-a3b")
    gpu_runtime.check_call("strata", None, "qwen3.6-35b-a3b")
    with pytest.raises(gpu_runtime.GpuRuntimeUnavailable, match="загружена qwen3.6-35b-a3b"):
        gpu_runtime.check_call("strata", None, "qwen3.8-flash-next")
    # Unknown served model, or a call that names none: nothing to compare.
    gpu_runtime.check_call("strata", None, None)
    monkeypatch.setattr(gpu_runtime, "strata_model", lambda: None)
    gpu_runtime.check_call("strata", None, "qwen3.8-flash-next")


def test_every_family_has_a_catalog_entry_with_its_served_name():
    from app.ai.model_registry import ModelRegistry

    reg = ModelRegistry.from_yaml("app/ai/config/model_registry.yaml")
    for family in strata_manager.FAMILIES.values():
        cap = reg.models[family["catalog_key"]]
        assert cap.provider.value == "strata"
        assert cap.provider_model == family["served_name"]


# ── Versions ────────────────────────────────────────────────────────────────


def test_version_order():
    vt = strata_lifecycle.version_tuple
    assert vt("v0.1.45") > vt("0.1.44.1") > vt("0.1.44") > vt("0.1.43.1")
    assert vt(None) == ()


def test_image_env_is_not_carried_to_the_new_image():
    image = [
        "PATH=/usr/local/cuda/bin:/usr/bin",
        "CUDA_VERSION=13.0.0",
        "NVIDIA_VISIBLE_DEVICES=all",
        "STRATA_EXECV=1",
    ]
    container = [*image, "MODEL=IQ2_XS", "API_KEY=k"]
    assert strata_lifecycle._user_env(container, image) == [
        "NVIDIA_VISIBLE_DEVICES=all",
        "MODEL=IQ2_XS",
        "API_KEY=k",
    ]


# ── Recreate (update / memory limit) ────────────────────────────────────────


class FakeDocker:
    """Records Docker Engine calls; one server container 'old'."""

    def __init__(self, *, fail_create: bool = False, running: bool = True):
        self.calls: list[tuple[str, str]] = []
        self.created: list[dict] = []
        self.fail_create = fail_create
        self.running = running
        self.images = {
            "ai-workspace/strata:a0116b2": {
                "Id": "sha256:old",
                "Config": {"Env": ["PATH=/old", "CUDA_VERSION=12"], "Labels": {}},
            },
            "ai-workspace/strata:477ac13": {
                "Id": "sha256:new",
                "Config": {"Env": ["PATH=/new", "CUDA_VERSION=13"], "Labels": {}},
            },
        }
        self.server = {
            "Id": "old" * 8,
            "Name": "/infra-strata-1",
            "Image": "sha256:old",
            "State": {"Running": running},
            "Config": {
                "Image": "ai-workspace/strata:a0116b2",
                "Env": ["PATH=/old", "CUDA_VERSION=12", "MODEL=IQ2_XS", "API_KEY=k"],
                "Labels": {
                    "com.docker.compose.project": "infra",
                    "com.docker.compose.service": "strata",
                    "maintainer": "NVIDIA CORPORATION",
                },
                "ExposedPorts": {"8080/tcp": {}},
            },
            "HostConfig": {"Runtime": "nvidia", "Memory": 0, "PortBindings": {"8080/tcp": []}},
            "NetworkSettings": {
                "Networks": {"infra_app": {"Aliases": ["infra-strata-1", "strata", "oldoldoldold"]}}
            },
        }

    async def get(self, path, params=None):
        self.calls.append(("GET", path))
        if path.startswith("/images/"):
            ref = path.removeprefix("/images/").removesuffix("/json")
            img = self.images.get(ref) or next(
                (i for i in self.images.values() if i["Id"] == ref), None
            )
            return SimpleNamespace(status_code=200 if img else 404, json=lambda: img, text="")
        return SimpleNamespace(status_code=200, json=lambda: self.server, text="")

    async def post(self, path, params=None, json=None, timeout=None):
        self.calls.append(("POST", path))
        if path == "/containers/create":
            if self.fail_create:
                return SimpleNamespace(status_code=500, text="no nvidia runtime", json=lambda: {})
            self.created.append(json)
            return SimpleNamespace(status_code=201, json=lambda: {"Id": "new" * 8}, text="")
        return SimpleNamespace(status_code=204, json=lambda: {}, text="")

    async def delete(self, path, params=None):
        self.calls.append(("DELETE", path))
        return SimpleNamespace(status_code=204, text="")


@pytest.fixture
def docker(monkeypatch, data):
    def install(fake: FakeDocker) -> FakeDocker:
        class Client:
            async def __aenter__(self):
                return fake

            async def __aexit__(self, *_a):
                return None

        async def find():
            return {"Id": fake.server["Id"], "State": "running" if fake.running else "exited"}

        actions: list[str] = []

        async def action(name, timeout=30):
            actions.append(name)

        fake.actions = actions
        monkeypatch.setattr(strata_manager, "_docker", lambda: Client())
        monkeypatch.setattr(strata_manager, "_find_container", find)
        monkeypatch.setattr(strata_manager, "_container_action", action)
        return fake

    return install


def test_update_recreates_the_server_on_the_new_image(docker):
    fake = docker(FakeDocker())
    asyncio.run(
        strata_lifecycle.recreate_server(image="ai-workspace/strata:477ac13", memory=12 * 2**30)
    )
    spec = fake.created[0]
    assert spec["Image"] == "ai-workspace/strata:477ac13"
    # The image's own env stays the new image's; the container's settings are kept.
    assert spec["Env"] == ["MODEL=IQ2_XS", "API_KEY=k"]
    assert spec["Labels"]["com.docker.compose.service"] == "strata"
    assert spec["Labels"]["com.docker.compose.image"] == "sha256:new"
    assert "maintainer" not in spec["Labels"]
    assert spec["Cmd"] == [strata_lifecycle.ENTRYPOINT_SCRIPT]
    assert spec["HostConfig"]["Memory"] == spec["HostConfig"]["MemorySwap"] == 12 * 2**30
    assert spec["HostConfig"]["Runtime"] == "nvidia"
    assert spec["NetworkingConfig"]["EndpointsConfig"]["infra_app"]["Aliases"] == [
        "infra-strata-1",
        "strata",
    ]
    order = [c for c in fake.calls if c[0] != "GET"]
    assert fake.actions == ["stop"]
    assert order[0][1].endswith("/rename") and order[1] == ("POST", "/containers/create")
    assert order[2] == ("POST", f"/containers/{'new' * 8}/start")  # it was running
    assert order[3] == ("DELETE", f"/containers/{'old' * 8}")


def test_failed_recreate_puts_the_old_container_back(docker):
    fake = docker(FakeDocker(fail_create=True))
    with pytest.raises(strata_manager.StrataError, match="no nvidia runtime"):
        asyncio.run(strata_lifecycle.recreate_server(image="ai-workspace/strata:477ac13"))
    posts = [c[1] for c in fake.calls if c[0] == "POST"]
    old = "old" * 8
    assert posts == [
        f"/containers/{old}/rename",
        "/containers/create",
        f"/containers/{old}/rename",
        f"/containers/{old}/start",
    ]
    assert not any(c[0] == "DELETE" for c in fake.calls)


def test_stopped_server_stays_stopped_after_an_update(docker):
    fake = docker(FakeDocker(running=False))
    asyncio.run(strata_lifecycle.recreate_server(image="ai-workspace/strata:477ac13"))
    assert fake.actions == []
    assert not any(c[1].endswith("/start") for c in fake.calls)


# ── Loading another family moves the slots along ────────────────────────────


def test_loading_another_family_moves_strata_slots_and_tasks(monkeypatch):
    from app.ai.schemas import AITask
    from app.api import providers_api as pa
    from app.api import strata_api

    flash = "strata_qwen3_8_flash_next"
    state = {"agent_orchestrator": flash, "invoice_extraction": "ollama_qwen", "ocr_fast": flash}
    monkeypatch.setattr(pa, "_registry", lambda: None)
    monkeypatch.setattr(pa, "_slot_current_model", lambda slot, _r: state.get(slot))
    monkeypatch.setattr(strata_api, "_slot_covered_tasks", lambda: {"invoice_ocr"})
    routing = {
        AITask.ENGINEERING_REASONING: SimpleNamespace(primary=flash),
        AITask.CLASSIFICATION: SimpleNamespace(primary="ollama_qwen"),
    }
    monkeypatch.setattr("app.ai.task_routing.get_task_routing", lambda: routing)

    slots, tasks, items = strata_api._follow_plan("strata_qwen3_6_35b")
    assert slots == {"agent_orchestrator": "strata_qwen3_6_35b", "ocr_fast": "strata_qwen3_6_35b"}
    assert tasks == ["engineering_reasoning"]
    assert {i.slot for i in items} == {
        "agent_orchestrator",
        "ocr_fast",
        "task:engineering_reasoning",
    }
    # The family Strata already holds: nothing to move.
    assert strata_api._follow_plan(flash)[:2] == ({}, [])


def test_qwen36_needs_a_strata_that_knows_it():
    """0.1.43.1's setup has no qwen36 family: a download would fail at setup."""
    with pytest.raises(strata_manager.StrataError, match="0.1.45 и новее"):
        strata_manager.check_version("qwen36-UD-Q4_K_XL", "0.1.43.1")
    strata_manager.check_version("qwen36-UD-Q4_K_XL", "0.1.45")
    strata_manager.check_version("IQ3_XXS", "0.1.43.1")
    strata_manager.check_version("qwen36-UD-Q4_K_XL", None)  # unknown: setup decides


def test_version_of_an_unlabelled_image_comes_from_its_build_json(monkeypatch):
    """Images built before the labels (a0116b2 = 0.1.43.1): the engine's own BUILD.json.

    Live, the archive query's ``path`` parameter collided with the helper's
    own argument name and the whole update block failed.
    """
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        data = json.dumps({"version": "0.1.43.1"}).encode()
        info = tarfile.TarInfo("BUILD.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    seen = {}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def get(self, path, params=None):
            seen[path] = params
            if path.endswith("/archive"):
                return SimpleNamespace(status_code=200, content=buf.getvalue())
            return SimpleNamespace(status_code=200, json=lambda: {"Config": {"Labels": {}}})

    monkeypatch.setattr(strata_manager, "_docker", lambda: Client())
    monkeypatch.setattr(
        strata_lifecycle,
        "_redis",
        lambda: SimpleNamespace(get=lambda _k: None, set=lambda *_a, **_k: None),
    )
    out = asyncio.run(
        strata_lifecycle.server_version(
            {"Id": "c1", "ImageID": "sha256:x", "Image": "ai-workspace/strata:a0116b2"}
        )
    )
    assert out["version"] == "0.1.43.1" and out["commit"] == "a0116b2"
    assert seen["/containers/c1/archive"] == {"path": "/opt/strata/engine/BUILD.json"}


def test_qwen36_sessions_must_fit_the_card(data):
    """Live 2026-10-18: Qwen3.6 set up at 256K with 2 parallel sessions wanted
    2 × 11 GiB of VRAM, 13.7 free — the engine refused, the container exited."""
    vid = "qwen36-UD-Q4_K_XL"
    with pytest.raises(strata_manager.StrataError, match="контекст не больше 128K"):
        strata_manager.check_vram_fit(vid, 262144, parallel=2, vram_gb=24.0)
    strata_manager.check_vram_fit(vid, 65536, parallel=2, vram_gb=24.0)
    strata_manager.check_vram_fit(vid, 262144, parallel=1, vram_gb=24.0)
    # Flash-Next's 8-bit KV is not this module's concern.
    strata_manager.check_vram_fit("IQ3_XXS", 262144, parallel=2, vram_gb=24.0)
    assert strata_manager.recommended_context(vid, 24.0) == 65536
    assert strata_manager.recommended_context("IQ3_XXS", 24.0) is None
    # The setting that would break the loaded model is refused before it is saved.
    with pytest.raises(strata_manager.StrataError):
        strata_manager.write_desired(model=vid, context=262144, vision=True, reinstall=False)


# ── Update: the release's ready image is pulled, not built ────────────────


def _build_harness(monkeypatch):
    created: list[dict] = []

    async def none(*_a, **_k):
        return None

    async def post(path, /, *, json_body=None, **_params):
        if path == "/containers/create":
            created.append(json_body)
            return SimpleNamespace(status_code=201, json=lambda: {"Id": "b1"}, text="")
        return SimpleNamespace(status_code=204, json=lambda: {}, text="")

    async def me():
        return {}

    monkeypatch.setattr(strata_lifecycle, "_by_role", none)
    monkeypatch.setattr(strata_lifecycle, "_image", none)
    monkeypatch.setattr(strata_lifecycle, "_ensure_builder_image", none)
    monkeypatch.setattr(strata_lifecycle, "_clear_job_error", lambda: None)
    monkeypatch.setattr(strata_lifecycle, "_self", me)
    monkeypatch.setattr(strata_lifecycle, "_post", post)
    monkeypatch.setattr(strata_lifecycle, "watch", lambda: None)
    return created


def test_update_pulls_the_ready_image_and_keeps_our_name(monkeypatch):
    """0.2.1 publishes images to ghcr: the update is a pull (minutes), not an
    hour of CUDA compile; uninstall and :current work on ai-workspace/strata."""
    monkeypatch.setattr(strata_lifecycle.settings, "strata_image_flavor", "cuda13")
    created = _build_harness(monkeypatch)
    out = asyncio.run(strata_lifecycle.start_build(version="0.2.1", commit="a" * 40, then="apply"))
    assert out == {"image": "ai-workspace/strata:0.2.1", "built": False}
    body = created[0]
    sh, flag, script, source, target = body["Cmd"]
    assert (sh, flag) == ("sh", "-c")
    assert script.startswith('docker pull "$0" && docker tag "$0" "$1"')
    assert source == "ghcr.io/niko1221/strata:0.2.1-cuda13"
    assert target == "ai-workspace/strata:0.2.1"
    assert body["Labels"][strata_lifecycle.LABEL_TARGET_IMAGE] == target


def test_a_release_without_a_ready_image_is_not_fetched(monkeypatch):
    created = _build_harness(monkeypatch)
    with pytest.raises(strata_manager.StrataError, match="0.2.1"):
        asyncio.run(strata_lifecycle.start_build(version="0.1.45", commit="b" * 40, then="apply"))
    assert created == []


def test_an_image_with_another_entrypoint_is_not_applied(monkeypatch):
    """0.1.x started through ./docker-entrypoint.sh: the 0.2 launch script on it
    (or on a later layout) would exit at once — the server is left as it is."""
    calls: list[str] = []

    async def image(_ref):
        return {
            "Config": {
                "Entrypoint": ["./docker-entrypoint.sh"],
                "Labels": {strata_lifecycle.OCI_VERSION_LABEL: "0.1.45"},
            }
        }

    async def post(path, /, **_kw):
        calls.append(path)

    monkeypatch.setattr(strata_lifecycle, "_image", image)
    monkeypatch.setattr(strata_lifecycle, "_post", post)
    with pytest.raises(strata_manager.StrataError, match="0.1.45"):
        asyncio.run(strata_lifecycle._finish("ai-workspace/strata:477ac13", "apply"))
    assert calls == []  # :current is not moved, the server is not touched


def test_update_info_does_not_offer_a_release_without_a_ready_image(monkeypatch):
    async def container():
        return {"Id": "s1"}

    async def current(_c=None):
        return {"version": "0.1.44"}

    async def latest(**_kw):
        return {"version": "0.1.45", "commit": "e" * 40, "supported": False}

    async def no_job():
        return None

    monkeypatch.setattr(strata_manager, "_find_container", container)
    monkeypatch.setattr(strata_lifecycle, "server_version", current)
    monkeypatch.setattr(strata_lifecycle, "latest_release", latest)
    monkeypatch.setattr(strata_lifecycle, "job_status", no_job)
    info = asyncio.run(strata_lifecycle.update_info())
    assert info["update_available"] is False
    assert "0.1.45" in info["update_blocked"]


def test_the_version_of_a_published_image_comes_from_its_oci_label(monkeypatch):
    async def image(_ref):
        return {"Config": {"Labels": {strata_lifecycle.OCI_VERSION_LABEL: "0.2.1"}}}

    monkeypatch.setattr(strata_lifecycle, "_image", image)
    out = asyncio.run(
        strata_lifecycle.server_version(
            {"Id": "c1", "ImageID": "sha256:x", "Image": "ai-workspace/strata:0.2.1"}
        )
    )
    assert out["version"] == "0.2.1"


# ── The launch script (Strata 0.2) ──────────────────────────────────────────


def test_launch_script_moves_the_old_layout_and_ends_in_strata_launch():
    script = strata_lifecycle.ENTRYPOINT_SCRIPT
    # 0.1.x files move under Strata-data once, a link stays for the old image.
    assert 'mv "/data/$d" "/data/Strata-data/$d" && ln -s "Strata-data/$d" "/data/$d"' in script
    # A variant without a run config is set up first, so the residency keys
    # are in the config before the first serve.
    assert 'setup_pass "$@" || exit 1' in script
    assert "STRATA_CONTROL_INSTALL=1" in script and 'setup.py "$@"' in script
    assert 'if [ "${INSTALL_ONLY:-0}" = "1" ]; then' in script  # side download
    assert "--data-dir /data/Strata-data" in script
    assert script.rstrip().endswith("exec /opt/strata/entrypoint.sh run")
    # Strata's own private parts stay owner-only; no recursive grant over /data.
    assert "chmod -R go-rwx /data/state /data/home /data/config/strata" in script
    assert "chmod -R g+rwX /data " not in script and 'chgrp -R "${STRATA_SHARED_GID' not in script
    # Settings the 0.2 entrypoint would warn about are not passed on.
    assert "unset LOW_RAM RESIDENT_BUDGET_GIB REINSTALL HOST" in script


def test_server_spec_runs_the_script_under_tini_without_legacy_env(monkeypatch):
    async def image(ref):
        return {"Id": "sha256:new", "Config": {"Env": ["PATH=/usr/bin"]}}

    monkeypatch.setattr(strata_lifecycle, "_image", image)
    info = {
        "Id": "abc",
        "Image": "old",
        "Config": {
            "Env": ["MODEL=IQ2_XS", "HOST=0.0.0.0", "LOW_RAM=auto", "API_KEY=k", "PATH=/usr/bin"],
            "Labels": {},
        },
        "HostConfig": {"ShmSize": 67108864},
        "NetworkSettings": {"Networks": {}},
    }
    spec = asyncio.run(
        strata_lifecycle._spec_from(info, image="ai-workspace/strata:0.2.1", memory=0)
    )
    assert spec["Entrypoint"] == ["/usr/bin/tini", "--", "/bin/sh", "-c"]
    assert "HOST=0.0.0.0" not in spec["Env"] and "LOW_RAM=auto" not in spec["Env"]
    assert "MODEL=IQ2_XS" in spec["Env"] and "API_KEY=k" in spec["Env"]
    assert spec["HostConfig"]["ShmSize"] == 2 * 1024**3
    assert spec["StopTimeout"] == 120


# ── A release is not an image: install what ghcr actually has ──────────────


def _update_harness(monkeypatch, *, current, latest):
    async def container():
        return {"Id": "s1"}

    async def cur(_c=None):
        return {"version": current}

    async def rel(**_kw):
        return latest

    async def no_job():
        return None

    monkeypatch.setattr(strata_manager, "_find_container", container)
    monkeypatch.setattr(strata_lifecycle, "server_version", cur)
    monkeypatch.setattr(strata_lifecycle, "latest_release", rel)
    monkeypatch.setattr(strata_lifecycle, "job_status", no_job)


def test_newest_image_version_is_not_the_release_number():
    """0.2.4-0.2.6 came out with no image (newest tag 0.2.3): the panel pulled
    0.2.6-cuda13 and got "manifest unknown" (live, 2026-10-26)."""
    published = ["0.2.1", "0.2.3", "0.2.2"]
    assert strata_lifecycle.newest_image_version(published, "0.2.6") == "0.2.3"
    assert strata_lifecycle.newest_image_version(published, "0.2.2") == "0.2.2"
    assert strata_lifecycle.newest_image_version([], "0.2.6") is None


def test_published_versions_read_the_flavor_tags(monkeypatch):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "t"})
        assert request.headers["Authorization"] == "Bearer t"
        tags = ["0.2.1-cuda13", "0.2.3-cuda13", "0.2.3-cuda12", "latest-cuda13", "0.2.2-sycl"]
        return httpx.Response(200, json={"tags": tags})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await strata_lifecycle.published_versions(client, "cuda13")

    assert asyncio.run(run()) == ["0.2.1", "0.2.3"]


def test_update_offers_the_newest_published_image(monkeypatch):
    _update_harness(
        monkeypatch,
        current="0.2.1",
        latest={
            "version": "0.2.6",
            "image_version": "0.2.3",
            "supported": True,
            "flavor": "cuda13",
        },
    )
    info = asyncio.run(strata_lifecycle.update_info())
    assert info["update_available"] is True
    assert "0.2.6" in info["update_blocked"] and "0.2.3" in info["update_blocked"]


def test_no_update_while_the_new_release_has_no_image(monkeypatch):
    _update_harness(
        monkeypatch,
        current="0.2.3",
        latest={
            "version": "0.2.6",
            "image_version": "0.2.3",
            "supported": True,
            "flavor": "cuda13",
        },
    )
    info = asyncio.run(strata_lifecycle.update_info())
    assert info["update_available"] is False
    assert "пока нет" in info["update_blocked"]
    with pytest.raises(strata_manager.StrataError, match="пока нет"):
        asyncio.run(strata_lifecycle.start_update())


def test_update_pulls_the_published_version(monkeypatch):
    _update_harness(
        monkeypatch,
        current="0.2.1",
        latest={
            "version": "0.2.6",
            "commit": "f" * 40,
            "image_version": "0.2.3",
            "image_commit": "c" * 40,
            "supported": True,
        },
    )
    seen: dict = {}

    async def build(**kw):
        seen.update(kw)
        return {"image": "x", "built": False}

    monkeypatch.setattr(strata_lifecycle, "start_build", build)
    asyncio.run(strata_lifecycle.start_update())
    assert seen == {"version": "0.2.3", "commit": "c" * 40, "then": "apply"}
