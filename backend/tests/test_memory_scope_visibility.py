"""Unit tests for MemoryFact department-scope visibility (_resolve_visible_scopes).

These test the SQLAlchemy predicate builder directly rather than going through
POST /api/memory/search, because that endpoint's other branches use pg_trgm/@@
which the SQLite test DB doesn't support (see the skipped tests in
test_memory_api.py). _resolve_visible_scopes is a pure expression builder, so
it can be exercised without a database at all — compile to SQL and inspect the
literal scope values that ended up in the predicate.
"""

from app.api.memory import _resolve_visible_scopes
from app.domain.graph import MemorySearchRequest


def _compiled(expr) -> str:
    return str(expr.compile(compile_kwargs={"literal_binds": True}))


def _payload(scope: str | None, session_id: str | None = None) -> MemorySearchRequest:
    return MemorySearchRequest(query="q", scope=scope, session_id=session_id)


# ── No department (department_id=None) — must reproduce pre-existing behaviour ──


def test_session_scope_no_department_matches_baseline():
    sql = _compiled(_resolve_visible_scopes(_payload("session"), department_id=None))
    assert "project" in sql and "global" in sql
    assert "department" not in sql


def test_owner_scope_no_department_matches_baseline():
    sql = _compiled(_resolve_visible_scopes(_payload("owner:alice"), department_id=None))
    assert "owner:alice" in sql
    assert "project" in sql and "global" in sql
    assert "department" not in sql


def test_explicit_scope_no_department_matches_baseline():
    sql = _compiled(_resolve_visible_scopes(_payload("custom"), department_id=None))
    assert "custom" in sql and "global" in sql
    assert "department" not in sql


def test_no_scope_no_department_matches_baseline():
    sql = _compiled(_resolve_visible_scopes(_payload(None), department_id=None))
    assert "project" in sql and "global" in sql
    assert "department" not in sql


# ── With department — additive, never narrows ────────────────────────────────


def test_session_scope_with_department_adds_department_branch():
    sql = _compiled(_resolve_visible_scopes(_payload("session"), department_id="dept-1"))
    assert "department:dept-1" in sql
    # baseline branches still present
    assert "project" in sql and "global" in sql


def test_owner_scope_with_department_adds_department_branch():
    sql = _compiled(_resolve_visible_scopes(_payload("owner:alice"), department_id="dept-1"))
    assert "owner:alice" in sql
    assert "department:dept-1" in sql


def test_explicit_scope_with_department_adds_department_branch():
    sql = _compiled(_resolve_visible_scopes(_payload("custom"), department_id="dept-1"))
    assert "custom" in sql
    assert "department:dept-1" in sql


def test_no_scope_with_department_adds_department_branch():
    sql = _compiled(_resolve_visible_scopes(_payload(None), department_id="dept-1"))
    assert "department:dept-1" in sql


def test_different_departments_do_not_cross_visibility():
    """A caller in dept-1 must not have dept-2's scope in its predicate."""
    sql = _compiled(_resolve_visible_scopes(_payload(None), department_id="dept-1"))
    assert "department:dept-1" in sql
    assert "department:dept-2" not in sql
