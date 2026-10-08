"""Shared pydantic helpers for PATCH bodies."""

from typing import Any

from pydantic import ValidationInfo, field_validator


def forbid_null(*fields: str) -> Any:
    """Field validator: the listed PATCH fields may be omitted but not sent as `null`.

    PATCH handlers apply `model_dump(exclude_unset=True)`, so an explicit `null` would be written
    to a NOT NULL column and surface as a 500. Validators do not run for omitted fields (defaults
    are not validated), so only an explicit `null` is rejected, with a 422.
    """

    def _check(cls: type, value: Any, info: ValidationInfo) -> Any:
        if value is None:
            raise ValueError(f"{info.field_name} cannot be null; omit it to leave it unchanged")
        return value

    return field_validator(*fields, mode="after")(classmethod(_check))
