from __future__ import annotations

from gsm.domain.reasons import Reason


class OpRejected(Exception):
    """An operation failed validation or authorization; the batch must roll back."""

    def __init__(self, *reasons: Reason) -> None:
        self.reasons = reasons
        super().__init__(",".join(r.value for r in reasons))
