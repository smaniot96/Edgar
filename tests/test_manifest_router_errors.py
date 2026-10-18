"""The manifest review routes never relay I/O or lookup error text to the client (EDG-156)."""

import pytest
from fastapi import HTTPException

from apps.api.routers import adventure_manifests as router


def _raise(exc):
    def action(*_a, **_kw):
        raise exc

    return action


@pytest.fixture(autouse=True)
def _public_source(monkeypatch):
    monkeypatch.setattr("apps.api.services.campaign_sources.validate_binding", lambda slug: None)
    monkeypatch.setattr("apps.api.services.preparation_privacy.require_public_source", lambda slug: None)


@pytest.mark.parametrize("exc", [OSError("/data/adventures/secret_path.json: denied"), KeyError("internal_key")])
def test_io_and_lookup_errors_get_a_fixed_safe_message(exc):
    with pytest.raises(HTTPException) as info:
        router._call(_raise(exc), "some_slug")
    assert info.value.status_code == 409
    message = info.value.detail["message"]
    assert "secret_path" not in message and "internal_key" not in message
    assert "could not be read" in message


def test_validation_messages_remain_available_to_the_host():
    with pytest.raises(HTTPException) as info:
        router._call(_raise(ValueError("2 passages are still unreviewed.")), "some_slug")
    assert info.value.detail["message"] == "2 passages are still unreviewed."


def test_unverified_source_gets_its_own_code_and_a_next_step():
    from apps.api.services.adventure_manifests import ManifestSourceUnverified

    with pytest.raises(HTTPException) as info:
        router._call(
            _raise(ManifestSourceUnverified("Source kind is unverified. Recover its canonical extraction first.")),
            "some_slug",
        )
    assert info.value.status_code == 409
    assert info.value.detail["code"] == "adventure_source_unverified"
    assert "Recover its canonical extraction" not in info.value.detail["message"]
    assert "no verified source record" in info.value.detail["message"]


def test_unverified_source_reaches_the_host_through_the_safe_error_boundary():
    from apps.api.services.safe_errors import classify_http_failure, http_failure_payload

    failure = classify_http_failure(
        409, code="adventure_source_unverified", outcome="not_committed"
    )
    payload = http_failure_payload(failure, context={})
    assert payload["detail"]["code"] == "adventure_source_unverified"
    assert "no verified source record" in payload["detail"]["message"]
    assert "retry" not in payload["detail"]["message"].lower()
