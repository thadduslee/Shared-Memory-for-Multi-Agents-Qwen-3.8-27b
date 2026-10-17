"""Evidence bundles: permitted projections plus non-sensitive provenance metadata."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from gsm.domain.types import View
from gsm.providers.base import GeneratorEvidence


class EvidenceItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_id: str
    version_id: str
    view: View
    semantic_type: str
    content_schema: str
    topic: str
    text: str
    fields: dict[str, str]
    observed_at: datetime
    valid_from: datetime
    valid_until: datetime | None
    version_state: str
    truth_state: str
    is_derived: bool
    score: float
    lexical_hit: bool
    vector_similarity: float | None
    policy_version: str

    def for_generator(self) -> GeneratorEvidence:
        return GeneratorEvidence(
            memory_id=self.memory_id,
            view=self.view,
            semantic_type=self.semantic_type,
            text=self.text,
            fields=dict(self.fields),
            observed_at=self.observed_at,
            content_schema=self.content_schema,
        )


class EvidenceBundle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[EvidenceItem, ...]
    as_of: datetime | None
    authorized_candidates: int
