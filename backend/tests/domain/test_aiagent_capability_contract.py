import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "check_aiagent_contract",
    ROOT / "scripts" / "check_aiagent_contract.py",
)
assert spec and spec.loader
check_aiagent_contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_aiagent_contract)
check_capability_contract = check_aiagent_contract.check_capability_contract


def test_capability_contract_matches_dispatcher_and_gates():
    errors, warnings, stats = check_capability_contract()

    assert errors == []
    assert stats["capabilities"] >= 10
    assert stats["capability_actions"] >= 50
    assert stats["capability_gate_actions"] >= 10
    assert isinstance(warnings, list)
