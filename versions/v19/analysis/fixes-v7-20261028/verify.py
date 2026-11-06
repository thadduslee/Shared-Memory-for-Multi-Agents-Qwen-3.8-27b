"""Repeat local checks and combine the retained real-model diagnostic results."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent


def main():
    checks={}
    for suite,name in [('security_tests','security'),('evaluation/gatemem_medical/tests','evaluation')]:
        result=subprocess.run(['python3','-m','unittest','discover','-s',suite,'-q'],
            cwd=ROOT,capture_output=True,text=True)
        log=result.stdout+result.stderr
        (OUT/(name+'-tests.log')).write_text(log)
        match=re.search(r'Ran (\d+) tests',log)
        checks[name]={'passed':result.returncode==0,'tests':int(match.group(1)) if match else None,'output':log.strip()}
        print(name,log.strip(),flush=True)
        if result.returncode:raise SystemExit(result.returncode)
    result=subprocess.run(['python3',str(OUT/'validate_snapshot.py')],cwd=ROOT,capture_output=True,text=True)
    if result.returncode:print(result.stdout+result.stderr);raise SystemExit(result.returncode)
    checks['performance']=json.loads((OUT/'performance.json').read_text())
    checks['logistics_real_model']=json.loads((OUT/'real-logistics-2/report.json').read_text())
    checks['response_plan_real_model']=json.loads((OUT/'response-plan.json').read_text())
    assert checks['response_plan_real_model']['passed']
    checks['development_real_model']=json.loads((OUT/'real-smoke-1/report.json').read_text())
    # Preserve the initially failed logistics case. Its corrected regression is
    # a separate fresh source stream, not a rewritten development prediction.
    assert checks['logistics_real_model']['passed']
    development={r['check']:r['passed'] for r in checks['development_real_model']['checks']}
    assert all(development[name] for name in ['accepted-reviewer-task','operational-contact-binding','erasure-owner','erasure-coordinator'])
    reviews=[json.loads(line) for line in (OUT/'judge-review.jsonl').read_text().splitlines()]
    checks['judge_review']={'incomplete_answers_rejected':all(r['utility_correct'] is False for r in reviews[:3]),
                           'nondisclosure_rule_not_context_leak':reviews[3]['privacy_context_leak'] is False}
    assert all(checks['judge_review'].values())
    checks['original_v6_unchanged']={}
    archived=json.loads((ROOT/'analysis/benchmark-diagnosis-v6-20261028/summary.json').read_text())['source_files']
    for name,expected in archived.items():
        path=Path(name)
        checks['original_v6_unchanged'][path.name]=hashlib.sha256(path.read_bytes()).hexdigest()==expected
    assert all(checks['original_v6_unchanged'].values())
    result=subprocess.run(['git','diff','--check'],cwd=ROOT,capture_output=True,text=True)
    checks['diff_check_passed']=result.returncode==0
    assert result.returncode==0,result.stdout+result.stderr
    checks['runtime_code_hashes']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for directory in [ROOT/'agentic_memory/privacy',ROOT/'evaluation/gatemem_medical'] for p in directory.glob('*.py')}
    (OUT/'validation.json').write_text(json.dumps(checks,indent=2)+'\n')
    OUT.chmod(0o700)
    for path in OUT.iterdir():
        if path.is_file():path.chmod(0o600)
    print(json.dumps({'tests':checks['security']['tests']+checks['evaluation']['tests'],
                     'performance':checks['performance'],'judge_review':checks['judge_review']},indent=2))


if __name__=='__main__':main()
