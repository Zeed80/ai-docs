"""Procedures: the prompt carries their index, the text comes by procedures.read."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.ai import procedures
from app.ai.procedures import list_procedures, procedures_index, read_procedure


def test_every_procedure_file_is_listed():
    # A file the parser rejects would silently vanish from the index.
    files = sorted(p.stem for p in procedures.PROCEDURES_DIR.glob("*.md"))
    assert files
    assert [p.name for p in list_procedures()] == files


def test_the_index_names_every_procedure_with_its_description():
    index = procedures_index()
    for proc in list_procedures():
        assert f"- `{proc.name}` — {proc.description}" in index


def test_read_returns_the_text_without_front_matter():
    proc = read_procedure("mail")
    assert proc is not None
    assert not proc.text.startswith("---")
    assert "email" in proc.text


def test_an_unknown_or_path_like_name_reads_nothing():
    assert read_procedure("no-such-procedure") is None
    assert read_procedure("../prompts/base") is None


def test_a_broken_front_matter_is_skipped(tmp_path):
    (tmp_path / "good.md").write_text('---\nname: good\ndescription: "Хорошая"\n---\nТекст\n')
    (tmp_path / "bad.md").write_text("---\nname: bad\ndescription: Плохая: двоеточие\n---\n")
    assert [p.name for p in list_procedures(tmp_path)] == ["good"]


def test_the_system_prompt_carries_the_index():
    from app.ai.agent_loop import _load_system_prompt

    assert procedures_index() in _load_system_prompt()


def test_procedures_read_is_a_routed_read_action():
    from app.ai.tool_catalog import get_tool
    from app.api.capability_router import capability_action_map, validate_capability_catalog

    tool = get_tool("procedures", "read")
    assert tool is not None and tool.effect == "read"
    assert capability_action_map()["procedures"] == ["read"]
    assert validate_capability_catalog() == []


def test_the_endpoint_serves_a_procedure():
    from app.api.agent_control_plane import get_agent_procedure, list_agent_procedures

    listed = asyncio.run(list_agent_procedures())
    assert {p.name for p in listed} == {p.name for p in list_procedures()}
    proc = asyncio.run(get_agent_procedure("warehouse"))
    assert proc.text == read_procedure("warehouse").text
    with pytest.raises(HTTPException) as err:
        asyncio.run(get_agent_procedure("nope"))
    assert err.value.status_code == 404
