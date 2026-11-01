"""Rebuild the release evidence manifest; no model-quality claims from doubles."""
import hashlib
import json
from pathlib import Path
import statistics
import xml.etree.ElementTree as ET

from agentic_memory.privacy.system import digest

ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent


def read(name):return json.loads((OUT/name).read_text())


def main():
    tests=ET.parse(OUT/'regression-tests.xml').getroot()
    suites=list(tests) if tests.tag=='testsuites' else [tests]
    counts={k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
    assert counts['failures']==counts['errors']==0,counts
    offline=read('offline-final-verified/summary.json')
    coverage=offline['coverage']
    assert all(coverage[k] for k in ('exact_checkpoint_coverage','exact_source_coverage','all_boundaries_complete'))
    assert not offline['degraded']
    files={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in (ROOT/'agentic_memory/privacy',ROOT/'evaluation/gatemem_medical')
        for p in sorted(folder.iterdir()) if p.is_file() and (p.suffix=='.py' or p.name=='fixture_policy.json')}
    code_digest=digest(files)
    manifest=read('offline-final-verified/manifest.json')
    assert manifest['code_digest']==code_digest,'Offline evidence belongs to an earlier implementation'
    project=read('live-project-fp8-release/report.json')
    assert project['all_checks_passed'],project['checks']
    measurements=read('measurements.json')
    assert measurements['semantic_calls']==measurements['source_turns']==40
    assert measurements['authorized_facts_retrieved']==40
    report={'implementation_code_digest':code_digest,'implementation_files':files,
        'regression_junit':counts,'offline_full_schedule':coverage,
        'offline_processor':'explicit no-op test double; utility is not measured',
        'scripted_measurement':{k:v for k,v in measurements.items() if k!='turns'},
        'real_project':{k:project[k] for k in ('model','precision','initial_access_empty','checks','all_checks_passed','turns','latency_seconds')},
        'full_real_model_benchmark_completed':False,'full_utility_improvement_established':False,
        'same_hardware_latency_target_established':False,
        'medical_diagnostics':[]}
    for name in ('live-medical-fp8-final','live-medical-fp8-v2-independent-relations','live-medical-fp8-v3-bounded','live-medical-fp8-release'):
        if not (OUT/name/'report.json').exists():continue
        r=read(name+'/report.json');t=[row['seconds'] for row in r['turns']]
        report['medical_diagnostics'].append({'artifact':name,'completed_turns':len(t),
            'mean_seconds':statistics.mean(t) if t else None,'median_seconds':statistics.median(t) if t else None,
            'max_seconds':max(t) if t else None,'scored_queries':len(r.get('predictions',[])),
            'completed':r.get('completed',bool(r.get('permission_jobs'))),'error':r.get('error',r.get('reason')),
            'official_benchmark':False})
    (OUT/'validation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k not in ('implementation_files','real_project')},indent=2))


if __name__=='__main__':main()
