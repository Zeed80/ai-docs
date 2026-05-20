"""Tests for Rooms API — group chat and direct messages."""

import uuid

import pytest
from httpx import AsyncClient

from app.db.models import Room, RoomMember, RoomMessage, RoomType


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def group_room(db_session):
    room = Room(
        name="Закупки Общий",
        type=RoomType.group,
        created_by="dev-user",
        description="Чат отдела закупок",
    )
    db_session.add(room)
    await db_session.flush()
    member = RoomMember(room_id=room.id, user_sub="dev-user", role="owner")
    db_session.add(member)
    await db_session.commit()
    return room


@pytest.fixture
async def room_with_message(db_session, group_room):
    msg = RoomMessage(
        room_id=group_room.id,
        sender_sub="dev-user",
        content="Привет всем!",
        content_type="text",
    )
    db_session.add(msg)
    await db_session.commit()
    return msg


# ── List rooms ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_rooms_empty(client: AsyncClient):
    resp = await client.get("/api/rooms")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert isinstance(data["items"], list)


@pytest.mark.asyncio
async def test_list_rooms(client: AsyncClient, group_room):
    resp = await client.get("/api/rooms")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    names = [r["name"] for r in data["items"]]
    assert "Закупки Общий" in names


# ── Create room ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_room(client: AsyncClient):
    resp = await client.post("/api/rooms", json={
        "name": "Бухгалтерия",
        "description": "Чат бухгалтерского отдела",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Бухгалтерия"
    assert data["type"] == "group"
    assert data["created_by"] == "dev-user"
    assert "id" in data


@pytest.mark.asyncio
async def test_create_room_appears_in_list(client: AsyncClient):
    create_resp = await client.post("/api/rooms", json={"name": "Производство"})
    assert create_resp.status_code == 201

    list_resp = await client.get("/api/rooms")
    names = [r["name"] for r in list_resp.json()["items"]]
    assert "Производство" in names


# ── Get room ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_room(client: AsyncClient, group_room):
    resp = await client.get(f"/api/rooms/{group_room.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Закупки Общий"
    assert data["type"] == "group"
    assert "member_count" in data


@pytest.mark.asyncio
async def test_get_room_not_member(client: AsyncClient, db_session):
    other_room = Room(
        name="Секретный чат",
        type=RoomType.group,
        created_by="other-user",
    )
    db_session.add(other_room)
    await db_session.commit()
    resp = await client.get(f"/api/rooms/{other_room.id}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_room_not_found(client: AsyncClient):
    resp = await client.get(f"/api/rooms/{uuid.uuid4()}")
    assert resp.status_code in (403, 404)


# ── DM ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_dm(client: AsyncClient):
    resp = await client.get("/api/rooms/dm/other-user-sub")
    assert resp.status_code == 200
    data = resp.json()
    assert data["type"] == "direct"
    assert data["member_count"] == 2


@pytest.mark.asyncio
async def test_dm_idempotent(client: AsyncClient):
    resp1 = await client.get("/api/rooms/dm/colleague-sub")
    assert resp1.status_code == 200
    room_id1 = resp1.json()["id"]

    resp2 = await client.get("/api/rooms/dm/colleague-sub")
    assert resp2.status_code == 200
    assert resp2.json()["id"] == room_id1


@pytest.mark.asyncio
async def test_dm_self_forbidden(client: AsyncClient):
    resp = await client.get("/api/rooms/dm/dev-user")
    assert resp.status_code == 400


# ── Members ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_members(client: AsyncClient, group_room):
    resp = await client.get(f"/api/rooms/{group_room.id}/members")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    subs = [m["user_sub"] for m in data]
    assert "dev-user" in subs


@pytest.mark.asyncio
async def test_add_member(client: AsyncClient, group_room):
    resp = await client.post(f"/api/rooms/{group_room.id}/members", json={
        "user_sub": "new-colleague",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] in ("added", "already_member")


@pytest.mark.asyncio
async def test_add_member_idempotent(client: AsyncClient, group_room):
    await client.post(f"/api/rooms/{group_room.id}/members", json={"user_sub": "user-x"})
    resp = await client.post(f"/api/rooms/{group_room.id}/members", json={"user_sub": "user-x"})
    assert resp.status_code == 201
    assert resp.json()["status"] == "already_member"


@pytest.mark.asyncio
async def test_remove_member(client: AsyncClient, group_room, db_session):
    db_session.add(RoomMember(room_id=group_room.id, user_sub="temp-user", role="member"))
    await db_session.commit()

    resp = await client.delete(f"/api/rooms/{group_room.id}/members/temp-user")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "removed"


# ── Messages ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_messages_empty(client: AsyncClient, group_room):
    resp = await client.get(f"/api/rooms/{group_room.id}/messages")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_list_messages(client: AsyncClient, group_room, room_with_message):
    resp = await client.get(f"/api/rooms/{group_room.id}/messages")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    contents = [m["content"] for m in data]
    assert "Привет всем!" in contents


@pytest.mark.asyncio
async def test_send_message(client: AsyncClient, group_room):
    resp = await client.post(f"/api/rooms/{group_room.id}/messages", json={
        "content": "Счёт получен, проверяю.",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["content"] == "Счёт получен, проверяю."
    assert data["sender_sub"] == "dev-user"
    assert "id" in data


@pytest.mark.asyncio
async def test_send_message_appears_in_list(client: AsyncClient, group_room):
    await client.post(f"/api/rooms/{group_room.id}/messages", json={
        "content": "Тестовое сообщение",
    })
    resp = await client.get(f"/api/rooms/{group_room.id}/messages")
    contents = [m["content"] for m in resp.json()]
    assert "Тестовое сообщение" in contents


@pytest.mark.asyncio
async def test_edit_message(client: AsyncClient, group_room, room_with_message):
    resp = await client.patch(
        f"/api/rooms/{group_room.id}/messages/{room_with_message.id}",
        json={"content": "Привет (исправлено)!"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["content"] == "Привет (исправлено)!"
    assert data["is_edited"] is True


@pytest.mark.asyncio
async def test_delete_message(client: AsyncClient, group_room, db_session):
    msg = RoomMessage(
        room_id=group_room.id,
        sender_sub="dev-user",
        content="Удалить меня",
        content_type="text",
    )
    db_session.add(msg)
    await db_session.commit()

    resp = await client.delete(f"/api/rooms/{group_room.id}/messages/{msg.id}")
    assert resp.status_code == 204


# ── Mark read ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_read(client: AsyncClient, group_room, room_with_message):
    resp = await client.post(f"/api/rooms/{group_room.id}/read")
    assert resp.status_code == 200
