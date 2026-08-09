"""Strict construction snapshot projection into EngineeringModelGraph."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.engineering_model_graph import (
    Assertion,
    BuildTarget,
    EngineeringModelGraph,
    Evidence,
    ExactValue,
    GraphEdge,
    GraphNode,
    Requirement,
    UnknownValue,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConstructionBox(_StrictModel):
    x_mm: float
    y_mm: float
    z_mm: float
    width_mm: float = Field(gt=0)
    depth_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)


class ConstructionStorey(_StrictModel):
    id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=300)
    elevation_mm: float


class _Element(_StrictModel):
    id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=300)
    storey_id: str = Field(min_length=1, max_length=160)
    box: ConstructionBox
    material: str | None = Field(default=None, max_length=300)


class WallElement(_Element):
    kind: Literal["wall"]
    load_bearing: bool = False


class SlabElement(_Element):
    kind: Literal["slab"]
    load_bearing: bool = True


class ColumnElement(_Element):
    kind: Literal["column"]
    load_bearing: bool = True


class SpaceElement(_Element):
    kind: Literal["space"]


class OpeningElement(_Element):
    kind: Literal["opening"]
    host_id: str = Field(min_length=1, max_length=160)


ConstructionElement = Annotated[
    WallElement | SlabElement | ColumnElement | SpaceElement | OpeningElement,
    Field(discriminator="kind"),
]


class ConstructionModel(_StrictModel):
    site_name: str = Field(min_length=1, max_length=300)
    building_name: str = Field(min_length=1, max_length=300)
    storeys: list[ConstructionStorey] = Field(min_length=1, max_length=500)
    elements: list[ConstructionElement] = Field(min_length=1, max_length=20_000)

    @model_validator(mode="after")
    def validate_hierarchy(self) -> "ConstructionModel":
        storey_ids = [item.id for item in self.storeys]
        element_ids = [item.id for item in self.elements]
        if len(storey_ids) != len(set(storey_ids)):
            raise ValueError("duplicate construction storey ids")
        if len(element_ids) != len(set(element_ids)):
            raise ValueError("duplicate construction element ids")
        storey_set = set(storey_ids)
        if missing := sorted({item.storey_id for item in self.elements} - storey_set):
            raise ValueError("unknown construction storeys: " + ", ".join(missing))
        by_id = {item.id: item for item in self.elements}
        for item in self.elements:
            if item.kind != "opening":
                continue
            host = by_id.get(item.host_id)
            if host is None or host.kind not in {"wall", "slab"}:
                raise ValueError(f"opening {item.id} must reference a wall or slab host")
            if host.storey_id != item.storey_id:
                raise ValueError(f"opening {item.id} and host must share a storey")
            if not _box_contains(host.box, item.box):
                raise ValueError(f"opening {item.id} lies outside host {host.id}")
        return self


def _box_contains(outer: ConstructionBox, inner: ConstructionBox) -> bool:
    tolerance = 1e-6
    return all(
        inner_min >= outer_min - tolerance and inner_max <= outer_max + tolerance
        for outer_min, outer_max, inner_min, inner_max in (
            (outer.x_mm, outer.x_mm + outer.width_mm, inner.x_mm, inner.x_mm + inner.width_mm),
            (outer.y_mm, outer.y_mm + outer.depth_mm, inner.y_mm, inner.y_mm + inner.depth_mm),
            (outer.z_mm, outer.z_mm + outer.height_mm, inner.z_mm, inner.z_mm + inner.height_mm),
        )
    )


def _stable(prefix: str, value: str) -> str:
    return f"{prefix}:{hashlib.sha256(value.encode()).hexdigest()[:16]}"


def construction_as_graph(
    *,
    graph_id: str,
    model: ConstructionModel,
    source_revision_id: str,
    source_approved: bool,
) -> EngineeringModelGraph:
    """Project one immutable construction source revision into canonical EMG."""
    source_assurance = "human_approved" if source_approved else "observed"
    source_evidence = Evidence(
        id=_stable("evidence:construction-source", source_revision_id),
        kind="human_decision",
        payload={
            "engineering_revision_id": source_revision_id,
            "approved": source_approved,
        },
        sha256=hashlib.sha256(source_revision_id.encode()).hexdigest(),
    )
    building_id = "product:building"
    site_id = "component:site"
    nodes = [
        GraphNode(id=building_id, type="Product", name=model.building_name),
        GraphNode(id=site_id, type="Component", name=model.site_name),
    ]
    edges = [
        GraphEdge(id="contains:building:site", type="contains", source_id=building_id, target_id=site_id)
    ]
    assertions: list[Assertion] = []
    required: list[str] = []
    storey_nodes: dict[str, str] = {}
    element_nodes: dict[str, str] = {}

    for storey in model.storeys:
        node_id = _stable("component:storey", storey.id)
        storey_nodes[storey.id] = node_id
        nodes.append(GraphNode(id=node_id, type="Component", name=storey.name))
        edges.append(GraphEdge(
            id=_stable("contains:site", storey.id),
            type="contains",
            source_id=site_id,
            target_id=node_id,
        ))
        assertion_id = _stable("assertion:spatial-level", storey.id)
        assertions.append(Assertion(
            id=assertion_id,
            subject_id=node_id,
            predicate="spatial.level",
            value=ExactValue(kind="exact", value=storey.elevation_mm),
            unit="mm",
            coordinate_system=building_id,
            origin="human",
            assurance=source_assurance,
            evidence_ids=[source_evidence.id],
            confidence=1.0,
            impacts=["envelope", "load_path"],
        ))
        required.append(assertion_id)

    for element in model.elements:
        node_id = _stable("feature:construction", element.id)
        element_nodes[element.id] = node_id
        nodes.append(GraphNode(id=node_id, type="Feature", name=element.name))
        edges.append(GraphEdge(
            id=_stable("located:construction", element.id),
            type="located_in",
            source_id=node_id,
            target_id=storey_nodes[element.storey_id],
        ))
        operation_id = _stable("operation:ifc", element.id)
        nodes.append(GraphNode(id=operation_id, type="BuildOperation", name=f"Build {element.kind}"))
        edges.append(GraphEdge(
            id=_stable("depends:ifc", element.id),
            type="depends_on",
            source_id=operation_id,
            target_id=node_id,
        ))
        impact = "connection_opening" if element.kind == "opening" else "base_topology"
        for predicate, value, unit in (
            ("element.kind", element.kind, None),
            ("geometry.box", element.box.model_dump(mode="json"), "mm"),
        ):
            assertion_id = _stable(f"assertion:{predicate}", element.id)
            assertions.append(Assertion(
                id=assertion_id,
                subject_id=node_id,
                predicate=predicate,
                value=ExactValue(kind="exact", value=value),
                unit=unit,
                coordinate_system=building_id if predicate == "geometry.box" else None,
                origin="human",
                assurance=source_assurance,
                evidence_ids=[source_evidence.id],
                confidence=1.0,
                impacts=[impact, "envelope"],
            ))
            required.append(assertion_id)
        if element.kind in {"wall", "slab", "column"}:
            assertion_id = _stable("assertion:element-material", element.id)
            assertions.append(Assertion(
                id=assertion_id,
                subject_id=node_id,
                predicate="element.material",
                value=(
                    ExactValue(kind="exact", value=element.material)
                    if element.material
                    else UnknownValue(kind="unknown", reason="construction material is missing")
                ),
                origin="human",
                assurance=source_assurance if element.material else "proposed",
                evidence_ids=[source_evidence.id] if element.material else [],
                confidence=1.0 if element.material else 0.0,
                impacts=["structural_capacity", "material_quantity"],
            ))
            required.append(assertion_id)

    for element in model.elements:
        if element.kind == "opening":
            edges.append(GraphEdge(
                id=_stable("opens-in", element.id),
                type="opens_in",
                source_id=element_nodes[element.id],
                target_id=element_nodes[element.host_id],
            ))

    contained_id = "assertion:construction:openings-contained"
    reopen_id = "assertion:construction:ifc-reopen"
    required_2d_id = "assertion:construction:required-sheets"
    assertions.extend([
        Assertion(
            id=contained_id,
            subject_id=building_id,
            predicate="construction.openings_contained",
            value=ExactValue(kind="exact", value=True),
            origin="derived",
            assurance="constraint_validated",
            confidence=1.0,
            impacts=["connection_opening", "operational_safety"],
        ),
        Assertion(
            id=reopen_id,
            subject_id=building_id,
            predicate="construction.ifc_reopen_valid",
            value=UnknownValue(kind="unknown", reason="IFC builder has not reopened an artifact"),
            origin="derived",
            assurance="proposed",
            impacts=["base_topology", "regulatory_check"],
        ),
        Assertion(
            id=required_2d_id,
            subject_id=building_id,
            predicate="construction.required_sheets_complete",
            value=UnknownValue(
                kind="unknown",
                reason="mandatory plans and sections have not been verified",
            ),
            origin="derived",
            assurance="proposed",
            impacts=["required_view", "required_section"],
        ),
    ])
    required.extend([contained_id, reopen_id, required_2d_id])
    requirement = Requirement(
        id="requirement:construction-release",
        kind="domain",
        target_node_ids=[building_id],
        assertion_ids=required,
    )
    sheet_requirement = Requirement(
        id="requirement:construction-2d",
        kind="view",
        target_node_ids=[building_id],
        assertion_ids=[required_2d_id],
    )
    return EngineeringModelGraph(
        graph_id=graph_id,
        profile="construction",
        nodes=nodes,
        edges=edges,
        assertions=assertions,
        evidence=[source_evidence],
        requirements=[requirement, sheet_requirement],
        build_targets=[
            BuildTarget(id="preview", kind="preview_ifc", root_node_ids=[building_id], critical_impacts=[]),
            BuildTarget(
                id="production",
                kind="production_ifc",
                root_node_ids=[building_id],
                requirement_ids=[requirement.id, sheet_requirement.id],
            ),
        ],
    ).sealed()


def _ifc_guid(ifcopenshell_module: object, seed: str) -> str:
    digest = hashlib.sha256(seed.encode()).digest()[:16]
    return ifcopenshell_module.guid.compress(uuid.UUID(bytes=digest).hex)  # type: ignore[attr-defined]


def compile_construction_ifc(model: ConstructionModel) -> tuple[bytes, dict]:
    """Build and independently reopen a deterministic IFC4 box-model.

    IfcOpenShell is imported lazily so graph reading and unit tests do not need
    the heavyweight geometry runtime installed on the host.
    """
    import ifcopenshell
    import ifcopenshell.geom
    import numpy
    from ifcopenshell.api.aggregate import assign_object
    from ifcopenshell.api.context import add_context
    from ifcopenshell.api.feature import add_feature
    from ifcopenshell.api.geometry import (
        add_wall_representation,
        assign_representation,
        edit_object_placement,
    )
    from ifcopenshell.api.project import create_file
    from ifcopenshell.api.root import create_entity
    from ifcopenshell.api.spatial import assign_container
    from ifcopenshell.api.unit import assign_unit

    ifc = create_file(version="IFC4")
    project = create_entity(ifc, ifc_class="IfcProject", name=model.building_name)
    assign_unit(ifc)
    model_context = add_context(ifc, context_type="Model")
    body_context = add_context(
        ifc,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=model_context,
    )
    site = create_entity(ifc, ifc_class="IfcSite", name=model.site_name)
    building = create_entity(ifc, ifc_class="IfcBuilding", name=model.building_name)
    assign_object(ifc, products=[site], relating_object=project)
    assign_object(ifc, products=[building], relating_object=site)

    storeys = {}
    for item in model.storeys:
        storey = create_entity(ifc, ifc_class="IfcBuildingStorey", name=item.name)
        storey.Elevation = item.elevation_mm / 1000.0
        assign_object(ifc, products=[storey], relating_object=building)
        matrix = numpy.eye(4)
        matrix[2, 3] = item.elevation_mm / 1000.0
        edit_object_placement(ifc, product=storey, matrix=matrix, is_si=True)
        storeys[item.id] = storey

    ifc_class = {
        "wall": "IfcWall",
        "slab": "IfcSlab",
        "column": "IfcColumn",
        "space": "IfcSpace",
        "opening": "IfcOpeningElement",
    }
    products = {}
    for item in model.elements:
        product = create_entity(ifc, ifc_class=ifc_class[item.kind], name=item.name)
        product.Description = item.id
        if hasattr(product, "Tag"):
            product.Tag = item.id
        box = item.box
        representation = add_wall_representation(
            ifc,
            context=body_context,
            length=box.width_mm / 1000.0,
            height=box.height_mm / 1000.0,
            thickness=box.depth_mm / 1000.0,
        )
        assign_representation(ifc, product=product, representation=representation)
        matrix = numpy.eye(4)
        matrix[:3, 3] = [box.x_mm / 1000.0, box.y_mm / 1000.0, box.z_mm / 1000.0]
        edit_object_placement(ifc, product=product, matrix=matrix, is_si=True)
        products[item.id] = product

    for item in model.elements:
        product = products[item.id]
        if item.kind == "opening":
            add_feature(ifc, feature=product, element=products[item.host_id])
        elif item.kind == "space":
            assign_object(
                ifc,
                products=[product],
                relating_object=storeys[item.storey_id],
            )
        else:
            assign_container(
                ifc,
                products=[product],
                relating_structure=storeys[item.storey_id],
            )

    # root.create_entity and relationship APIs generate random IFC GUIDs.
    # Replace them in deterministic entity order after the model is complete.
    for index, root in enumerate(ifc.by_type("IfcRoot")):
        root.GlobalId = _ifc_guid(
            ifcopenshell,
            f"construction:{index}:{root.is_a()}:{getattr(root, 'Name', '')}",
        )
    # IfcOpenShell relationship APIs collect members through sets.  Their
    # serialization order can change between calls (and therefore change the
    # artifact SHA) even though the building topology is identical.  Canonical
    # member order is part of the immutable graph -> artifact contract.
    def _product_key(product: object) -> tuple[str, str, str, int]:
        return (
            str(getattr(product, "Description", "") or ""),
            str(getattr(product, "Tag", "") or ""),
            str(getattr(product, "Name", "") or ""),
            product.id(),  # type: ignore[attr-defined]
        )

    for relation in ifc.by_type("IfcRelContainedInSpatialStructure"):
        relation.RelatedElements = tuple(sorted(relation.RelatedElements, key=_product_key))
    for relation in ifc.by_type("IfcRelAggregates"):
        relation.RelatedObjects = tuple(sorted(relation.RelatedObjects, key=_product_key))
    for assignment in ifc.by_type("IfcUnitAssignment"):
        assignment.Units = tuple(
            sorted(
                assignment.Units,
                key=lambda unit: (str(getattr(unit, "UnitType", "")), unit.id()),
            )
        )
    ifc.header.file_name.name = "construction.ifc"
    ifc.header.file_name.time_stamp = "1970-01-01T00:00:00"
    ifc.header.file_name.author = ("EngineeringModelGraph",)
    ifc.header.file_name.organization = ("AI Workspace",)
    ifc_bytes = ifc.to_string().encode()

    reopened = ifcopenshell.file.from_string(ifc_bytes.decode())
    roots = reopened.by_type("IfcRoot")
    products_reopened = [
        item for item in reopened.by_type("IfcProduct")
        if getattr(item, "Representation", None)
    ]
    geometry_failures = []
    geometry_settings = ifcopenshell.geom.settings()
    geometry_settings.set(geometry_settings.USE_WORLD_COORDS, True)
    for product in products_reopened:
        try:
            ifcopenshell.geom.create_shape(geometry_settings, product)
        except Exception as exc:  # noqa: BLE001 - every failed product is reported
            geometry_failures.append({
                "ifc_class": product.is_a(),
                "name": getattr(product, "Name", None),
                "error": str(exc)[:300],
            })
    expected_counts = Counter(ifc_class[item.kind] for item in model.elements)
    actual_counts = Counter(item.is_a() for item in products_reopened)
    guids = [str(item.GlobalId) for item in roots if getattr(item, "GlobalId", None)]
    report = {
        "schema": reopened.schema,
        "entity_count": sum(1 for _ in reopened),
        "root_count": len(roots),
        "unique_guid_count": len(set(guids)),
        "project_count": len(reopened.by_type("IfcProject")),
        "site_count": len(reopened.by_type("IfcSite")),
        "building_count": len(reopened.by_type("IfcBuilding")),
        "storey_count": len(reopened.by_type("IfcBuildingStorey")),
        "space_count": len(reopened.by_type("IfcSpace")),
        "opening_relation_count": len(reopened.by_type("IfcRelVoidsElement")),
        "represented_product_count": len(products_reopened),
        "products": [
            {
                "source_id": getattr(item, "Description", None),
                "ifc_class": item.is_a(),
                "global_id": str(item.GlobalId),
                "name": getattr(item, "Name", None),
            }
            for item in products_reopened
        ],
        "product_class_counts": dict(sorted(actual_counts.items())),
        "expected_product_class_counts": dict(sorted(expected_counts.items())),
        "geometry_failures": geometry_failures,
    }
    report["valid"] = bool(
        report["schema"] == "IFC4"
        and report["project_count"] == 1
        and report["site_count"] == 1
        and report["building_count"] == 1
        and report["storey_count"] == len(model.storeys)
        and report["unique_guid_count"] == report["root_count"]
        and actual_counts == expected_counts
        and report["opening_relation_count"]
        == sum(item.kind == "opening" for item in model.elements)
        and not geometry_failures
    )
    report["ifc_sha256"] = hashlib.sha256(ifc_bytes).hexdigest()
    report["canonical_report_sha256"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ifc_bytes, report
