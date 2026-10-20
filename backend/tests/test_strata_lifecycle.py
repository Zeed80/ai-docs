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


# ── Build: the commit reaches package_terminal.py ───────────────────────────

_TERMINAL_STAGE = """FROM ${RUNTIME_BASE} AS terminal
COPY . /src/
RUN python3 /src/packaging/package_terminal.py --out /out --cache /cache \\
 && /opt/strata/terminal/bin/strata --version
"""


def test_dockerfile_patch_passes_the_commit_and_a_git_shim():
    """v0.2.1 asks `git rev-parse HEAD` in a stage with no git; later sources
    read STRATA_BUILD_COMMIT. The patch serves both, only for that RUN."""
    out = strata_lifecycle.patch_dockerfile(_TERMINAL_STAGE)
    assert out is not None
    assert "ARG STRATA_COMMIT=\nENV STRATA_BUILD_COMMIT=${STRATA_COMMIT}\n" in out
    assert "RUN PATH=/opt/aiw-git:$PATH python3 /src/packaging/package_terminal.py" in out
    assert out.index("ENV STRATA_BUILD_COMMIT") < out.index("package_terminal.py")
    assert strata_lifecycle.patch_dockerfile("FROM ubuntu\nRUN true\n") is None


def _build_harness(monkeypatch, dockerfile):
    created: list[dict] = []

    async def none(*_a, **_k):
        return None

    async def post(path, /, *, json_body=None, **_params):
        if path == "/containers/create":
            created.append(json_body)
            return SimpleNamespace(status_code=201, json=lambda: {"Id": "b1"}, text="")
        return SimpleNamespace(status_code=204, json=lambda: {}, text="")

    async def fetch(_commit):
        return dockerfile

    async def me():
        return {}

    monkeypatch.setattr(strata_lifecycle, "_by_role", none)
    monkeypatch.setattr(strata_lifecycle, "_image", none)
    monkeypatch.setattr(strata_lifecycle, "_ensure_builder_image", none)
    monkeypatch.setattr(strata_lifecycle, "_clear_job_error", lambda: None)
    monkeypatch.setattr(strata_lifecycle, "_self", me)
    monkeypatch.setattr(strata_lifecycle, "_post", post)
    monkeypatch.setattr(strata_lifecycle, "_fetch_dockerfile", fetch)
    return created


def test_build_feeds_the_patched_dockerfile_through_stdin(monkeypatch):
    created = _build_harness(monkeypatch, _TERMINAL_STAGE)
    commit = "a" * 40
    asyncio.run(strata_lifecycle.start_build(version="0.2.1", commit=commit, then="prepare"))
    body = created[0]
    sh, flag, script, context = body["Cmd"]
    assert (sh, flag) == ("sh", "-c")
    assert script.startswith('printf "%s" "$STRATA_DOCKERFILE" | docker build ')
    assert script.endswith(' -f - "$0"')
    assert f"STRATA_COMMIT={commit}" in script
    assert context == f"{strata_lifecycle.GIT_URL}#{commit}"
    env = dict(e.split("=", 1) for e in body["Env"])
    assert "PATH=/opt/aiw-git:$PATH" in env["STRATA_DOCKERFILE"]


def test_build_without_the_step_uses_the_repo_dockerfile(monkeypatch):
    created = _build_harness(monkeypatch, None)
    commit = "b" * 40
    asyncio.run(strata_lifecycle.start_build(version="0.2.2", commit=commit, then="prepare"))
    cmd = created[0]["Cmd"]
    assert cmd[:2] == ["docker", "build"]
    assert cmd[-1] == f"{strata_lifecycle.GIT_URL}#{commit}"
    assert created[0]["Env"] == []
