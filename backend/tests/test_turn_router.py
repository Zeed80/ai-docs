"""TurnRouter — deterministic logic (Phase 3).

Routing-quality (does the LLM classify "расчёт себестоимости" as NOT a count
question?) is verified separately against a live model in
test_turn_router_live.py — that requires Ollama and is skipped without it.
"""

from app.ai.turn_router import (
    RecommendedTool,
    TurnDecision,
    coerce_channel,
    safe_default_decision,
    validate_recommended,
)
from app.api.capability_router import capability_action_map


def test_safe_default_is_keyword_free_specialist_on_chat():
    d = safe_default_decision("посчитай счета")
    assert d.intent == "specialist"
    assert d.output_channel == "chat"
    assert d.confidence == 0.0
    assert d.recommended == []


def test_validate_recommended_drops_unknown_capability():
    amap = capability_action_map()
    rec = [
        RecommendedTool(capability="invoices", action="list"),
        RecommendedTool(capability="nonexistent", action="foo"),
    ]
    out = validate_recommended(rec, amap)
    assert [r.capability for r in out] == ["invoices"]


def test_validate_recommended_strips_hallucinated_action_keeps_capability():
    amap = capability_action_map()
    rec = [RecommendedTool(capability="invoices", action="totally_made_up")]
    out = validate_recommended(rec, amap)
    assert len(out) == 1
    assert out[0].capability == "invoices"
    assert out[0].action == ""  # bad action dropped, capability kept


def test_coerce_channel_forces_workspace_for_analytical():
    d = TurnDecision(intent="analytical_table", output_channel="chat")
    assert coerce_channel(d).output_channel == "workspace"
    d2 = TurnDecision(intent="table_edit", output_channel="chat")
    assert coerce_channel(d2).output_channel == "workspace"


def test_coerce_channel_leaves_chat_for_smalltalk():
    d = TurnDecision(intent="smalltalk", output_channel="chat")
    assert coerce_channel(d).output_channel == "chat"


def test_validator_rescues_imperfect_model_output():
    # Mirrors real local-model output that used to fail validation outright:
    # role=null, role="email" (a capability), bare-string recommended, numeric entity.
    d = TurnDecision.model_validate({
        "intent": "answer_self",
        "role": None,                       # → data_analyst
        "output_channel": "desktop",        # invalid → chat
        "grounding": "vector",              # invalid → none
        "recommended": ["invoices", {"capability": "email", "action": "send"}],
        "entities": {"number_1": 12345},    # numeric → str
        "confidence": 0.9,
    })
    assert d.role == "data_analyst"
    assert d.output_channel == "chat"
    assert d.grounding == "none"
    assert d.recommended[0].capability == "invoices"
    assert d.entities["number_1"] == "12345"


def test_validator_coerces_invalid_role_string():
    d = TurnDecision.model_validate({"intent": "specialist", "role": "email"})
    assert d.role == "data_analyst"


def test_lenient_parse_yaml_bullets():
    # Real Qwopus3.6:27b output: YAML bullets instead of JSON. Must still route.
    from app.ai.turn_router import lenient_parse_decision

    raw = (
        "- intent: analytical_table\n- role: null\n- output_channel: workspace\n"
        "- grounding: memory\n- recommended: [spec_table, table_query]\n"
        "- entities: {}\n- goal: список фрез по поставщикам\n- confidence: 0.95"
    )
    parsed = lenient_parse_decision(raw)
    d = TurnDecision.model_validate(parsed)
    assert d.intent == "analytical_table"
    assert d.output_channel == "workspace"
    assert d.confidence == 0.95


def test_lenient_parse_fenced_json_and_plain():
    from app.ai.turn_router import lenient_parse_decision

    assert lenient_parse_decision('```json\n{"intent":"count"}\n```')["intent"] == "count"
    assert lenient_parse_decision('{"intent":"smalltalk","confidence":1.0}')["intent"] == "smalltalk"
    # Must NOT be fooled by an empty {} embedded in prose.
    assert lenient_parse_decision("entities: {}\nno decision here") is None
