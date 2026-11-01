"""Read-only authorization microprofile; no model/embedding calls or store writes.

The cached comparison only changes an in-process function during a fixed read
snapshot. It is a diagnostic experiment, not a deployed cache or full search test.
"""
import cProfile
import io
import json
import pstats
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
RUN = ROOT / 'runs/gatemem-medical-raw-stream-v6'
sys.path.insert(0, str(ROOT))
from agentic_memory.privacy.system import PrivacyMemorySystem
from agentic_memory.privacy import permissions


def main():
    # Bypass the normal constructor because it initializes writable stores.
    core = PrivacyMemorySystem.__new__(PrivacyMemorySystem)
    core.db = sqlite3.connect((RUN / 'memory.sqlite').as_uri() + '?mode=ro', uri=True)
    core.db.row_factory = sqlite3.Row
    core.db.execute('ATTACH DATABASE ? AS privacy', ((RUN / 'memory.privacy.sqlite').as_uri() + '?mode=ro',))
    core.db.execute('PRAGMA query_only=ON')
    core.db.execute('BEGIN')
    bootstrap = core.db.execute('SELECT compartment,issuer FROM permission_bootstrap').fetchone()
    fixture_time = core.db.execute("SELECT value FROM metadata WHERE key LIKE 'fixture_time:%' LIMIT 1").fetchone()[0]
    core.clock = lambda: fixture_time
    core._lock = threading.RLock()
    core._sessions = {}; core._processors = {}; core._tickets = {}
    ctx = core.issue_session(bootstrap['issuer'], 'readonly-authorization-profile',
                             actions=['search', 'read', 'derive', 'manage_policy'],
                             compartments=[bootstrap['compartment']],
                             processing_routes=['local:minilm'])
    selected = [core.db.execute("SELECT id FROM resources WHERE kind=? ORDER BY created " + order + " LIMIT 1", (kind,)).fetchone()[0]
                for kind, order in [('memory', 'ASC'), ('memory', 'DESC'), ('enrichment', 'DESC')]]
    profiler = cProfile.Profile()
    profiler.enable()
    first_decision = core.allowed(ctx, selected[-1], 'search')
    profiler.disable()
    output = io.StringIO()
    pstats.Stats(profiler, stream=output).sort_stats('cumulative').print_stats(22)
    (OUT / 'authorization_profile.txt').write_text(output.getvalue())

    def sample():
        start = time.monotonic()
        decisions = [{'id': rid, 'action': action,
                      'allowed': core.allowed(ctx, rid, action, processor='local:minilm' if action == 'derive' else None)}
                     for rid in selected for action in ['search', 'read', 'derive']]
        return {'seconds': time.monotonic() - start, 'decisions': decisions}

    baseline = sample()
    original_records = permissions._records
    started = time.monotonic()
    pinned_state = original_records(core, bootstrap['compartment'])
    cache_build_s = time.monotonic() - started

    def fixed_snapshot_records(c, compartment):
        assert c is core and compartment == bootstrap['compartment']
        return pinned_state

    try:
        permissions._records = fixed_snapshot_records
        cached = sample()
    finally:
        permissions._records = original_records
        core.db.rollback()
        core.db.close()
    assert baseline['decisions'] == cached['decisions']
    report = {'semantics': 'Nine host authorization checks on three saved resources, in a pinned read-only snapshot. No real embedding, inference, or full neighbor search. Diagnostic in-process reuse of an identical permission ledger; production invalidation is unimplemented.',
              'fixture_time': fixture_time, 'profiled_first_decision': first_decision,
              'baseline': baseline, 'fixed_snapshot_ledger_reuse': cached,
              'ledger_load_seconds': cache_build_s,
              'same_decisions': True}
    (OUT / 'authorization_profile.json').write_text(json.dumps(report, indent=2) + '\n')
    for name in ['authorization_profile.txt', 'authorization_profile.json']:
        (OUT / name).chmod(0o600)
    print(output.getvalue())
    print(json.dumps({'baseline_seconds': baseline['seconds'], 'ledger_reuse_seconds': cached['seconds'],
                      'ledger_load_seconds': cache_build_s, 'same_nine_decisions': True}, indent=2))


if __name__ == '__main__':
    main()
