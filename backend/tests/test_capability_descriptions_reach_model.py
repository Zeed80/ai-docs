"""Every curated capability description reaches the model whole.

E52: tools were cut at 1200 characters, so the documents.link rules, the
email draft example and the compose_assist note never reached the model.
"""

import yaml

from app.ai.agent_loop import _load_capabilities, gateway_config


def test_capability_descriptions_reach_the_model_whole():
    tools, _ = _load_capabilities()
    sent = {t["function"]["name"]: t["function"]["description"] for t in tools}
    raw = yaml.safe_load(gateway_config.capabilities_path.read_text(encoding="utf-8"))
    caps = raw["capabilities"]
    items = caps.items() if isinstance(caps, dict) else [(c["name"], c) for c in caps]
    long_ones = 0
    for name, cap in items:
        text = (cap.get("description") or "").strip()
        if len(text) > 1200:
            long_ones += 1
        if name in sent:
            assert sent[name] == text, name
    assert long_ones, "the guard needs a description longer than the old cut"
    assert "linked_entity_id" in sent["documents"]
