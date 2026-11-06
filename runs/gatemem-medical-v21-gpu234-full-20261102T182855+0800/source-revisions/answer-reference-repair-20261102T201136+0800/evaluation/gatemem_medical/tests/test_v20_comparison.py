"""Quality gates require matched cases, context checks, and full coverage."""
import importlib.util
import json
from pathlib import Path


spec=importlib.util.spec_from_file_location('v20_compare',Path(__file__).resolve().parents[3]/'analysis/workflow-v20/compare_runs.py')
comparison=importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


def write_run(path,rows):
    path.mkdir()
    (path/'scores.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    (path/'manifest.json').write_text(json.dumps({'dataset_digest':'same','models':{'memory':'same','judge':'same'}}))
    (path/'summary.json').write_text(json.dumps({'complete':True,'coverage':{'scored_checkpoints':len(rows)}}))


def rows():
    return [
        {'checkpoint_id':'u','query_type':'utility','utility_correct':True,'over_refusal':False},
        {'checkpoint_id':'p','query_type':'privacy','privacy_leak':False,'privacy_context_leak':False,'privacy_e2e_leak':False},
        {'checkpoint_id':'d','query_type':'safety','deletion_leak':False,'deletion_context_leak':False,'deletion_e2e_leak':False},
    ]


def test_matched_sample_cannot_be_reported_as_complete_full_benchmark(tmp_path):
    a,b=tmp_path/'a',tmp_path/'b'
    write_run(a,rows());write_run(b,rows())
    result=comparison.compare(a,b,count=3)
    assert result['matched_gate']['passed']
    assert not result['full_coverage_complete']
    assert not result['full_gate']['passed']


def test_context_only_leak_fails_even_with_safe_final_answer(tmp_path):
    a,b=tmp_path/'a',tmp_path/'b'
    candidate=rows();candidate[1].update(privacy_context_leak=True,privacy_e2e_leak=True)
    write_run(a,rows());write_run(b,candidate)
    result=comparison.compare(a,b,count=3)
    assert not result['matched_gate']['privacy_no_worse']


def test_different_checkpoint_order_is_not_a_matched_comparison(tmp_path):
    a,b=tmp_path/'a',tmp_path/'b'
    write_run(a,rows());write_run(b,list(reversed(rows())))
    assert not comparison.compare(a,b,count=3)['matched_gate']['passed']
