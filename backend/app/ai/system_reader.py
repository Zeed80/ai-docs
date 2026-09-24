"""Ф5.3: read a P&ID / MEP / electrical / hydraulic schematic into an
EngineeringSystemModel.

Unlike the mechanical (Ф1-2) and construction (Ф5.2) readers this domain
carries NO geometry at all -- `EngineeringSystemModel` (`system_emg.py`) is
pure connectivity: equipment, their typed ports, and which ports connect to
which. That is exactly what a schematic sheet states directly (line-following
between symbols, arrow direction, medium labels near lines) with no
coordinate reading involved, so this reader is a single VLM pass, much
closer in shape to `assembly_extractor.py`'s BOM-table reading than to the
mechanical spec reader's fragment-first machinery.

Fail-closed the same way as every other reader in this codebase: an
equipment/port/connection the model's own `EngineeringSystemModel` validator
rejects (unknown reference, incompatible medium/direction, overloaded port
cardinality) is excluded individually and reported, never silently dropped
or guessed into compliance.

First run on real P&IDs (2026-09-25, three Wikimedia sheets, see
`test-drawings/SOURCES.md`): not a single connection was built -- the prompt
asked for "medium with parameter" and the two ends of one line came back as
different strings, which the validator compares verbatim. Now the medium is
the substance only (`canonical_medium`), parameters go to `line`, and a port
may branch through a tee (`branches`). Recall is still low and unstable
between runs (3..10 of ~12 connections on the pump/tank sheet): connectivity
should be traced on the sheet, the model reading only symbols and tags.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.ai.cad_recognize.spec_vectorize import SpecEvidence

SystemReadProfile = Literal["mep", "electrical", "hydraulic", "pid"]


class EquipmentRead(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=300)
    equipment_type: str = Field(min_length=1, max_length=160)
    evidence: list[SpecEvidence] = Field(default_factory=list)


class PortRead(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    equipment_id: str = Field(min_length=1, max_length=160)
    kind: str = Field(min_length=1, max_length=160)
    direction: Literal["in", "out", "bidirectional"]
    medium: str = Field(min_length=1, max_length=160)
    nominal_size_mm: float | None = Field(default=None, gt=0)
    required_connection: bool = True
    # Номер линии и параметры среды (температура, напряжение, расход) —
    # отдельно от самой среды: валидатор модели сравнивает среды концов связи.
    line: str | None = Field(default=None, max_length=300)
    # Сколько линий отходит от вывода на листе: штуцер бака через тройник
    # питает два насоса (живой P&ID RI Sample) — вторая ветвь отбрасывалась
    # как «перегрузка порта». Читается с листа, по умолчанию одна.
    branches: int = Field(default=1, ge=1, le=20)
    evidence: list[SpecEvidence] = Field(default_factory=list)


class ConnectionRead(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    first_port_id: str = Field(min_length=1, max_length=160)
    second_port_id: str = Field(min_length=1, max_length=160)
    evidence: list[SpecEvidence] = Field(default_factory=list)


class SystemSheetRead(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    system_kind: str = Field(min_length=1, max_length=160)
    equipment: list[EquipmentRead] = Field(default_factory=list, max_length=2000)
    ports: list[PortRead] = Field(default_factory=list, max_length=10_000)
    connections: list[ConnectionRead] = Field(default_factory=list, max_length=20_000)


_SYSTEM_PROMPT = """Ты читаешь инженерную схему — P&ID / MEP / электрическую /
гидравлическую (принципиальную или монтажную).

Верни СТРОГО JSON без markdown-блоков и без пояснений — ТОЛЬКО то, что реально
показано на схеме (условные обозначения, подписи, линии связи и стрелки
направления потока):

{
  "name": "Схема отопления, эт. 1",
  "system_kind": "отопление",
  "equipment": [
    {"id": "e1", "name": "Котёл К1", "equipment_type": "boiler"}
  ],
  "ports": [
    {
      "id": "p1", "equipment_id": "e1", "kind": "supply",
      "direction": "out", "medium": "вода", "line": "Т1, 80°C",
      "nominal_size_mm": 32, "required_connection": true, "branches": 1
    }
  ],
  "connections": [
    {"id": "c1", "first_port_id": "p1", "second_port_id": "p2"}
  ]
}

ВАЖНЫЕ ПРАВИЛА:
- equipment — каждый отдельный аппарат/прибор/арматура на схеме со своим
  условным обозначением (котёл, насос, клапан, щит, розетка и т.п.).
- ports — каждый патрубок/вывод/клемма оборудования, который реально
  подключается линией на схеме. direction читай по стрелке потока на линии
  (если стрелки нет и это заведомо двусторонний узел — bidirectional).
  medium — ТОЛЬКО вещество или вид энергии одним-двумя словами (вода, пар,
  воздух, растворитель, электричество, сигнал) и одинаково у обоих концов
  одной линии. Номер линии, температура, напряжение, расход — в "line"
  (например "02-100-PE-N", "80°C", "220В"). branches — сколько линий
  реально отходит от вывода (линия ветвится тройником — число ветвей),
  по умолчанию 1.
- connections — только линии связи, которые ТЫ ДЕЙСТВИТЕЛЬНО можешь
  проследить на схеме от одного порта до другого. Если линия обрывается,
  уходит за рамку листа или пересечение неоднозначно — не соединяй порты
  вслепую, просто не включай эту связь.
- Не добавляй оборудование, порты или связи, которых на листе нет."""


def system_read_as_model(
    sheet: SystemSheetRead,
    *,
    profile: SystemReadProfile,
) -> tuple[Any | None, dict[str, Any]]:
    """Deterministic assembly from a read sheet -- reuses
    `EngineeringSystemModel`'s OWN validator as the fail-closed gate (adding
    connections one at a time and keeping only those that keep the whole
    model valid) instead of re-implementing its compatibility/cardinality
    rules a second time."""
    from app.ai.system_emg import (
        EngineeringSystemModel,
        SystemConnection,
        SystemEquipment,
        SystemPort,
    )

    skipped: list[dict[str, Any]] = []
    equipment = [
        SystemEquipment(id=item.id, name=item.name, equipment_type=item.equipment_type)
        for item in sheet.equipment
    ]
    equipment_ids = {item.id for item in equipment}

    ports: list[SystemPort] = []
    port_ids: set[str] = set()
    medium_details: dict[str, str] = {}
    for item in sheet.ports:
        if item.equipment_id not in equipment_ids:
            skipped.append({"id": item.id, "kind": "port", "reason": "unknown_equipment"})
            continue
        if item.id in port_ids:
            skipped.append({"id": item.id, "kind": "port", "reason": "duplicate_id"})
            continue
        port_ids.add(item.id)
        if canonical_medium(item.medium) != item.medium.strip() or item.line:
            medium_details[item.id] = "; ".join(
                part for part in (item.medium.strip(), item.line) if part
            )
        ports.append(
            SystemPort(
                id=item.id,
                equipment_id=item.equipment_id,
                kind=item.kind,
                direction=item.direction,
                medium=canonical_medium(item.medium),
                nominal_size_mm=item.nominal_size_mm,
                required_connection=item.required_connection,
                max_connections=item.branches,
            )
        )

    try:
        base = EngineeringSystemModel(
            profile=profile,
            name=sheet.name,
            system_kind=sheet.system_kind,
            equipment=equipment,
            ports=ports,
            connections=[],
        )
    except ValidationError as exc:
        return None, {
            "blocked": True,
            "blocked_reason": "system_model_validation_failed",
            "validation_error": str(exc),
            "skipped": skipped,
        }

    accepted: list[SystemConnection] = []
    for item in sheet.connections:
        if item.first_port_id not in port_ids or item.second_port_id not in port_ids:
            skipped.append({"id": item.id, "kind": "connection", "reason": "unknown_port"})
            continue
        candidate = [
            *accepted,
            SystemConnection(
                id=item.id, first_port_id=item.first_port_id, second_port_id=item.second_port_id
            ),
        ]
        try:
            EngineeringSystemModel(
                profile=profile,
                name=sheet.name,
                system_kind=sheet.system_kind,
                equipment=equipment,
                ports=ports,
                connections=candidate,
            )
        except ValidationError as exc:
            skipped.append(
                {
                    "id": item.id,
                    "kind": "connection",
                    "reason": "incompatible_or_overloaded",
                    "detail": str(exc)[:200],
                }
            )
            continue
        accepted = candidate

    model = EngineeringSystemModel(
        profile=profile,
        name=sheet.name,
        system_kind=sheet.system_kind,
        equipment=equipment,
        ports=ports,
        connections=accepted,
    )
    del base  # only needed to validate equipment/ports before connections
    report: dict[str, Any] = {
        "equipment_built": len(equipment),
        "ports_built": len(ports),
        "connections_read": len(sheet.connections),
        "connections_built": len(accepted),
        "unresolved_required_ports": model.unresolved_port_ids(),
        "medium_details": medium_details,
        "skipped": skipped,
    }
    return model, report


def canonical_medium(text: str) -> str:
    """Среда порта — вещество, без параметров и номера линии.

    Живые P&ID (2026-09-25): модель писала у концов одной линии
    «растворитель (solvent) из UNIT 1, линия 01-100-PE-N» и «растворитель
    (solvent)»; валидатор сравнивает среды строками, и ни одна связь на трёх
    реальных листах не построилась. Отрезается всё после скобки, запятой или
    точки с запятой — вещество «вода» и «пар» остаются разными.
    """
    import re

    head = re.split(r"[(,;]", text, maxsplit=1)[0]
    return " ".join(head.split()) or text.strip()


async def _read_sheet_via_vlm(
    image_bytes: bytes,
    *,
    router: Any | None,
    confidential: bool,
    allow_cloud: bool,
) -> SystemSheetRead | None:
    import base64

    from app.ai.cad_recognize.spec_vectorize import _parse_spec_json
    from app.ai.schemas import AIRequest, AITask, ChatMessage

    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        messages=[
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(role="user", content="Прочитай схему с этого листа."),
        ],
        images=[base64.b64encode(image_bytes).decode()],
        confidential=confidential,
        allow_cloud=allow_cloud,
    )
    try:
        response = await router.run(request)
    except Exception:  # noqa: BLE001 -- a router failure fails closed below
        return None
    parsed = _parse_spec_json(response.text or "")
    if not parsed:
        return None
    try:
        return SystemSheetRead.model_validate(parsed)
    except ValidationError:
        return None


async def read_system_diagram(
    image_bytes: bytes,
    *,
    profile: SystemReadProfile,
    router: Any | None = None,
    confidential: bool = True,
    allow_cloud: bool = False,
) -> tuple[Any | None, dict[str, Any]]:
    """Ф5.3 public entry point: image bytes -> (EngineeringSystemModel | None, report)."""
    sheet = await _read_sheet_via_vlm(
        image_bytes, router=router, confidential=confidential, allow_cloud=allow_cloud
    )
    if sheet is None:
        return None, {"read_failed": True}
    return system_read_as_model(sheet, profile=profile)
