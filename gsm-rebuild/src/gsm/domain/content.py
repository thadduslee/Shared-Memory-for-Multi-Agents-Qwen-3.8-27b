"""Memory content payloads."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

FieldName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class Content(BaseModel):
    """A structured payload. ``text`` is free text; ``fields`` are schema-typed values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str | None = Field(default=None, max_length=8000)
    fields: dict[FieldName, Annotated[str, StringConstraints(max_length=2000)]] = Field(default_factory=dict)
