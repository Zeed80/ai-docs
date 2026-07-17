"""G4: CAD pipeline/solver/export metrics exist and are wired."""

import pytest


def test_cad_metrics_declared():
    from app.core import metrics

    for name in (
        "cad_digitize_total", "cad_digitize_duration_seconds",
        "cad_export_total", "cad_solver_runs_total", "cad_kernel_compile_total",
    ):
        assert hasattr(metrics, name), name


@pytest.mark.asyncio
async def test_solver_run_increments_metric(client):
    from app.core import metrics

    material = (await client.post("/api/engineering/materials", json={
        "designation": "G4 сталь", "yield_strength_mpa": 300,
    })).json()
    project = (await client.post("/api/engineering/projects", json={"name": "G4"})).json()
    revision = (await client.post(f"/api/engineering/projects/{project['id']}/revisions", json={"base_revision": None})).json()
    case = (await client.post(f"/api/engineering/revisions/{revision['id']}/analysis-cases", json={
        "name": "осевое", "material_id": material["id"],
        "inputs": {"force_n": 100, "area_mm2": 10},
    })).json()

    def _count() -> float:
        try:
            return metrics.cad_solver_runs_total.labels(
                analysis_type="axial_stress", status="passed"
            )._value.get()
        except AttributeError:  # prometheus_client not installed → noop
            return -1.0

    before = _count()
    run = await client.post(f"/api/engineering/analysis-cases/{case['id']}/run")
    assert run.status_code == 200
    if before >= 0:
        assert _count() == before + 1


@pytest.mark.asyncio
async def test_export_increments_metric(client, db_session):
    from app.core import metrics
    from app.db.models import ImageGeneration
    from app.storage import upload_file

    gen = ImageGeneration(
        operation="techdraw", params={"dxf_path": "test/g4.dxf"}, owner_sub="dev-user"
    )
    db_session.add(gen)
    await db_session.commit()
    upload_file(b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n", "test/g4.dxf", "application/dxf")

    def _count() -> float:
        try:
            return metrics.cad_export_total.labels(kind="dxf", status="ok")._value.get()
        except AttributeError:
            return -1.0

    before = _count()
    resp = await client.get(f"/api/image-gen/{gen.id}/artifact?kind=dxf")
    assert resp.status_code == 200
    if before >= 0:
        assert _count() == before + 1
