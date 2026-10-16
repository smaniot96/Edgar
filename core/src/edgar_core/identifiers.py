"""Canonical identifiers shared by source manifests and prepared campaign records."""

from typing import Annotated

from pydantic import Field

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
