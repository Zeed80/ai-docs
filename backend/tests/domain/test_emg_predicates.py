"""EMG predicate registry: self-consistency + live cross-check against the
four domain builders and the legacy-spec compatibility layer.

The registry (``app.domain.emg_predicates``) is additive documentation and a
drift-prevention aid, not a runtime gate on ``EngineeringModelGraph`` itself
(a human correction or a domain extension may legitimately introduce a
predicate it has never seen). What this test protects is narrower and more
valuable: that the CURATED vocabulary the four builders actually emit stays
exactly what the registry says it is — no builder quietly renaming
``"assembly.interference_clear"`` to ``"assembly.interference_ok"`` while the
registry (and the other three builders) still say the original spelling.
"""

from __future__ import annotations

import pytest

from app.ai.assembly_emg import assembly_as_graph
from app.ai.cad_emg_compat import legacy_spec_as_low_assurance
from app.ai.construction_emg import ConstructionModel, construction_as_graph
from app.ai.system_emg import EngineeringSystemModel, system_as_graph
from app.domain.assembly import analyze_assembly_dof
from app.domain.emg_predicates import (
    PREDICATE,
    PREDICATES,
    TEMPLATED_PREDICATES,
    classify_predicate,
    is_known_predicate,
)
from app.domain.engineering_model_graph import DOMAIN_ADAPTERS

# ── Registry self-consistency ───────────────────────────────────────────────


def test_registry_has_no_duplicate_keys_across_families():
    # _predicates() already raises at import time on a duplicate within one
    # family; this additionally checks the two families never collide.
    assert not set(PREDICATES) & set(TEMPLATED_PREDICATES)


def test_every_entry_has_a_description_and_at_least_one_value_kind():
    for key, item in {**PREDICATES, **TEMPLATED_PREDICATES}.items():
        assert item.description.strip(), key
        assert item.value_kinds, key
        assert item.subject_node_types, key


def test_predicate_constants_round_trip_to_their_own_key():
    for key, item in PREDICATES.items():
        attr = key.upper().replace(".", "_").rstrip("_")
        assert getattr(PREDICATE, attr) == key


@pytest.mark.parametrize(
    ("predicate", "subject_id", "expected"),
    [
        (PREDICATE.ASSEMBLY_INTERFERENCE_CLEAR, "product:assembly", "registered"),
        ("operation.param.length_mm", "operation:0", "templated"),
        ("build.unresolved.3", "product:legacy-spec", "templated"),
        ("main_view.outer[0].diameter_mm", "product:legacy-spec", "legacy_passthrough"),
        ("main_view.outer[0].diameter_mm", "product:assembly", "unregistered"),
        ("assembly.interference_ok", "product:assembly", "unregistered"),
    ],
)
def test_classify_predicate(predicate, subject_id, expected):
    assert classify_predicate(predicate, subject_id) == expected
    assert is_known_predicate(predicate, subject_id) == (expected != "unregistered")


def test_domain_adapter_mandatory_assertions_are_all_registered():
    """DOMAIN_ADAPTERS lives in engineering_model_graph.py itself (it cannot
    import the registry without cycling back through it) and hand-lists a
    few predicate names of its own. Catch the two lists drifting apart."""
    for profile, adapter in DOMAIN_ADAPTERS.items():
        for predicate in adapter.mandatory_assertions:
            assert predicate in PREDICATES, (profile, predicate)


# ── Live cross-check: every assertion a real builder emits is known ────────


def _unregistered(graph) -> list[tuple[str, str]]:
    return sorted(
        (item.predicate, item.subject_id)
        for item in graph.assertions
        if classify_predicate(item.predicate, item.subject_id) == "unregistered"
    )


def test_assembly_builder_only_emits_registered_predicates():
    components = [
        {"instance_key": "housing", "designation": "Housing", "quantity": 1,
         "metadata_": {"grounded": True}, "transform": {}},
        {"instance_key": "shaft", "designation": "Shaft", "quantity": 1,
         "metadata_": {}, "transform": {"x": 12.0}},
    ]
    mates = [{
        "id": "mate-1", "mate_type": "fixed",
        "first_instance_key": "housing", "second_instance_key": "shaft",
        "parameters": {},
    }]
    graph = assembly_as_graph(
        graph_id="assembly:predicate-check",
        name="Predicate-check assembly",
        designation="ASM-PRED-001",
        components=components,
        mates=mates,
        dof=analyze_assembly_dof(components, mates),
        collisions=[],
        exact_checked=["housing", "shaft"],
        interference_degraded=None,
    )
    assert _unregistered(graph) == []


def test_construction_builder_only_emits_registered_predicates():
    model = ConstructionModel.model_validate({
        "site_name": "Predicate-check site",
        "building_name": "Predicate-check building",
        "storeys": [{"id": "level-1", "name": "Level 1", "elevation_mm": 0}],
        "elements": [{
            "id": "wall-1", "kind": "wall", "name": "External wall",
            "storey_id": "level-1", "material": "Concrete C30/37", "load_bearing": True,
            "box": {"x_mm": 0, "y_mm": 0, "z_mm": 0,
                    "width_mm": 5000, "depth_mm": 200, "height_mm": 3000},
        }],
    })
    graph = construction_as_graph(
        graph_id="construction:predicate-check",
        model=model,
        source_revision_id="rev-1",
        source_approved=True,
    )
    assert _unregistered(graph) == []


def test_system_builder_only_emits_registered_predicates():
    model = EngineeringSystemModel.model_validate({
        "profile": "mep",
        "name": "Predicate-check system",
        "system_kind": "supply_air",
        "equipment": [{"id": "ahu-1", "name": "AHU 1", "equipment_type": "air_handling_unit"}],
        "ports": [{
            "id": "port-1", "equipment_id": "ahu-1", "kind": "supply",
            "direction": "out", "medium": "air", "nominal_size_mm": 200,
        }],
        "connections": [],
    })
    graph = system_as_graph(
        graph_id="system:predicate-check",
        model=model,
        source_revision_id="rev-1",
        source_approved=True,
    )
    assert _unregistered(graph) == []


def test_legacy_spec_passthrough_predicates_are_not_flagged_as_unregistered():
    """The legacy compat layer's predicates are the spec's own dotted JSON
    paths (an intentionally open namespace) — classify_predicate must exempt
    them via subject_id, not require them in the registry."""
    graph = legacy_spec_as_low_assurance(
        {"main_view": {"outer": [{"diameter_mm": 30}]}, "part": "Test shaft"},
        graph_id="mechanical:predicate-check",
    )
    assert _unregistered(graph) == []
    predicates = {item.predicate for item in graph.assertions}
    assert "main_view.outer[0].diameter_mm" in predicates
    assert classify_predicate(
        "main_view.outer[0].diameter_mm", "product:legacy-spec",
    ) == "legacy_passthrough"
