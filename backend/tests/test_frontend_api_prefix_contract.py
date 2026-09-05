"""Every frontend call to the backend must go through the /api prefix.

The catalog upload on the supplier card never reached the backend: two calls in
`frontend/app/suppliers/[id]/page.tsx` were written as `${API}/tool-catalog/...`
while the Next.js proxy (`frontend/app/api/[...proxy]/route.ts`) forwards only
`/api/*`. With API === "" (the normal same-origin case) those requests hit
Next.js and returned 404 HTML — the tab showed "0 позиций" and the upload
banner claimed success. Neighbouring lines in the SAME file had the prefix.

A grep-shaped contract catches the whole class, not the two lines we fixed.
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
SEARCH_DIRS = ("app", "lib", "components")

# `${API}/...` where the path does NOT start with /api — the broken shape.
_BAD_CALL = re.compile(r"\$\{API\}/(?!api/)[a-z]")


def _sources() -> list[Path]:
    files: list[Path] = []
    for directory in SEARCH_DIRS:
        root = FRONTEND / directory
        if not root.exists():
            continue
        for suffix in ("*.ts", "*.tsx"):
            files.extend(path for path in root.rglob(suffix) if "node_modules" not in path.parts)
    return files


def test_frontend_backend_calls_use_api_prefix():
    offenders: list[str] = []
    for path in _sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _BAD_CALL.search(line):
                rel = path.relative_to(FRONTEND)
                offenders.append(f"{rel}:{lineno}: {line.strip()[:100]}")
    assert not offenders, (
        "frontend calls that bypass the /api proxy (they will 404 in the browser):\n"
        + "\n".join(offenders)
    )


def test_the_contract_would_catch_the_original_defect():
    """Guard the guard: the regex must flag the exact line that was broken."""
    broken = "        `${API}/tool-catalog/by-supplier/${partyId}/catalog`,"
    fixed = "        `${API}/api/tool-catalog/by-supplier/${partyId}/catalog`,"
    assert _BAD_CALL.search(broken)
    assert not _BAD_CALL.search(fixed)


def test_frontend_directory_is_reachable_from_tests():
    """If the layout moves, the contract must fail loudly, not pass vacuously."""
    assert _sources(), f"no frontend sources found under {FRONTEND}"
