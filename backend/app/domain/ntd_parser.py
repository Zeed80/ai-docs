"""Conservative SQL-first parser for normative text."""

from __future__ import annotations

import re
from dataclasses import dataclass


CLAUSE_RE = re.compile(r"^\s*(?P<number>\d+(?:\.\d+){0,5})[\).\s-]+(?P<text>.+?)\s*$")
REQUIREMENT_MARKERS = (
    "должен",
    "должна",
    "должно",
    "должны",
    "следует",
    "необходимо",
    "требуется",
    "указывают",
    "указать",
)


@dataclass(frozen=True)
class ParsedClause:
    clause_number: str
    title: str | None
    text: str


@dataclass(frozen=True)
class ParsedRequirement:
    clause_number: str
    requirement_code: str
    requirement_type: str
    text: str
    required_keywords: list[str]
    severity: str


@dataclass(frozen=True)
class ParsedNTD:
    clauses: list[ParsedClause]
    requirements: list[ParsedRequirement]


@dataclass(frozen=True)
class DetectedNTDMeta:
    code: str
    title: str
    document_type: str
    version: str


def detect_normative_metadata(text: str, *, fallback_title: str = "Нормативный документ") -> DetectedNTDMeta:
    normalized = _normalize_space(text)
    code_match = re.search(
        r"\b(?P<type>ГОСТ|ОСТ|СТП|ТУ|РД|МИ)\s*(?P<number>[\d.\-–—/]+(?:-\d{2,4})?)",
        normalized,
        re.I,
    )
    if code_match:
        document_type = code_match.group("type").upper()
        number = code_match.group("number").replace("–", "-").replace("—", "-")
        code = f"{document_type} {number}"
    else:
        document_type = "НТД"
        code = "НТД-АВТО"
    version = _detect_version(code)
    title = _detect_title(normalized, fallback_title=fallback_title, code=code)
    return DetectedNTDMeta(code=code, title=title, document_type=document_type, version=version)


def parse_normative_text(text: str, *, code: str, default_requirement_type: str = "generic") -> ParsedNTD:
    blocks = _split_clauses(text)
    clauses: list[ParsedClause] = []
    requirements: list[ParsedRequirement] = []
    for number, body in blocks:
        clean_body = _normalize_space(body)
        if not clean_body:
            continue
        title = _guess_title(clean_body)
        clauses.append(ParsedClause(clause_number=number, title=title, text=clean_body))
        if _looks_like_requirement(clean_body):
            requirements.append(
                ParsedRequirement(
                    clause_number=number,
                    requirement_code=f"{code}:{number}",
                    requirement_type=default_requirement_type,
                    text=clean_body,
                    required_keywords=_extract_keywords(clean_body),
                    severity=_guess_severity(clean_body),
                )
            )
    if not clauses and text.strip():
        clean_text = _normalize_space(text)
        clauses.append(ParsedClause(clause_number="1", title=None, text=clean_text))
        if _looks_like_requirement(clean_text):
            requirements.append(
                ParsedRequirement(
                    clause_number="1",
                    requirement_code=f"{code}:1",
                    requirement_type=default_requirement_type,
                    text=clean_text,
                    required_keywords=_extract_keywords(clean_text),
                    severity=_guess_severity(clean_text),
                )
            )
    return ParsedNTD(clauses=clauses, requirements=requirements)


def _split_clauses(text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, list[str]]] = []
    current_number: str | None = None
    current_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = CLAUSE_RE.match(line)
        if match:
            if current_number and current_lines:
                blocks.append((current_number, current_lines))
            current_number = match.group("number")
            current_lines = [match.group("text")]
        elif current_number:
            current_lines.append(line)
    if current_number and current_lines:
        blocks.append((current_number, current_lines))
    return [(number, "\n".join(lines)) for number, lines in blocks]


def _looks_like_requirement(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in REQUIREMENT_MARKERS)


def _extract_keywords(text: str) -> list[str]:
    lower = text.lower()
    candidates = [
        "материал",
        "контроль",
        "операция",
        "оснастка",
        "инструмент",
        "станок",
        "шероховатость",
        "допуск",
        "размер",
        "маршрут",
        "норма",
    ]
    return [candidate for candidate in candidates if candidate in lower]


def _guess_severity(text: str) -> str:
    lower = text.lower()
    if "запрещ" in lower or "не допуска" in lower:
        return "error"
    if "долж" in lower or "необходимо" in lower or "требуется" in lower:
        return "warning"
    return "info"


def _guess_title(text: str) -> str | None:
    first_sentence = re.split(r"[.!?]\s+", text, maxsplit=1)[0].strip()
    if 0 < len(first_sentence) <= 120:
        return first_sentence
    return None


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _detect_version(code: str) -> str:
    match = re.search(r"-(\d{2,4})\b", code)
    if not match:
        return "current"
    year = match.group(1)
    if len(year) == 2:
        year = "20" + year if int(year) < 50 else "19" + year
    return year


def _detect_title(text: str, *, fallback_title: str, code: str) -> str:
    if not text:
        return fallback_title
    code_pos = text.lower().find(code.lower())
    tail = text[code_pos + len(code):] if code_pos >= 0 else text
    candidates = re.split(r"[.;\n]", tail, maxsplit=2)
    for candidate in candidates:
        clean = candidate.strip(" :-—")
        if 8 <= len(clean) <= 220 and not re.fullmatch(r"[\d.\-–—/ ]+", clean):
            return clean
    first = text[:220].strip(" :-—")
    return first or fallback_title
