"""
Verification test strictly validating submission requirements from SUBMISSION.md.
"""

import pytest
import requests
import json

BASE_URL = "http://localhost:8000"


def test_submission_health_endpoint():
    """Requirement 2: listens on port 8000 and answers GET /health with 200."""
    r = requests.get(f"{BASE_URL}/health", timeout=5)
    assert r.status_code == 200
    data = r.json()
    assert data.get("status") == "ok"


def test_submission_messages_and_trace_and_ops_endpoints():
    """
    Requirements 4, 5, 6:
    - POST /messages returns {"reply": "..."}
    - GET /trace/{rider_id} returns list of objects with at, type, name, input, output
    - GET /ops/pending returns list of objects with id, rider_id, type, amount, reason, created_at
    """
    # 1. Reset desk
    requests.post(f"{BASE_URL}/admin/reset", timeout=5)

    # 2. Test Approval scenario (R016)
    payload_approval = {
        "message_id": "wamid.TEST_SUBMISSION_APP",
        "rider_id": "R016",
        "text": "19 sept ke 5 orders ka paisa hi nahi aaya!! jaldi karo",
        "received_at": "2026-09-22T11:30:00+05:30"
    }
    r_msg = requests.post(f"{BASE_URL}/messages", json=payload_approval, timeout=30)
    assert r_msg.status_code == 200
    msg_data = r_msg.json()
    assert "reply" in msg_data
    assert isinstance(msg_data["reply"], str)

    # 3. Test Escalation scenario (R036)
    payload_escalation = {
        "message_id": "wamid.TEST_SUBMISSION_ESC",
        "rider_id": "R036",
        "text": "order T827192 pe distance galat laga tha, map pe 7km dikha raha tha par 3km ka paisa mila",
        "received_at": "2026-09-22T17:00:00+05:30"
    }
    r_esc = requests.post(f"{BASE_URL}/messages", json=payload_escalation, timeout=30)
    assert r_esc.status_code == 200
    assert "reply" in r_esc.json()

    # 4. Check Trace endpoint shape (GET /trace/{rider_id})
    r_trace = requests.get(f"{BASE_URL}/trace/R016", timeout=5)
    assert r_trace.status_code == 200
    traces = r_trace.json()
    assert isinstance(traces, list)
    assert len(traces) > 0

    required_trace_keys = {"at", "type", "name", "input", "output"}
    valid_trace_types = {"message_in", "tool_call", "decision", "reply", "error", "nlu"}

    for step in traces:
        assert required_trace_keys.issubset(step.keys()), f"Missing keys in trace: {step}"
        assert step["type"] in valid_trace_types, f"Invalid step type: {step['type']}"
        assert isinstance(step["at"], str)
        assert isinstance(step["name"], str)

    # 5. Check Ops Pending endpoint shape (GET /ops/pending)
    r_ops = requests.get(f"{BASE_URL}/ops/pending", timeout=5)
    assert r_ops.status_code == 200
    ops_items = r_ops.json()
    assert isinstance(ops_items, list)
    assert len(ops_items) >= 2

    required_ops_keys = {"id", "rider_id", "type", "amount", "reason", "created_at"}
    approval_found = False
    escalation_found = False

    for item in ops_items:
        assert required_ops_keys.issubset(item.keys()), f"Missing keys in ops item: {item}"
        assert item["type"] in {"approval", "escalation"}
        assert isinstance(item["id"], str)
        assert isinstance(item["rider_id"], str)
        assert isinstance(item["reason"], str)
        assert isinstance(item["created_at"], str)

        if item["type"] == "approval":
            assert isinstance(item["amount"], int)
            approval_found = True
        elif item["type"] == "escalation":
            assert item["amount"] is None
            escalation_found = True

    assert approval_found, "Ops pending must contain at least one approval item"
    assert escalation_found, "Ops pending must contain at least one escalation item"

