"""EDG-028: read-only supported-scope endpoint (no database or model access)."""

import json

from fastapi.testclient import TestClient

from agent.supported_rules import SUPPORTED_MECHANICS, UNSUPPORTED_MECHANICS
from apps.api.main import app


def test_supported_scope_endpoint_is_read_only_and_internal_free() -> None:
    client = TestClient(app)
    response = client.get("/api/rules/supported-scope")
    assert response.status_code == 200
    body = response.json()
    assert body["contract_id"] == "edgar.supported-5e-subset"
    assert {m["id"] for m in body["engine_executed"]} == {m.id for m in SUPPORTED_MECHANICS}
    assert {m["id"] for m in body["not_supported"]} == {m.id for m in UNSUPPORTED_MECHANICS}
    assert body["solo_adaptations"] and "subdued" in json.dumps(body["solo_adaptations"])
    serialised = json.dumps(body)
    assert "tests/" not in serialised and "EDG-" not in serialised
    assert client.post("/api/rules/supported-scope", json={}).status_code == 405
