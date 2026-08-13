import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[3] / "infra/cad-kernel/step_canonical.py"
SPEC = importlib.util.spec_from_file_location("step_canonical", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _step(data: bytes, *, timestamp: bytes = b"2026-08-14T00:00:00") -> bytes:
    return b"".join((
        b"ISO-10303-21;\nHEADER;\n",
        b"FILE_NAME('model.step','" + timestamp + b"',(''),(''),'",
        b"Open CASCADE STEP translator 7.9 42','','');\nENDSEC;\nDATA;\n",
        data,
        b"\nENDSEC;\nEND-ISO-10303-21;\n",
    ))


def test_canonical_step_normalizes_metadata_and_numeric_negative_zero():
    cold = _step(
        b"#1=DIRECTION('',(0.,-0.,1.));\n#2=TEXT('-0.');",
        timestamp=b"2026-08-14T00:00:01",
    )
    warm = _step(
        b"#1=DIRECTION('',(-0.,0.,1.));\n#2=TEXT('-0.');",
        timestamp=b"2026-08-14T00:00:02",
    )

    assert MODULE.canonicalize_step_bytes(cold) == MODULE.canonicalize_step_bytes(warm)
    canonical = MODULE.canonicalize_step_bytes(cold)
    assert b"1970-01-01T00:00:00" in canonical
    assert b"translator 7.9 1" in canonical
    assert b"TEXT('-0.')" in canonical
    assert b"-0.0E+00" not in MODULE.canonicalize_step_bytes(
        _step(b"#1=DIRECTION('',(-0.0E+00,0.,1.));")
    )


def test_canonical_step_preserves_negative_nonzero_values():
    canonical = MODULE.canonicalize_step_bytes(
        _step(b"#1=CARTESIAN_POINT('',(-0.125,0.,1.));")
    )

    assert b"-0.125" in canonical


@pytest.mark.parametrize("payload", [b"", b"HEADER;", b"FILE_NAME('x','now');DATA;"])
def test_canonical_step_rejects_incomplete_contract(payload: bytes):
    with pytest.raises(ValueError):
        MODULE.canonicalize_step_bytes(payload)
