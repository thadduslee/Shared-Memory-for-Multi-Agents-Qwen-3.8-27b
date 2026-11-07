"""Summarize the V19 privacy-fallback validation reports and runtime hashes.

python analysis/v19-privacy-fallback/validation_summary.py
"""
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
REPORTS = {
    'regression-tests.xml': 'security_tests + evaluation/gatemem_medical/tests (V20 modules and the episode module excluded)',
    'episode-replay-tests.xml': 'security_tests/test_privacy_fallback_episodes_v19.py (GateMem medical 004/002 source prefixes)',
}
KNOWN_BASELINE = {'security_tests.test_sliding_permissions.SlidingPermissionTests::'
                  'test_existing_legacy_grants_cannot_silently_lose_their_scope_limits'}


def report(path):
    suite = ET.parse(path).getroot().find('testsuite')
    modules, failed = Counter(), []
    for case in suite.iter('testcase'):
        name = case.get('classname') + '::' + case.get('name')
        modules[case.get('classname').split('.')[1] if '.' in case.get('classname') else case.get('classname')] += 1
        if case.find('failure') is not None or case.find('error') is not None:
            failed.append(name)
    return {'tests': int(suite.get('tests')), 'failures': int(suite.get('failures')), 'errors': int(suite.get('errors')),
        'skipped': int(suite.get('skipped')), 'failed': failed,
        'unexpected_failures': [f for f in failed if f not in KNOWN_BASELINE],
        'focused_modules': {m: n for m, n in sorted(modules.items()) if 'privacy_fallback' in m or m in {
            'test_scoped_holds_v19', 'test_recipient_revocation_v19', 'test_host_tasks_v19'}}}


git = lambda *a: subprocess.check_output(['git', '-C', str(ROOT), *a], text=True)
changed = sorted({*git('diff', '--name-only', 'HEAD').split(), *git('ls-files', '--others', '--exclude-standard').split()})
runtime = [p for p in changed if p.startswith(('agentic_memory/', 'evaluation/')) and p.endswith('.py') and '/tests/' not in p]
reports = {name: {'covers': covers, **report(HERE / name)} for name, covers in REPORTS.items()}
replay = json.loads((HERE / '004-01-card-replay.json').read_text())
summary = {
    'workspace': str(ROOT), 'base_revision': git('rev-parse', 'HEAD').strip(), 'uncommitted_changes': True,
    'policy_version': 'v19-independent-privacy-fallback:1',
    'source_prompt_version': 'permission-inference-v19-independent-privacy-v1',
    'assessment_prompt_version': 'privacy-assessment-v19-independent-v1',
    'result_kind': 'scripted test doubles only; NOT real-model results',
    'reports': reports,
    'known_baseline_failures': sorted(KNOWN_BASELINE),
    'known_baseline_evidence': 'analysis/v19-context-fixes/baseline-wording-failure.xml (reproduced on the unchanged source snapshot)',
    'passed': all(not r['unexpected_failures'] for r in reports.values()),
    'replay_004_01': {k: replay[k] for k in ('result_kind', 'external_model_calls', 'extra_privacy_assessment_calls',
        'processing_state', 'required_plan_details_present', 'superseded_predicate_would_block')}
        | {'finalization_outcome': replay['control_assessment']['finalization']['outcome'],
           'released_current_approved_evidence': len(replay['released_current_approved_evidence'])},
    'historical_benchmark_untouched': True, 'paid_benchmark_launched': False,
    'changed_files': changed,
    'runtime_sha256': {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in runtime},
}
(HERE / 'validation-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
print(json.dumps({k: summary[k] for k in ('passed', 'known_baseline_failures')} |
    {n: {k: r[k] for k in ('tests', 'failures', 'unexpected_failures')} for n, r in reports.items()}, indent=2))
