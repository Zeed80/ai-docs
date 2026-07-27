"""The kernel boundary: one list of feature kinds, written down twice.

``infra/cad-kernel/server.py`` validates incoming features with
``extra="forbid"``, so a ``kind`` the kernel does not know is not a degraded
build — it is an immediate 422 for EVERY 3D build. The two declarations live in
different languages of the same system (a FastAPI service in its own container,
and the Pydantic model the backend emits), and nothing has been checking that
they still agree. This does.

Only ``kind`` is compared on purpose: ``params`` is a free ``dict[str, Any]`` on
both sides, so new PARAMETERS are deliberately allowed to appear on one side
first.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_KERNEL = _REPO / "infra" / "cad-kernel" / "server.py"
_BACKEND = _REPO / "backend" / "app" / "ai" / "cad_ir" / "feature_tree.py"


def _literal_kinds(path: Path, class_name: str) -> list[str]:
    """The ``kind: Literal[...]`` values declared by one class."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for statement in node.body:
            if (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.target.id == "kind"
            ):
                annotation = statement.annotation
                # Literal[...] — with or without a default value after it.
                if isinstance(annotation, ast.Subscript):
                    elements = annotation.slice
                    values = (
                        elements.elts if isinstance(elements, ast.Tuple) else [elements]
                    )
                    return [
                        value.value
                        for value in values
                        if isinstance(value, ast.Constant) and isinstance(value.value, str)
                    ]
    raise AssertionError(f"{class_name}.kind: Literal[...] not found in {path}")


def test_feature_kinds_match_across_the_kernel_boundary():
    if not _KERNEL.exists():  # pragma: no cover — kernel sources not checked out
        pytest.skip("infra/cad-kernel/server.py is not available")

    kernel = _literal_kinds(_KERNEL, "Feature")
    backend = _literal_kinds(_BACKEND, "Feature3D")

    assert kernel, "the kernel declares no feature kinds"
    # Sets, not lists: order carries no meaning for a Literal, and demanding it
    # would fail a harmless reordering.
    assert set(backend) == set(kernel), (
        "Feature3D.kind and the kernel's Feature.kind have drifted apart — "
        f"backend only: {sorted(set(backend) - set(kernel))}, "
        f"kernel only: {sorted(set(kernel) - set(backend))}. "
        "The kernel rejects unknown kinds outright (extra=\"forbid\"), so this "
        "breaks every 3D build."
    )
