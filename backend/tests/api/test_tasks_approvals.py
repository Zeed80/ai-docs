from __future__ import annotations


def test_document_process_is_queued_and_run_next_executes(client) -> None:
    case = client.post("/api/cases", json={"title": "Queued processing"}).json()
    document = client.post(
        f"/api/cases/{case['id']}/documents",
        files={"file": ("invoice.txt", b"Invoice queued total 1000", "text/plain")},
    ).json()

    queued_response = client.post(f"/api/documents/{document['id']}/process")

    assert queued_response.status_code == 200
    queued = queued_response.json()
    assert queued["task_type"] == "document.process"
    assert queued["status"] == "pending"

    run_response = client.post("/api/tasks/run-next")

    assert run_response.status_code == 200
    executed = run_response.json()
    assert executed["id"] == queued["id"]
    assert executed["status"] == "completed"
    assert executed["result"]["document_status"] == "processed"


def test_approval_gate_can_be_approved_and_executed(client) -> None:
    case = client.post("/api/cases", json={"title": "Approval execution"}).json()
    scenario_response = client.post(
        "/api/agent/scenarios/draft_email/run",
        json={
            "case_id": case["id"],
            "draft_id": "draft-1",
            "requested_tools": ["email.send.request_approval"],
        },
    )
    gate = scenario_response.json()["approval_gates"][0]

    list_response = client.get(f"/api/approvals?case_id={case['id']}")
    assert list_response.status_code == 200
    assert list_response.json()[0]["id"] == gate["id"]

    approve_response = client.post(
        f"/api/approvals/{gate['id']}/approve",
        json={"actor": "tester", "reason": "Approved in test"},
    )

    assert approve_response.status_code == 200
    decision = approve_response.json()
    assert decision["approval_gate"]["status"] == "approved"
    assert decision["task"]["task_type"] == "email.send.request_approval"

    run_response = client.post(f"/api/tasks/{decision['task']['id']}/run")
    assert run_response.status_code == 200
    assert run_response.json()["status"] == "completed"

    audit_response = client.get(f"/api/cases/{case['id']}/audit")
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "approval_gate_approved" in event_types
    assert "approval_gate_executed" in event_types


def test_approval_gate_can_be_rejected(client) -> None:
    case = client.post("/api/cases", json={"title": "Approval reject"}).json()
    scenario_response = client.post(
        "/api/agent/scenarios/draft_email/run",
        json={
            "case_id": case["id"],
            "draft_id": "draft-2",
            "requested_tools": ["email.send.request_approval"],
        },
    )
    gate = scenario_response.json()["approval_gates"][0]

    reject_response = client.post(
        f"/api/approvals/{gate['id']}/reject",
        json={"actor": "tester", "reason": "Rejected in test"},
    )

    assert reject_response.status_code == 200
    assert reject_response.json()["approval_gate"]["status"] == "rejected"

    audit_response = client.get(f"/api/cases/{case['id']}/audit")
    event_types = [event["event_type"] for event in audit_response.json()]
    assert "approval_gate_rejected" in event_types
