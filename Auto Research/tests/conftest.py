import pytest

import config


@pytest.fixture(autouse=True)
def _seed_evaluation_is_opt_in(monkeypatch):
    """The scripted scenarios number their rounds from iteration 1.

    EVALUATE_SEED adds an iteration 0 in front of them, which shifts every
    expectation about lineage and numbering. It is on in production and tested
    on its own (test_graph_paths::test_the_seed_is_measured_as_iteration_0...);
    everywhere else it is off unless a test turns it on.
    """
    monkeypatch.setattr(config, "EVALUATE_SEED", False)
    # Same for the probe stage: the scenarios script their scores and shard
    # failures by (iteration, stage), and a probe round in front of every dev
    # round shifts what they expect. On in production; on here only by request.
    monkeypatch.setattr(config, "PROBE_CHECKPOINTS", 0)
