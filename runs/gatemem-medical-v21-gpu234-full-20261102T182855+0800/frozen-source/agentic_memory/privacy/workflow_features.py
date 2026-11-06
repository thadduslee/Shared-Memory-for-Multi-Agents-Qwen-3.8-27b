"""Shared semantic safeguards, independent of any scheduling/deadline policy.

V21 uses the V19 call envelope. These validators are shared with V20, but the
V20 deadline scheduler and timeout/defer behavior are deliberately not enabled.
"""
from .workflow_v20 import (
    semantic_context, subjects, whole_fact_allowed, setup, enqueue, pending,
    note_binding_failure, reviewable, validate_recovery_controls,
    finish_recovery, BINDING_INSTRUCTION,
)


def enabled(value):
    return str(getattr(value, 'version', value)).split('-', 1)[0] in {'20', '21'}


def review_limit(updater):
    if str(updater.version).split('-', 1)[0] == '21':
        return 24  # V19's batch bound; no wall-clock-dependent shrinking.
    from .workflow_v20 import review_limit as bounded
    return bounded(updater)
