"""Static checks for the optional log shipper, its edge filter and the log alert examples (EDG-063).

Nothing here runs Docker, Vector or a sink. These tests keep the examples structurally sound and in
step with the application's redaction list.
"""

import re
import tomllib
from pathlib import Path

import yaml

from apps.api.logging_config import FORBIDDEN_LOG_FIELDS

REPO = Path(__file__).resolve().parents[2]
OVERRIDE = REPO / "docker-compose.logging.yml"
BASE_COMPOSE = REPO / "docker-compose.yml"
VECTOR = REPO / "ops" / "logging" / "vector.toml"
LOG_ALERTS = REPO / "ops" / "alerts.logs.example.yml"


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_log_shipper_is_an_opt_in_profile_with_no_published_ports() -> None:
    override = _load_yaml(OVERRIDE)
    assert set(override["services"]) == {"log_shipper"}
    shipper = override["services"]["log_shipper"]
    assert shipper["profiles"] == ["logging"]
    for forbidden_key in ("ports", "expose", "network_mode", "privileged", "pid", "ipc"):
        assert forbidden_key not in shipper, forbidden_key


def test_log_shipper_is_read_only_and_hardened() -> None:
    shipper = _load_yaml(OVERRIDE)["services"]["log_shipper"]
    assert shipper["read_only"] is True
    assert shipper["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in shipper["security_opt"]
    assert ":latest" not in shipper["image"] and ":" in shipper["image"]


def test_log_shipper_mounts_logs_read_only_and_config_read_only() -> None:
    shipper = _load_yaml(OVERRIDE)["services"]["log_shipper"]
    volumes = shipper["volumes"]
    assert "/var/lib/docker/containers:/var/lib/docker/containers:ro" in volumes
    assert "./ops/logging/vector.toml:/etc/vector/vector.toml:ro" in volumes
    assert any(v.startswith("vector_data:") for v in volumes)
    assert "vector_data" in _load_yaml(OVERRIDE)["volumes"]
    # Only the Docker logs and the shipper's own state are writable or readable from the host.
    for volume in volumes:
        assert not volume.startswith("/:") and not volume.startswith("/var/run/docker.sock")


def test_log_shipper_requires_sink_settings_and_rotates_its_own_logs() -> None:
    shipper = _load_yaml(OVERRIDE)["services"]["log_shipper"]
    env = shipper["environment"]
    assert env["EDGAR_LOG_SINK_URL"].startswith("${EDGAR_LOG_SINK_URL:?")
    assert env["EDGAR_LOG_SINK_TOKEN"].startswith("${EDGAR_LOG_SINK_TOKEN:?")
    assert shipper["logging"]["options"] == {"max-size": "10m", "max-file": "3"}


def test_override_does_not_change_default_stack_services() -> None:
    base = _load_yaml(BASE_COMPOSE)
    assert "log_shipper" not in base["services"]


def test_vector_config_is_valid_toml_with_one_pipeline() -> None:
    config = tomllib.loads(VECTOR.read_text(encoding="utf-8"))
    assert set(config) >= {"data_dir", "sources", "transforms", "sinks"}
    assert list(config["sources"]) == ["docker_json_logs"]
    assert list(config["transforms"]) == ["edgar_lines"]
    assert list(config["sinks"]) == ["protected_archive"]
    sink = config["sinks"]["protected_archive"]
    assert sink["inputs"] == ["edgar_lines"]
    assert sink["uri"] == "${EDGAR_LOG_SINK_URL}"  # placeholder only, never a literal host
    assert sink["auth"]["token"] == "${EDGAR_LOG_SINK_TOKEN}"
    assert sink["buffer"]["when_full"] == "block"


def test_vector_edge_filter_removes_exactly_the_application_denylist() -> None:
    source = tomllib.loads(VECTOR.read_text(encoding="utf-8"))["transforms"]["edgar_lines"][
        "source"
    ]
    removed = re.findall(r"^del\(\.((?:extra\.|loguru_extra\.)?)([a-z_]+)\)$", source, re.M)
    by_scope: dict[str, set[str]] = {"": set(), "extra.": set(), "loguru_extra.": set()}
    for scope, key in removed:
        by_scope[scope].add(key)
    for scope, keys in by_scope.items():
        assert keys == FORBIDDEN_LOG_FIELDS, scope


def test_vector_drops_non_json_lines_instead_of_shipping_text() -> None:
    source = tomllib.loads(VECTOR.read_text(encoding="utf-8"))["transforms"]["edgar_lines"]
    assert source["drop_on_error"] is True and source["drop_on_abort"] is True
    assert "parse_json!(.message)" in source["source"]
    assert "!exists(line.logger)" in source["source"]


def test_log_alert_examples_are_prometheus_style_groups_with_severity() -> None:
    doc = _load_yaml(LOG_ALERTS)
    rules = [rule for group in doc["groups"] for rule in group["rules"]]
    assert len(rules) >= 4
    for rule in rules:
        assert rule["alert"].startswith("EdgarLog")
        assert rule["labels"]["severity"] in {"info", "warning", "critical"}
        assert rule["annotations"]["summary"]
        assert "{{" not in rule["expr"]  # no template placeholders left in an expression
