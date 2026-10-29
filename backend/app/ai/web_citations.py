"""W5: what the answer cites is what the turn read.

Every page the employee reads in a turn — ``search.browse``, ``research``
(quick and deep), ``read_source``, ``computer_use.browser_fetch`` and
``desktop_read`` — gets one number for the whole turn. The tool result the
model sees carries that number (``n``, ``cite_as``), so the answer says
``[3]`` instead of retyping a URL, and two calls cannot both call their first
page "1".

After the turn :func:`check_answer` holds the answer to the register:

* ``CITED_SOURCE_NOT_READ`` — a ``[n]`` or a URL no page of the turn has;
* ``BLOCKED_SOURCE_USED`` — a citation of a page that was not read (bot wall,
  empty, error);
* ``VALUE_NOT_IN_SOURCE`` — a number next to ``[n]`` that is not in that
  page's text (compared as numbers: «1 200», «1200», «1,2 тыс.» is not
  attempted — only what the page literally has);
* ``UNCITED_WEB_CLAIM`` — the turn read the web and the answer gives numbers
  without saying from where (advisory: the numbers may be the company's own).

The checks are deterministic. What to do about them — one repair of the
answer, then a visible «не подтверждено» — is the orchestrator's.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from app.ai.audit import AuditCode, AuditIssue
from app.ai.web_urls import url_key

# What a citation looks like: [3], [3, 5], [3][5], [3–5].
_CITE = re.compile(r"\[(\d{1,3}(?:\s*[,–-]\s*\d{1,3})*)\]")
_URL = re.compile(r"https?://[^\s<>()\[\]«»\"']+")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[   ]\d{3})*(?:[.,]\d+)?(?![\w])")
# A number with something that makes it a measured claim: unit, %, currency.
_UNIT = re.compile(
    r"\d\s?(?:%|°|мм|см|м\b|м3|м³|м/с|км|кг|т\b|мл|бар|атм|мпа|кпа|квт|вт\b|ква|"
    r"об/мин|гц|руб|₽|\$|€|usd|eur|kg|mm|kw|rpm|bar|psi|hz)",
    re.I,
)


def _turn_register_entry(n: int, url: str, **fields: Any) -> dict:
    host = (urlparse(url).hostname or "").lower()
    return {"n": n, "url": url, "domain": host.removeprefix("www."), **fields}


class SourceRegister:
    """The pages one turn read, numbered once for the whole turn."""

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self._by_key: dict[str, dict] = {}
        self._by_ref: dict[str, dict] = {}

    def reset(self) -> None:
        self.__init__()

    def add(self, url: str, **fields: Any) -> dict:
        key = url_key(url)
        entry = self._by_key.get(key)
        if entry is None:
            entry = _turn_register_entry(len(self.entries) + 1, url)
            self.entries.append(entry)
            self._by_key[key] = entry
        for name, value in fields.items():
            # A later successful read of the same page improves the entry.
            if value is not None and (name != "read_status" or entry.get(name) != "ok"):
                entry[name] = value
        if entry.get("source_ref"):
            self._by_ref[entry["source_ref"]] = entry
        return entry

    def by_ref(self, ref: str) -> dict | None:
        return self._by_ref.get(ref)

    def get(self, n: int) -> dict | None:
        return self.entries[n - 1] if 0 < n <= len(self.entries) else None

    def keys(self) -> set[str]:
        return set(self._by_key)


def _inner(result: Any) -> dict | None:
    """The payload of a ToolResult v1 envelope, or the plain dict."""
    if not isinstance(result, dict):
        return None
    if result.get("version") == 1 and isinstance(result.get("data"), dict):
        return result["data"]
    if isinstance(result.get("result"), dict) and "action_id" in result:
        return result["result"]  # computer_use /execute answer
    return result


def _cite(entry: dict) -> str:
    return f"[{entry['n']}]"


def number_sources(register: SourceRegister, capability: str, args: dict, result: Any) -> None:
    """Give the pages in this tool result their turn numbers, in place."""
    data = _inner(result)
    if data is None:
        return
    action = str(args.get("action") or "")
    if capability == "search" and action == "browse":
        url = data.get("final_url") or data.get("url") or args.get("url")
        if url:
            entry = register.add(
                str(url),
                title=data.get("title"),
                read_status=data.get("read_status"),
                source_ref=data.get("source_ref"),
            )
            data["n"], data["cite_as"] = entry["n"], _cite(entry)
        return
    if capability == "search" and action == "research":
        renumber: dict[int, int] = {}
        for source in data.get("sources") or []:
            if not isinstance(source, dict) or not source.get("url"):
                continue
            entry = register.add(
                str(source["url"]),
                title=source.get("title"),
                read_status=source.get("read_status"),
                source_ref=source.get("source_ref"),
                kind=source.get("kind"),
            )
            if isinstance(source.get("n"), int):
                renumber[source["n"]] = entry["n"]
            source["n"], source["cite_as"] = entry["n"], _cite(entry)
        for source in data.get("sources") or []:
            if isinstance(source, dict) and isinstance(source.get("duplicate_of"), int):
                source["duplicate_of"] = renumber.get(
                    source["duplicate_of"], source["duplicate_of"]
                )
        for fact in data.get("facts") or []:
            for group in fact.get("values") or []:
                group["sources"] = [renumber.get(n, n) for n in group.get("sources") or []]
        return
    if capability == "search" and action == "read_source":
        entry = register.by_ref(str(data.get("source_ref") or "")) or (
            register.add(str(data["url"]), title=data.get("title"), read_status="ok")
            if data.get("url")
            else None
        )
        if entry is not None:
            data["n"], data["cite_as"] = entry["n"], _cite(entry)
        return
    if capability == "computer_use" and action in {"browser_fetch", "desktop_snapshot"}:
        url = data.get("final_url") or data.get("url")
        if url:
            entry = register.add(
                str(url),
                title=data.get("title"),
                read_status=data.get("read_status") or ("ok" if data.get("text") else "empty"),
                source_ref=data.get("source_ref"),
                text=(data.get("text") or "")[:200000] if not data.get("source_ref") else None,
            )
            data["n"], data["cite_as"] = entry["n"], _cite(entry)
        return
    if capability == "computer_use" and action == "desktop_read" and data.get("url"):
        entry = register.add(
            str(data["url"]),
            title=data.get("title"),
            read_status="ok" if data.get("text") else "empty",
            text=(data.get("text") or "")[:200000],
        )
        data["n"], data["cite_as"] = entry["n"], _cite(entry)


# ── checking the answer ──────────────────────────────────────────────────────


def _cited_numbers(token: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"\s*,\s*", token):
        bounds = re.split(r"\s*[–-]\s*", part)
        if len(bounds) == 2 and all(b.isdigit() for b in bounds):
            lo, hi = int(bounds[0]), int(bounds[1])
            if 0 < lo <= hi and hi - lo < 20:
                out.extend(range(lo, hi + 1))
        elif part.strip().isdigit():
            out.append(int(part))
    return out


def _as_number(text: str) -> float | None:
    clean = re.sub(r"[   ]", "", text).replace(",", ".")
    try:
        return float(clean)
    except ValueError:
        return None


def numbers_in(text: str) -> set[float]:
    found = set()
    for match in _NUMBER.finditer(text or ""):
        value = _as_number(match.group(0))
        if value is not None:
            found.add(value)
    return found


def _segments(answer: str) -> list[str]:
    """Lines and sentences: a citation covers the claim it stands in."""
    parts: list[str] = []
    for line in answer.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("|"):
            parts.append(line)  # a table row is one claim
            continue
        parts.extend(p for p in re.split(r"(?<=[.!?;])\s+(?=[A-ZА-ЯЁ\d«(])", line) if p.strip())
    return parts


def _claim_numbers(segment: str) -> set[float]:
    """Numbers the segment states, without the citation marks and URLs."""
    bare = _CITE.sub(" ", _URL.sub(" ", segment))
    return {n for n in numbers_in(bare) if not (1900 <= n <= 2100 and float(n).is_integer())}


def check_answer(answer: str, register: SourceRegister, texts: dict[int, str]) -> list[AuditIssue]:
    """Issues of the answer against the turn's pages (``texts``: n → page text)."""
    issues: list[AuditIssue] = []
    if not register.entries or not (answer or "").strip():
        return issues
    known = register.keys()
    domains = {e["domain"] for e in register.entries}

    for url in dict.fromkeys(_URL.findall(answer)):
        url = url.rstrip(".,;:")
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        if url_key(url) not in known and host not in domains:
            issues.append(
                AuditIssue(
                    code=AuditCode.CITED_SOURCE_NOT_READ,
                    message=f"Ссылка {url} — не из прочитанных в этом ходе страниц.",
                    context={"url": url},
                )
            )

    cited_anywhere = False
    for segment in _segments(answer):
        marks = [n for token in _CITE.findall(segment) for n in _cited_numbers(token)]
        claims = _claim_numbers(segment)
        if not marks:
            if claims and _UNIT.search(_URL.sub(" ", segment)) and not _URL.search(segment):
                issues.append(
                    AuditIssue(
                        code=AuditCode.UNCITED_WEB_CLAIM,
                        severity="advisory",
                        message=f"Значение без источника: «{segment[:160]}».",
                        context={"segment": segment[:300]},
                    )
                )
            continue
        cited_anywhere = True
        unknown = [n for n in marks if register.get(n) is None]
        for n in unknown:
            issues.append(
                AuditIssue(
                    code=AuditCode.CITED_SOURCE_NOT_READ,
                    message=f"Источника [{n}] в этом ходе нет.",
                    context={"n": n},
                )
            )
        cited = [register.get(n) for n in marks if register.get(n) is not None]
        for entry in cited:
            if entry.get("read_status") not in (None, "ok"):
                issues.append(
                    AuditIssue(
                        code=AuditCode.BLOCKED_SOURCE_USED,
                        message=(
                            f"[{entry['n']}] не прочитан ({entry.get('read_status')}) — "
                            "на него нельзя ссылаться как на подтверждение."
                        ),
                        context={"n": entry["n"], "read_status": entry.get("read_status")},
                    )
                )
        readable = [e for e in cited if e.get("read_status") in (None, "ok") and texts.get(e["n"])]
        if not claims or not readable:
            continue  # nothing to compare, or the text is not at hand: unknown, not wrong
        on_pages = set().union(*(numbers_in(texts[e["n"]]) for e in readable))
        for value in sorted(claims - on_pages):
            shown = int(value) if float(value).is_integer() else value
            issues.append(
                AuditIssue(
                    code=AuditCode.VALUE_NOT_IN_SOURCE,
                    message=(
                        f"Числа {shown} нет в тексте "
                        f"{', '.join(_cite(e) for e in readable)}: «{segment[:160]}»."
                    ),
                    context={
                        "value": value,
                        "sources": [e["n"] for e in readable],
                        "segment": segment[:300],
                    },
                )
            )
    if not cited_anywhere:
        # Advisory issues about uncited values are kept only when nothing is
        # cited at all — then they say «this answer gives no sources».
        return issues
    return [i for i in issues if i.code != AuditCode.UNCITED_WEB_CLAIM]


def cited_in(answer: str) -> list[int]:
    seen: list[int] = []
    for token in _CITE.findall(answer or ""):
        for n in _cited_numbers(token):
            if n not in seen:
                seen.append(n)
    return seen


def sources_block(answer: str, register: SourceRegister) -> str:
    """«Источники:» with the pages the answer cites, unless it lists them itself."""
    lines = []
    for n in cited_in(answer):
        entry = register.get(n)
        if entry is None or entry["url"] in answer:
            continue
        title = (entry.get("title") or entry["domain"] or entry["url"]).strip()[:120]
        lines.append(f"[{n}] {title} — {entry['url']}")
    return ("Источники:\n" + "\n".join(lines)) if lines else ""


def mark_unconfirmed(answer: str, issues: list[AuditIssue]) -> str:
    """What could not be repaired is said, not hidden."""
    notes = []
    for issue in issues:
        if issue.code in (
            AuditCode.VALUE_NOT_IN_SOURCE,
            AuditCode.CITED_SOURCE_NOT_READ,
            AuditCode.BLOCKED_SOURCE_USED,
        ):
            notes.append(f"— {issue.message}")
    if not notes:
        return answer
    return (
        answer.rstrip() + "\n\n⚠ Не подтверждено источниками:\n" + "\n".join(dict.fromkeys(notes))
    )


def citation_issues(issues: list[AuditIssue]) -> list[AuditIssue]:
    return [
        i
        for i in issues
        if i.code
        in (
            AuditCode.VALUE_NOT_IN_SOURCE,
            AuditCode.CITED_SOURCE_NOT_READ,
            AuditCode.BLOCKED_SOURCE_USED,
        )
    ]
