import pytest

from app.ai.cad_ir.feature_tree import Feature3D, FeatureTreeCandidate, ParamProvenance
from app.config import settings
from app.tasks import cad_trace


def test_emg_rollout_is_limited_to_explicit_profiles(monkeypatch):
    monkeypatch.setattr(settings, "emg_pipeline_enabled", True)
    monkeypatch.setattr(settings, "emg_pipeline_profiles", "mechanical,assembly")

    assert settings.emg_enabled_for("mechanical")
    assert settings.emg_enabled_for("mechanical_eskd")
    assert settings.emg_enabled_for("assembly")
    assert not settings.emg_enabled_for("construction")
    assert not settings.emg_enabled_for("auto")


@pytest.mark.asyncio
async def test_emg_pipeline_compiles_kernel_candidate_back_from_sealed_graph(monkeypatch):
    """Ф2.6: with EMG enabled, the kernel candidate must be re-derived from the
    sealed graph revision's BuildOperation nodes, not used straight off
    ``feature_tree_from_spec``. This is the production default (see
    ``infra/.env``: ``EMG_PIPELINE_ENABLED=true``), not an opt-in canary."""
    candidate = FeatureTreeCandidate(
        features=[
            Feature3D(
                kind="extrude",
                params={"depth_mm": 12.0, "width_mm": 40.0, "height_mm": 20.0},
                param_provenance={
                    "depth_mm": ParamProvenance(origin="stated", detail="dimension"),
                    "width_mm": ParamProvenance(origin="measured", detail="profile"),
                    "height_mm": ParamProvenance(origin="measured", detail="profile"),
                },
                confidence=0.9,
            )
        ],
        score=0.9,
        label="legacy candidate",
    )

    async def record(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings, "emg_pipeline_enabled", True)
    monkeypatch.setattr("app.ai.cad_solid.feature_tree_from_spec", lambda _spec: candidate)
    monkeypatch.setattr(
        "app.ai.cad_solid.solid_build_gate",
        lambda *_args, **_kwargs: {
            "allowed": False,
            "blockers": ["dimension chain unresolved"],
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        "app.ai.cad_solid.solid_preview_gate",
        lambda _gate: {
            "allowed": False,
            "hard_blockers": ["dimension chain unresolved"],
            "excluded": [],
        },
    )
    monkeypatch.setattr("app.ai.cad_process_log.record_cad_process_event", record)

    result = await cad_trace._build_spec_solid(
        {"part": "test", "main_view": {"type": "plate"}},
        "generation-1",
        "owner",
        source_sha256="a" * 64,
    )

    assert result is not None
    graph = result["_engineering_model_graph"]
    assert graph.canonical_sha256 == graph.calculated_sha256()
    assert graph.graph_id == "image-generation:generation-1"
    assert result["feature_tree"]["features"][0]["params"] == candidate.features[0].params
    assert result["build_status"] == "blocked"
    assert result["graph_admission"]["allowed"] is True
    assert result["graph_admission"]["advisory_verification_issue_codes"] == [
        "domain_mandatory_assertion_missing"
    ]
    assert "drawing_scale_not_available" in result["graph_admission"]["verification_issue_codes"]


@pytest.mark.asyncio
async def test_mechanical_kernel_is_not_called_when_graph_hash_is_invalid(monkeypatch):
    from app.ai.cad_emg_compat import spec_feature_tree_as_graph

    candidate = FeatureTreeCandidate(
        features=[
            Feature3D(
                kind="extrude",
                params={"depth_mm": 12.0, "width_mm": 40.0, "height_mm": 20.0},
                param_provenance={
                    "depth_mm": ParamProvenance(origin="stated", detail="dimension"),
                    "width_mm": ParamProvenance(origin="measured", detail="profile"),
                    "height_mm": ParamProvenance(origin="measured", detail="profile"),
                },
                confidence=0.9,
            )
        ],
        score=0.9,
        label="tampered graph candidate",
    )
    spec = {"part": "test", "main_view": {"type": "plate"}}
    graph = spec_feature_tree_as_graph(
        spec,
        candidate,
        graph_id="image-generation:tampered",
        source_sha256="a" * 64,
    ).model_copy(update={"canonical_sha256": "0" * 64})

    async def record(*_args, **_kwargs):
        return None

    async def forbidden_compile(*_args, **_kwargs):
        raise AssertionError("kernel must not be called for a rejected graph")

    monkeypatch.setattr(
        "app.ai.cad_solid.solid_build_gate",
        lambda *_args, **_kwargs: {"allowed": True, "blockers": [], "warnings": []},
    )
    monkeypatch.setattr(
        "app.ai.cad_solid.solid_preview_gate",
        lambda gate: {
            "allowed": False,
            "hard_blockers": list(gate["blockers"]),
            "excluded": [],
        },
    )
    monkeypatch.setattr("app.ai.cad_process_log.record_cad_process_event", record)
    monkeypatch.setattr("app.services.cad_kernel.compile_candidate", forbidden_compile)

    result = await cad_trace._build_spec_solid(
        spec,
        "tampered",
        "owner",
        engineering_graph_override=graph,
    )

    assert result is not None
    assert result["built"] is False
    assert result["graph_admission"]["allowed"] is False
    assert "verification_canonical_hash_mismatch" in {
        item["code"] for item in result["graph_admission"]["blockers"]
    }


@pytest.mark.asyncio
async def test_spec_rebuild_uses_available_session_factory(monkeypatch):
    class MissingGenerationSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args):
            return None

    monkeypatch.setattr(
        "app.db.session._get_session_factory",
        lambda: lambda: MissingGenerationSession(),
    )

    result = await cad_trace._rebuild_from_spec("00000000-0000-0000-0000-000000000001")

    assert result == {"error": "not found"}


@pytest.mark.asyncio
async def test_spec_rebuild_drops_a_superseded_correction_before_build(monkeypatch):
    class Generation:
        owner_sub = "owner"
        params = {
            "spec": {"part": "test"},
            "spec_corrected": {"part": "newest"},
            "spec_correction_event_id": "event-newest",
        }

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args):
            return Generation()

    monkeypatch.setattr(
        "app.db.session._get_session_factory",
        lambda: lambda: Session(),
    )

    result = await cad_trace._rebuild_from_spec(
        "00000000-0000-0000-0000-000000000001",
        "event-stale",
    )

    assert result == {
        "ok": True,
        "superseded": True,
        "generation_id": "00000000-0000-0000-0000-000000000001",
        "correction_event_id": "event-stale",
    }
