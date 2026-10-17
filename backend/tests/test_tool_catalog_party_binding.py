"""tool_catalog actions keyed by party accept the catalog supplier id (E52).

resolve_supplier answers with party_id and tool_supplier_id; the model passed
the latter as supplier_id to list_entries/list_uploads and got
«Missing required path parameters: ['party_id']» in every catalog run.
"""

from contextlib import asynccontextmanager

import pytest

from app.api import capability_router


@pytest.fixture
def linked(db_session):
    async def make():
        from app.db.models import Party, PartyRole, ToolSupplier

        party = Party(name="ООО «Эвал-Никель»", inn="7703000064", role=PartyRole.supplier)
        db_session.add(party)
        await db_session.flush()
        supplier = ToolSupplier(name=party.name, main_supplier_id=party.id, is_active=True)
        db_session.add(supplier)
        await db_session.commit()
        return party, supplier

    return make


@pytest.fixture
def same_session(db_session, monkeypatch):
    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr("app.db.session._get_session_factory", lambda: session)


@pytest.mark.asyncio
async def test_the_catalog_supplier_id_names_its_party(linked, same_session):
    party, supplier = await linked()
    body = {"action": "list_entries", "supplier_id": str(supplier.id)}
    await capability_router._party_from_tool_supplier(body)
    assert body["party_id"] == str(party.id)


@pytest.mark.asyncio
async def test_a_given_party_is_never_replaced(linked, same_session):
    _party, supplier = await linked()
    body = {"party_id": "p-given", "supplier_id": str(supplier.id)}
    await capability_router._party_from_tool_supplier(body)
    assert body["party_id"] == "p-given"


@pytest.mark.asyncio
async def test_an_unknown_supplier_leaves_the_call_to_fail_as_before(same_session):
    body = {"supplier_id": "8c557970-582b-4cd2-b10e-42219cadac57"}
    await capability_router._party_from_tool_supplier(body)
    assert "party_id" not in body
