"""Validated, non-secret model settings for Edgar runtime stages.

The checked-in TOML file is intentionally optional at deployment: when it is absent,
legacy ``LLM_MODEL`` and ``LLM_REASONING_EFFORT`` settings remain the play defaults.
Preparation roles are policy-bound and cannot be changed by configuration.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

from edgar_core.artifacts import text_digest
from edgar_core.config import EDGAR_ROOT

SCHEMA_VERSION = 1
LOCKED_MODEL = "gpt-6.1-sol"
LOCKED_EFFORT = "high"
LOCKED_API: Literal["responses"] = "responses"
DEFAULT_PROFILE_PATH = EDGAR_ROOT / "config" / "model-profiles.toml"
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ALLOWED_EFFORTS = {"none", "low", "medium", "high", "xhigh"}
_PROFILE_KEYS = {
    "model",
    "reasoning_effort",
    "temperature",
    "max_input_tokens",
    "max_output_tokens",
    "timeout_seconds",
    "retries",
    "api",
}
_IMAGE_KEYS = {
    "model",
    "size",
    "quality",
    "output_format",
    "timeout_seconds",
    "retries",
}

PLAY_STAGES = (
    "play.default",
    "play.parser",
    "play.consequence_planner",
    "play.narrator",
    "play.combat_builder",
    "play.combat_narrator",
    "play.memory_summarizer",
    "play.session_intro",
)
AUTHORING_STAGES = (
    "authoring.outline",
    "authoring.chapter",
    "authoring.coherence",
    "authoring.manifest",
)
PREPARATION_STAGES = tuple(f"preparation.p{i:02d}" for i in range(1, 13))
TEXT_STAGES = PLAY_STAGES + AUTHORING_STAGES + PREPARATION_STAGES
IMAGE_STAGES = ("image.adventure_map", "image.illustration", "image.creature_art")


class ModelProfileError(ValueError):
    """Safe local configuration error; messages never include setting values or secrets."""


@dataclass(frozen=True, slots=True)
class ModelProfile:
    name: str
    model: str
    reasoning_effort: str | None = None
    temperature: float | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    timeout_seconds: float = 60.0
    retries: int = 3
    api: Literal["auto", "responses"] = "auto"

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "temperature": self.temperature,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "timeout_seconds": self.timeout_seconds,
            "retries": self.retries,
            "api": self.api,
        }


@dataclass(frozen=True, slots=True)
class ImageProfile:
    name: str
    model: str
    size: str = "1024x1024"
    quality: str = "medium"
    output_format: str = "png"
    timeout_seconds: float = 120.0
    retries: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "size": self.size,
            "quality": self.quality,
            "output_format": self.output_format,
            "timeout_seconds": self.timeout_seconds,
            "retries": self.retries,
        }


@dataclass(frozen=True, slots=True)
class ModelProfileSnapshot:
    """Immutable effective settings captured once for a request or durable authoring run."""

    profile_version: str
    profiles: Mapping[str, ModelProfile]
    image_profiles: Mapping[str, ImageProfile]
    source: Literal["toml", "environment"] = "environment"
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "profiles", MappingProxyType(dict(self.profiles)))
        object.__setattr__(self, "image_profiles", MappingProxyType(dict(self.image_profiles)))
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != SCHEMA_VERSION
        ):
            raise ModelProfileError("Unsupported model profile snapshot schema version.")
        _validate_version(self.profile_version)
        if self.source not in {"toml", "environment"}:
            raise ModelProfileError("Model profile snapshot source is invalid.")
        _validate_snapshot_profiles(self.profiles, self.image_profiles)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe, secret-free record suitable for durable run storage."""
        return {
            "schema_version": self.schema_version,
            "profile_version": self.profile_version,
            "source": self.source,
            "profiles": {name: profile.to_dict() for name, profile in sorted(self.profiles.items())},
            "image_profiles": {
                name: profile.to_dict() for name, profile in sorted(self.image_profiles.items())
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ModelProfileSnapshot:
        """Restore a frozen snapshot without reading config files or the environment."""
        if not isinstance(value, Mapping) or set(value) != {
            "schema_version",
            "profile_version",
            "source",
            "profiles",
            "image_profiles",
        }:
            raise ModelProfileError("Model profile snapshot has an invalid shape.")
        source = value["source"]
        if not isinstance(source, str) or source not in {"toml", "environment"}:
            raise ModelProfileError("Model profile snapshot source is invalid.")
        profiles_raw = value["profiles"]
        images_raw = value["image_profiles"]
        if not isinstance(profiles_raw, Mapping) or not isinstance(images_raw, Mapping):
            raise ModelProfileError("Model profile snapshot profiles must be objects.")
        profiles = {
            name: _profile_from_mapping(name, settings, locked=name in AUTHORING_STAGES + PREPARATION_STAGES)
            for name, settings in profiles_raw.items()
        }
        image_profiles = {
            name: _image_profile_from_mapping(name, settings)
            for name, settings in images_raw.items()
        }
        return cls(
            profile_version=value["profile_version"],
            profiles=profiles,
            image_profiles=image_profiles,
            source=cast(Literal["toml", "environment"], source),
            schema_version=value["schema_version"],
        )

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            self.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        return cast(str, text_digest(canonical))


def _validate_version(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise ModelProfileError("profile_version must be a non-empty string of at most 80 characters.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", value):
        raise ModelProfileError("profile_version contains unsupported characters.")
    return value


def _flatten_profiles(value: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, child in value.items():
        if not isinstance(key, str) or not key:
            raise ModelProfileError("Profile names must be non-empty strings.")
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(child, Mapping):
            if not child or all(not isinstance(item, Mapping) for item in child.values()):
                flattened[name] = child
            else:
                flattened.update(_flatten_profiles(child, name))
        else:
            flattened[name] = child
    return flattened


def _validated_name(name: Any, allowed: tuple[str, ...], kind: str) -> str:
    if not isinstance(name, str) or name not in allowed:
        raise ModelProfileError(f"Unknown {kind} stage. Check the documented profile names.")
    return name


def _profile_from_mapping(name: Any, settings: Any, *, locked: bool = False) -> ModelProfile:
    known_name = _validated_name(name, TEXT_STAGES, "text")
    if not isinstance(settings, Mapping):
        raise ModelProfileError(f"Profile '{known_name}' must be a table.")
    unknown = set(settings) - _PROFILE_KEYS
    if unknown:
        raise ModelProfileError(f"Profile '{known_name}' has unsupported option keys.")
    model = settings.get("model", LOCKED_MODEL if locked else "gpt-6-luna")
    if not isinstance(model, str) or not _MODEL_RE.fullmatch(model):
        raise ModelProfileError(f"Profile '{known_name}' has an invalid model identifier.")
    effort = settings.get("reasoning_effort", LOCKED_EFFORT if locked else None)
    if effort is not None and (
        not isinstance(effort, str) or effort not in _ALLOWED_EFFORTS
    ):
        raise ModelProfileError(f"Profile '{known_name}' has an unsupported reasoning effort.")
    temperature = settings.get("temperature")
    if temperature is not None and (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not 0 <= temperature <= 2
    ):
        raise ModelProfileError(f"Profile '{known_name}' temperature must be between 0 and 2.")
    input_tokens = settings.get("max_input_tokens")
    if input_tokens is not None and (
        isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 1
    ):
        raise ModelProfileError(f"Profile '{known_name}' max_input_tokens must be a positive integer.")
    output_tokens = settings.get("max_output_tokens")
    if output_tokens is not None and (
        isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or output_tokens < 1
    ):
        raise ModelProfileError(f"Profile '{known_name}' max_output_tokens must be a positive integer.")
    timeout = settings.get("timeout_seconds", 60.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 3600:
        raise ModelProfileError(f"Profile '{known_name}' timeout_seconds must be between 1 and 3600.")
    retries = settings.get("retries", 3)
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 10:
        raise ModelProfileError(f"Profile '{known_name}' retries must be an integer from 0 to 10.")
    api = settings.get("api", "responses" if locked else "auto")
    if not isinstance(api, str) or api not in {"auto", "responses"}:
        raise ModelProfileError(f"Profile '{known_name}' has an unsupported provider API.")
    if locked and (
        model != LOCKED_MODEL
        or effort != LOCKED_EFFORT
        or api != LOCKED_API
        or temperature is not None
    ):
        raise ModelProfileError(
            f"Profile '{known_name}' is policy-bound to gpt-6.1-sol/high on the Responses API."
        )
    if effort is not None and not _is_reasoning_model(model):
        raise ModelProfileError(
            f"Profile '{known_name}' sets reasoning_effort for a model without reasoning support."
        )
    if temperature is not None and _is_reasoning_model(model):
        raise ModelProfileError(
            f"Profile '{known_name}' sets temperature for a reasoning model; omit it."
        )
    if _is_reasoning_model(model) and api != "responses":
        raise ModelProfileError(
            f"Profile '{known_name}' uses a reasoning model and must select the Responses API."
        )
    return ModelProfile(
        name=known_name,
        model=model,
        reasoning_effort=effort,
        temperature=float(temperature) if temperature is not None else None,
        max_input_tokens=input_tokens,
        max_output_tokens=output_tokens,
        timeout_seconds=float(timeout),
        retries=retries,
        api=cast(Literal["auto", "responses"], api),
    )


def _image_profile_from_mapping(name: Any, settings: Any) -> ImageProfile:
    known_name = _validated_name(name, IMAGE_STAGES, "image")
    if not isinstance(settings, Mapping):
        raise ModelProfileError(f"Image profile '{known_name}' must be a table.")
    unknown = set(settings) - _IMAGE_KEYS
    if unknown:
        raise ModelProfileError(f"Image profile '{known_name}' has unsupported option keys.")
    model = settings.get("model", "gpt-image-1")
    if not isinstance(model, str) or not _MODEL_RE.fullmatch(model):
        raise ModelProfileError(f"Image profile '{known_name}' has an invalid model identifier.")
    size = settings.get("size", "1024x1024")
    if not isinstance(size, str) or size not in {
        "1024x1024",
        "1536x1024",
        "1024x1536",
        "auto",
    }:
        raise ModelProfileError(f"Image profile '{known_name}' has an unsupported size.")
    quality = settings.get("quality", "medium")
    if not isinstance(quality, str) or quality not in {"low", "medium", "high", "auto"}:
        raise ModelProfileError(f"Image profile '{known_name}' has an unsupported quality.")
    output_format = settings.get("output_format", "png")
    if not isinstance(output_format, str) or output_format not in {"png", "jpeg", "webp"}:
        raise ModelProfileError(f"Image profile '{known_name}' has an unsupported output format.")
    timeout = settings.get("timeout_seconds", 120.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 3600:
        raise ModelProfileError(
            f"Image profile '{known_name}' timeout_seconds must be between 1 and 3600."
        )
    retries = settings.get("retries", 2)
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 10:
        raise ModelProfileError(f"Image profile '{known_name}' retries must be an integer from 0 to 10.")
    return ImageProfile(
        name=known_name,
        model=model,
        size=size,
        quality=quality,
        output_format=output_format,
        timeout_seconds=float(timeout),
        retries=retries,
    )


def _validate_snapshot_profiles(
    profiles: Mapping[str, ModelProfile], image_profiles: Mapping[str, ImageProfile]
) -> None:
    if set(profiles) != set(TEXT_STAGES):
        raise ModelProfileError("Model profile snapshot is missing or has unknown text stages.")
    if set(image_profiles) != set(IMAGE_STAGES):
        raise ModelProfileError("Model profile snapshot is missing or has unknown image stages.")
    for name, text_profile in profiles.items():
        if not isinstance(text_profile, ModelProfile):
            raise ModelProfileError("Model profile snapshot contains an invalid text profile.")
        if text_profile.name != name:
            raise ModelProfileError("Model profile snapshot stage names do not match their records.")
        validated = _profile_from_mapping(
            name, text_profile.to_dict(), locked=name in AUTHORING_STAGES + PREPARATION_STAGES
        )
        if validated != text_profile:
            raise ModelProfileError("Model profile snapshot contains invalid effective settings.")
    for name, image_profile in image_profiles.items():
        if not isinstance(image_profile, ImageProfile):
            raise ModelProfileError("Model profile snapshot contains an invalid image profile.")
        if image_profile.name != name:
            raise ModelProfileError("Image profile snapshot stage names do not match their records.")
        validated_image = _image_profile_from_mapping(name, image_profile.to_dict())
        if validated_image != image_profile:
            raise ModelProfileError("Image profile snapshot contains invalid effective settings.")


def _is_reasoning_model(model: str) -> bool:
    return model.startswith(("o1", "o3", "o4", "gpt-5", "gpt-6")) and "chat" not in model


def _env_mapping() -> Mapping[str, str]:
    return os.environ


def load_model_profiles(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ModelProfileSnapshot:
    """Load and validate a complete snapshot; missing files preserve legacy env behavior."""
    env = environ if environ is not None else _env_mapping()
    configured_path = path if path is not None else env.get("MODEL_PROFILES_PATH")
    profile_path = Path(configured_path).expanduser() if configured_path else DEFAULT_PROFILE_PATH
    if configured_path and not profile_path.is_file():
        raise ModelProfileError("Configured model profile file is unavailable. Check MODEL_PROFILES_PATH.")
    raw: Mapping[str, Any] = {}
    source: Literal["toml", "environment"] = "environment"
    if profile_path.exists():
        try:
            with profile_path.open("rb") as stream:
                raw_loaded = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ModelProfileError(
                f"Could not read model profile TOML: {type(exc).__name__}."
            ) from None
        if set(raw_loaded) - {"schema_version", "profile_version", "profiles", "images"}:
            raise ModelProfileError("Model profile file contains unsupported top-level keys.")
        schema_version = raw_loaded.get("schema_version", SCHEMA_VERSION)
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != SCHEMA_VERSION
        ):
            raise ModelProfileError("Unsupported model profile file schema_version.")
        raw = raw_loaded
        source = "toml"
    profile_version = _validate_version(raw.get("profile_version", "1"))
    raw_profiles = raw.get("profiles", {})
    raw_images = raw.get("images", {})
    if not isinstance(raw_profiles, Mapping) or not isinstance(raw_images, Mapping):
        raise ModelProfileError("profiles and images must be TOML tables.")
    flattened_profiles = _flatten_profiles(raw_profiles)
    flattened_images = _flatten_profiles(raw_images)
    unknown_profiles = set(flattened_profiles) - set(TEXT_STAGES)
    unknown_images = set(flattened_images) - set(IMAGE_STAGES)
    if unknown_profiles:
        raise ModelProfileError("Model profile file contains an unknown text stage.")
    if unknown_images:
        raise ModelProfileError("Model profile file contains an unknown image stage.")

    env_model = env.get("LLM_MODEL", "gpt-6-luna")
    env_effort = env.get("LLM_REASONING_EFFORT", "low")
    if not isinstance(env_model, str) or not _MODEL_RE.fullmatch(env_model):
        raise ModelProfileError("LLM_MODEL must be a valid model identifier.")
    if env_effort not in _ALLOWED_EFFORTS:
        raise ModelProfileError("LLM_REASONING_EFFORT has an unsupported value.")
    defaults: dict[str, dict[str, Any]] = {}
    for name in PLAY_STAGES:
        configured = flattened_profiles.get(name, {})
        selected_model = configured.get("model", env_model)
        base: dict[str, Any] = {"model": selected_model}
        if _is_reasoning_model(selected_model):
            base["reasoning_effort"] = env_effort
            base["api"] = "responses"
        defaults[name] = base
    for name in AUTHORING_STAGES + PREPARATION_STAGES:
        defaults[name] = {
            "model": LOCKED_MODEL,
            "reasoning_effort": LOCKED_EFFORT,
            "api": LOCKED_API,
        }
    profiles = {
        name: _profile_from_mapping(
            name, {**defaults[name], **flattened_profiles.get(name, {})},
            locked=name in AUTHORING_STAGES + PREPARATION_STAGES,
        )
        for name in TEXT_STAGES
    }
    image_defaults = {
        name: {"model": "gpt-image-1"} for name in IMAGE_STAGES
    }
    image_profiles = {
        name: _image_profile_from_mapping(
            name, {**image_defaults[name], **flattened_images.get(name, {})}
        )
        for name in IMAGE_STAGES
    }
    return ModelProfileSnapshot(
        profile_version=profile_version,
        profiles=profiles,
        image_profiles=image_profiles,
        source=source,
    )


_CURRENT_SNAPSHOT: ContextVar[ModelProfileSnapshot | None] = ContextVar(
    "edgar_model_profile_snapshot", default=None
)


@contextmanager
def model_profile_snapshot(
    profiles: ModelProfileSnapshot | None = None,
) -> Iterator[ModelProfileSnapshot]:
    """Freeze profile settings in this sync/async context, resetting safely on errors/cancel."""
    snapshot = profiles or load_model_profiles()
    token = _CURRENT_SNAPSHOT.set(snapshot)
    try:
        yield snapshot
    finally:
        _CURRENT_SNAPSHOT.reset(token)


def current_model_profiles() -> ModelProfileSnapshot:
    """Return the current frozen snapshot, or load a fresh snapshot outside a scoped request."""
    current = _CURRENT_SNAPSHOT.get()
    return current if current is not None else load_model_profiles()


def effective_profile(
    stage: str, profiles: ModelProfileSnapshot | None = None
) -> ModelProfile:
    """Resolve one validated stage from a frozen snapshot."""
    name = _validated_name(stage, TEXT_STAGES, "text")
    snapshot = profiles or current_model_profiles()
    profile = snapshot.profiles[name]
    if name in AUTHORING_STAGES + PREPARATION_STAGES and (
        profile.model != LOCKED_MODEL
        or profile.reasoning_effort != LOCKED_EFFORT
        or profile.api != LOCKED_API
    ):
        raise ModelProfileError(f"Profile '{name}' violates the campaign preparation model policy.")
    return profile


def effective_image_profile(
    stage: str, profiles: ModelProfileSnapshot | None = None
) -> ImageProfile:
    """Resolve an image-only profile without claiming the account can access its model."""
    name = _validated_name(stage, IMAGE_STAGES, "image")
    return (profiles or current_model_profiles()).image_profiles[name]


def profile_diagnostics(snapshot: ModelProfileSnapshot | None = None) -> dict[str, Any]:
    """Safe non-secret effective settings for developer diagnostics and run receipts."""
    frozen = snapshot or current_model_profiles()
    return {
        "profile_version": frozen.profile_version,
        "profile_digest": frozen.digest,
        "source": frozen.source,
        "availability_checked": False,
        "profiles": {name: item.to_dict() for name, item in sorted(frozen.profiles.items())},
        "image_profiles": {
            name: item.to_dict() for name, item in sorted(frozen.image_profiles.items())
        },
    }


def validate_provider_configuration(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Check local provider settings at an API or CLI boundary without calling a provider.

    Raises ``ModelProfileError`` naming the faulty setting, never its value.
    """
    env = environ if environ is not None else _env_mapping()
    api_key = env.get("OPENAI_API_KEY", "").strip()
    if not api_key or api_key.startswith("sk-your"):  # .env.example placeholder
        raise ModelProfileError(
            "OPENAI_API_KEY is missing or still the .env.example placeholder. "
            "Set it in .env or the process environment."
        )
    embedding_model = env.get("EMBEDDING_MODEL", "text-embedding-3-small")
    if not _MODEL_RE.fullmatch(embedding_model):
        raise ModelProfileError("EMBEDDING_MODEL must be a valid model identifier.")
    return {
        **profile_diagnostics(load_model_profiles(environ=env)),
        "embedding_model": embedding_model,
        "required_credentials": ["OPENAI_API_KEY"],
    }
