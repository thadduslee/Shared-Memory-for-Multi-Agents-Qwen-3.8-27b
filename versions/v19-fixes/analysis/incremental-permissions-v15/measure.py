"""Reproducible host/call-count measurements with scripted semantic decisions.

This does not measure real-model latency, semantic accuracy or benchmark utility.
Run from the repository root:
PYTHONPATH=. python analysis/incremental-permissions-v15/measure.py
"""
import json
from pathlib import Path
import statistics
import time

from security_tests.test_incremental_permissions import IncrementalTests


def main():
    case=IncrementalTests();case.setUp()
    try:
        case.setup_access();case.classify=lambda p,s:'instruction'
        measurements=[]
        for i in range(40):
            before=len(case.model.calls);started=time.perf_counter()
            case.turn(f'Pickup stop {i} is at the east entrance; bring permit {i}.')
            calls=case.model.calls[before:]
            measurements.append({'turn':i+1,'seconds':time.perf_counter()-started,
                'calls':len(calls),'input_chars':sum(len(json.dumps(p)) for p in calls),
                'repeated_historical_pairs':sum(len(p['required_pairs']) for p in calls)})
        assert all(r['calls']==1 and r['repeated_historical_pairs']==0 for r in measurements)
        assert len(case.selected())==40
        durations=sorted(r['seconds'] for r in measurements)
        report={'mode':'scripted-semantic-decisions','real_model_latency_measured':False,
            'real_model_semantic_accuracy_measured':False,
            'description':'Actual source persistence, canonical validation, two-store publication, indexing and authorized retrieval. Times include test-model schema validation.',
            'source_turns':40,'semantic_calls':sum(r['calls'] for r in measurements),
            'mean_host_and_test_seconds':statistics.mean(durations),
            'median_host_and_test_seconds':statistics.median(durations),
            'p90_host_and_test_seconds':durations[35],
            'max_host_and_test_seconds':max(durations),
            'authorized_facts_retrieved':len(case.selected()),'turns':measurements}
        path=Path(__file__).with_name('measurements.json')
        path.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({k:v for k,v in report.items() if k!='turns'},indent=2))
    finally:case.tearDown()


if __name__=='__main__':main()
