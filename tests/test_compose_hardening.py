"""Static checks for Compose secret scoping, runtime hardening and the hosting profile.

These tests read the Compose files, the Dockerfile and the Python sources. They never start Docker
and never open a network connection, so they run in the plain `edgar-tests` job.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
BASE = REPO / "docker-compose.yml"
DEV = REPO / "docker-compose.dev.yml"
HOSTING = REPO / "docker-compose.hosting.yml"
DOCKERFILE = REPO / "Dockerfile"
CONFIG_PY = REPO / "core" / "src" / "edgar_core" / "config.py"
MODEL_PROFILES_PY = REPO / "core" / "src" / "edgar_core" / "model_profiles.py"

# Source trees that run inside a container. tools/ runs only on the host and is out of scope.
RUNTIME_DIRS = ("core/src", "db", "apps/api", "apps/agent", "apps/ingestion", "apps/rules_engine")

# Names set by the Compose file itself, not interpolated from .env.
LITERALS = {"DB_HOST", "REDIS_HOST", "QDRANT_HOST", "EDGAR_INDEX_LOCK_DIR"}
DATASTORE_COORDS = {"DB_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}
PROVIDER = {
    "OPENAI_API_KEY",
    "VECTOR_DB_API_KEY",
    "LLM_MODEL",
    "LLM_REASONING_EFFORT",
    "EMBEDDING_MODEL",
    "MODEL_PROFILES_PATH",
}
RETRIEVAL = {"QDRANT_PORT"}
NEW_API_SETTINGS = (
    "EDGAR_MEMORY_SUMMARY_CONCURRENCY",
    "EDGAR_ALLOWED_ORIGINS",
    "EDGAR_ALLOW_CREDENTIALS",
    "EDGAR_TRUSTED_HOSTS",
    "EDGAR_AUTH_MODE",
    "EDGAR_ADMIN_USER_IDS",
    "EDGAR_ALLOW_DEBUG_IN_TOKEN_MODE",
    "EDGAR_ALLOW_MULTIPLE_REPLICAS",
    "EDGAR_REPLICA_LOCK_WAIT_SEC",
)

LIMIT_SETTINGS = (
    "LIMIT_TURNS_PER_MINUTE",
    "LIMIT_JOB_STARTS_PER_HOUR",
    "LIMIT_MAX_CONCURRENT_TURNS",
    "LIMIT_MAX_CONCURRENT_TURNS_PER_CLIENT",
    "LIMIT_MAX_CONCURRENT_JOBS",
    "LIMIT_MAX_UPLOAD_BYTES",
    "LIMIT_DAILY_SPEND_MICROUSD",
    "LIMIT_ASSUMED_CALL_COST_MICROUSD",
    "LIMIT_UNBUDGETED_JOB_COST_MICROUSD",
    "LIMIT_TURN_LEASE_SECONDS",
    "LIMIT_JOB_LEASE_SECONDS",
)

API_ONLY = {
    "ENV",
    "REDIS_PORT",
    "EDGAR_DEBUG",
    "EDGAR_METRICS_ENABLED",
    "EDGAR_METRICS_TOKEN",
    "EDGAR_TRUSTED_IMAGE_PRICE_RECORD",
    "EDGAR_VISUAL_REVIEW_PUBLIC_KEY",
    "EDGAR_VISUAL_REVIEW_PRIVATE_KEY",
    *LIMIT_SETTINGS,
    *NEW_API_SETTINGS,
}

# The exact set of names each service receives from the default (base) file. Changing one is a
# deliberate scoping decision, so it must be made here as well as in docker-compose.yml.
EXPECTED_BASE = {
    "migrate": {"DB_HOST", "DB_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"},
    "api": LITERALS | DATASTORE_COORDS | PROVIDER | RETRIEVAL | API_ONLY,
    "ingestion": {"DB_HOST", "QDRANT_HOST", "EDGAR_INDEX_LOCK_DIR"} | PROVIDER | RETRIEVAL,
    "rules_engine": {"DB_HOST", "QDRANT_HOST", "EDGAR_INDEX_LOCK_DIR"} | PROVIDER | RETRIEVAL,
    "db": DATASTORE_COORDS - {"DB_PORT"},
    "redis": set(),
    "qdrant": set(),
}

# Settings read without a default. Each consumer must receive them; nothing else may.
REQUIRED_WITHOUT_DEFAULT = {
    "OPENAI_API_KEY": {"api", "ingestion", "rules_engine"},
    "VECTOR_DB_API_KEY": {"api", "ingestion", "rules_engine"},
}

# Secrets and the only services allowed to receive each one in the default stack.
SECRET_SCOPE = {
    "OPENAI_API_KEY": {"api", "ingestion", "rules_engine"},
    "VECTOR_DB_API_KEY": {"api", "ingestion", "rules_engine"},
    "POSTGRES_PASSWORD": {"api", "migrate", "db"},
    "EDGAR_METRICS_TOKEN": {"api"},
    "EDGAR_VISUAL_REVIEW_PRIVATE_KEY": {"api"},
}

# Names only the hosting profile may add. They must not appear in the default files.
HOSTING_ONLY_NAMES = {"REDIS_PASSWORD", "REDISCLI_AUTH", "QDRANT__SERVICE__API_KEY"}

# What the hosting profile adds per service (merged over the base allow-list).
HOSTING_EXTRA = {
    "api": {"POSTGRES_PASSWORD", "REDIS_PASSWORD", "VECTOR_DB_API_KEY"},
    "migrate": {"POSTGRES_PASSWORD"},
    "ingestion": {"VECTOR_DB_API_KEY"},
    "rules_engine": {"VECTOR_DB_API_KEY"},
    "db": {"POSTGRES_PASSWORD"},
    "redis": {"REDISCLI_AUTH"},
    "qdrant": {"QDRANT__SERVICE__API_KEY"},
}
HOSTING_REQUIRED = {"POSTGRES_PASSWORD", "REDIS_PASSWORD", "VECTOR_DB_API_KEY"}

NONROOT_APP_SERVICES = ("migrate", "api", "ingestion", "rules_engine")
DATASTORE_SERVICES = ("db", "redis", "qdrant")

# Names whose interpolated default is empty. Code must treat an empty value as unset.
EMPTY_DEFAULT_OK = {
    "OPENAI_API_KEY",
    "VECTOR_DB_API_KEY",
    "MODEL_PROFILES_PATH",
    "EDGAR_METRICS_ENABLED",
    "EDGAR_METRICS_TOKEN",
    "EDGAR_TRUSTED_IMAGE_PRICE_RECORD",
    "EDGAR_VISUAL_REVIEW_PUBLIC_KEY",
    "EDGAR_VISUAL_REVIEW_PRIVATE_KEY",
    # `_int_setting` treats an empty value as unset and falls back to its code default.
    *LIMIT_SETTINGS,
    *NEW_API_SETTINGS,
}

# EDGAR_DEBUG has no code default: the code reads "" as off, and "0" is off as well.
DEFAULT_EQUIVALENTS = {"EDGAR_DEBUG": {"0"}}

INTERPOLATION = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)(:-|:\?)([^}]*)\}$")
ENV_READ_PATTERNS = (
    re.compile(r"""(?:getenv\(|environ\.get\(|environ\[|env\.get\()\s*["']([A-Z][A-Z0-9_]*)["']"""),
    re.compile(r"""^[A-Z][A-Z_]*_ENV\s*=\s*["']([A-Z][A-Z0-9_]*)["']""", re.MULTILINE),
    # Typed helpers in edgar_core.config (`_int_setting("NAME", ...)`), added with EDG-066.
    re.compile(r"""_(?:int|bool|str|float)_setting\(\s*["']([A-Z][A-Z0-9_]*)["']"""),
)


class _ComposeLoader(yaml.SafeLoader):
    """Loads Compose merge tags (`!reset`, `!override`) as their plain value."""


def _construct_tagged(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _construct_tagged)


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)  # noqa: S506


def _env(service: dict) -> dict[str, str]:
    """Return a service's environment as {NAME: raw value}, for list or mapping form."""
    env = service.get("environment") or {}
    if isinstance(env, list):
        out: dict[str, str] = {}
        for item in env:
            key, _, value = item.partition("=")
            out[key] = value
        return out
    return {key: "" if value is None else str(value) for key, value in env.items()}


def _services(compose: dict) -> dict[str, dict]:
    return compose["services"]


def _getenv_calls(path: Path) -> dict[str, str | None]:
    """Map each literal setting name that a module reads to its literal default, if any."""
    found: dict[str, str | None] = {}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in {"getenv", "get"} or not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", first.value):
            continue
        default = None
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            default = node.args[1].value if isinstance(node.args[1].value, str) else None
        found[first.value] = default
    return found


def _code_defaults() -> dict[str, str]:
    """Non-empty literal defaults. An empty-string default (`env.get(NAME, "")`) means unset here."""
    defaults: dict[str, str] = {}
    for path in (CONFIG_PY, MODEL_PROFILES_PY):
        for name, default in _getenv_calls(path).items():
            if default:
                defaults[name] = default
    return defaults


def _runtime_env_names() -> set[str]:
    names: set[str] = set()
    for rel in RUNTIME_DIRS:
        for path in (REPO / rel).rglob("*.py"):
            parts = path.relative_to(REPO).parts
            if "tests" in parts or "__pycache__" in parts:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in ENV_READ_PATTERNS:
                names.update(pattern.findall(text))
    return names


@pytest.fixture(scope="module")
def base() -> dict:
    return _load(BASE)


@pytest.fixture(scope="module")
def dev() -> dict:
    return _load(DEV)


@pytest.fixture(scope="module")
def hosting() -> dict:
    return _load(HOSTING)


def test_no_compose_file_uses_env_file() -> None:
    for path in (BASE, DEV, HOSTING):
        for name, service in _services(_load(path)).items():
            assert "env_file" not in service, f"{path.name}:{name} still uses env_file"


def test_each_service_receives_exactly_its_allow_list(base: dict) -> None:
    services = _services(base)
    for name, expected in EXPECTED_BASE.items():
        received = set(_env(services[name]))
        assert received == expected, name


def test_allow_lists_match_the_names_the_code_reads() -> None:
    read = _runtime_env_names()
    assigned = set().union(*EXPECTED_BASE.values())
    hosting_only = set().union(*HOSTING_EXTRA.values())
    unassigned = read - assigned - hosting_only
    stale = assigned - read
    assert not unassigned, f"runtime code reads names no service receives: {sorted(unassigned)}"
    assert not stale, f"allow-lists name settings the code never reads: {sorted(stale)}"


def test_settings_read_without_a_default_reach_every_consumer(base: dict) -> None:
    services = _services(base)
    no_default = {name for name, default in _getenv_calls(CONFIG_PY).items() if default is None}
    assert no_default == set(REQUIRED_WITHOUT_DEFAULT), (
        "a new setting without a default needs an explicit consumer list here"
    )
    for name, consumers in REQUIRED_WITHOUT_DEFAULT.items():
        for service in consumers:
            assert name in _env(services[service]), f"{service} misses required {name}"


def test_secrets_go_only_to_the_services_that_use_them(base: dict) -> None:
    services = _services(base)
    for name, allowed in SECRET_SCOPE.items():
        holders = {svc for svc, body in services.items() if name in _env(body)}
        assert holders == allowed, name


def test_secret_values_are_interpolated_never_literal(base: dict, dev: dict, hosting: dict) -> None:
    for compose in (base, dev, hosting):
        for name, service in _services(compose).items():
            for key, value in _env(service).items():
                if key in SECRET_SCOPE or key in HOSTING_ONLY_NAMES or key in HOSTING_REQUIRED:
                    assert value == "" or value.startswith("${"), f"{name}.{key} is a literal"
    for path in (BASE, DEV, HOSTING):
        assert not re.search(r"sk-[A-Za-z0-9_-]{8,}", path.read_text(encoding="utf-8")), path.name


def test_interpolated_defaults_match_the_code_defaults(base: dict) -> None:
    defaults = _code_defaults()
    for name, service in _services(base).items():
        for key, value in _env(service).items():
            match = INTERPOLATION.match(value)
            if not match or match.group(1) != key:
                continue
            _, operator, default = match.groups()
            if operator == ":?":
                pytest.fail(f"{name}.{key} requires a value in the default stack")
            if default == "":
                assert key in EMPTY_DEFAULT_OK, f"{name}.{key} defaults to empty"
                assert key not in defaults, f"{key} has a code default; empty would override it"
                continue
            if key in DEFAULT_EQUIVALENTS:
                assert default in DEFAULT_EQUIVALENTS[key], f"{name}.{key}"
            elif key in defaults:
                assert default == defaults[key], f"{name}.{key} default drifted from the code"


def test_default_stack_keeps_datastores_on_loopback_and_adds_no_networks(base: dict) -> None:
    services = _services(base)
    for name, service in services.items():
        assert "networks" not in service, name
        for port in service.get("ports") or []:
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} beyond loopback"
    for name in DATASTORE_SERVICES:
        assert services[name].get("ports"), f"{name} lost its loopback port in the default stack"


def test_hardening_applies_to_app_services_only(base: dict) -> None:
    services = _services(base)
    for name in NONROOT_APP_SERVICES:
        assert services[name]["cap_drop"] == ["ALL"], name
        assert services[name]["security_opt"] == ["no-new-privileges:true"], name
    for name in DATASTORE_SERVICES:
        assert "cap_drop" not in services[name], name
        assert "security_opt" not in services[name], name


def test_dockerfile_runs_every_app_stage_as_non_root() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    stages = {}
    current = None
    for line in text.splitlines():
        match = re.match(r"^FROM\s+\S+\s+AS\s+(\S+)", line)
        if match:
            current = match.group(1)
            stages[current] = ""
        elif current is not None:
            stages[current] += line + "\n"
    for name in NONROOT_APP_SERVICES:
        assert re.search(r"^USER edgar$", stages[name], re.MULTILINE), name


def test_dev_override_does_not_change_networks_ports_or_secrets(dev: dict) -> None:
    for name, service in _services(dev).items():
        assert not {"ports", "networks", "cap_drop", "security_opt"} & set(service), name
        assert not set(_env(service)) & (SECRET_SCOPE.keys() | HOSTING_ONLY_NAMES), name
    assert _services(dev)["api"]["environment"] == {"EDGAR_DEBUG": "1"}


def test_hosting_profile_removes_datastore_host_ports(hosting: dict) -> None:
    services = _services(hosting)
    for name in DATASTORE_SERVICES:
        assert services[name]["ports"] == [], name
    text = HOSTING.read_text(encoding="utf-8")
    assert text.count("ports: !reset []") == len(DATASTORE_SERVICES)


def test_hosting_profile_requires_every_credential_without_a_default() -> None:
    text = HOSTING.read_text(encoding="utf-8")
    for name in HOSTING_REQUIRED:
        assert f"${{{name}:-" not in text, f"{name} has a default in the hosting profile"
        assert f"${{{name}:?" in text, f"{name} is not required in the hosting profile"


def test_hosting_profile_adds_exactly_its_credentials_to_each_service(hosting: dict, base: dict) -> None:
    hosting_services = _services(hosting)
    base_services = _services(base)
    for name, extra in HOSTING_EXTRA.items():
        declared = set(_env(hosting_services[name]))
        assert declared == extra, name
        merged = {**_env(base_services[name]), **_env(hosting_services[name])}
        assert set(merged) == EXPECTED_BASE[name] | extra, name
        for key in extra:
            source = re.match(r"^\$\{([A-Z][A-Z0-9_]*):\?", merged[key])
            assert source and source.group(1) in HOSTING_REQUIRED, f"{name}.{key} is not required"


def test_hosting_profile_datastores_sit_on_an_internal_network(hosting: dict) -> None:
    networks = hosting["networks"]
    assert networks["datastore"]["internal"] is True
    assert "internal" not in networks["egress"]
    services = _services(hosting)
    for name in DATASTORE_SERVICES:
        assert services[name]["networks"] == ["datastore"], name
    for name in ("api", "ingestion", "rules_engine"):
        assert set(services[name]["networks"]) == {"egress", "datastore"}, name
    assert services["migrate"]["networks"] == ["datastore"]


def test_hosting_profile_keeps_api_loopback_and_redis_auth(base: dict, hosting: dict) -> None:
    base_api_ports = _services(base)["api"]["ports"]
    assert base_api_ports == ["127.0.0.1:8000:8000"]
    assert "ports" not in _services(hosting)["api"]
    command = _services(hosting)["redis"]["command"]
    assert command[:2] == ["redis-server", "--requirepass"]
    assert command[2] == "${REDIS_PASSWORD:?set REDIS_PASSWORD for the hosting profile}"


def test_import_time_defaults_cover_every_setting_the_config_reads() -> None:
    reads = _getenv_calls(CONFIG_PY)
    assert {"DB_HOST", "REDIS_PASSWORD", "QDRANT_HOST", "ENV"} <= set(reads)
    for name, default in reads.items():
        if default is None:
            assert name in REQUIRED_WITHOUT_DEFAULT, name


def test_hosting_only_names_are_absent_from_the_default_files() -> None:
    for path in (BASE, DEV):
        text = path.read_text(encoding="utf-8")
        for name in HOSTING_ONLY_NAMES:
            assert name not in text, f"{name} leaked into {path.name}"
