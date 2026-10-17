"""Normalized policy decisions."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from gsm.domain.reasons import Reason
from gsm.domain.types import Decision, View


class PolicyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: Decision
    view: View | None
    reasons: tuple[Reason, ...]
    policy_version: str
    memory_id: str | None
    principal_id: str
    decided_at: datetime
    # For DENY only: may the requester be told the record exists (REFUSE)?
    existence_ack: bool = False

    @property
    def allowed(self) -> bool:
        return self.decision in (Decision.ALLOW, Decision.ALLOW_REDACTED)
