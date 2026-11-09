"""The domain pipeline reproduces the archived medical run's derived files exactly, and refuses bad inputs.

No model, GPU or network is used: the strict rejudge test replays the GPT-4.1 replies
saved in runs/medical/gpt-4.1-strict-rejudge/cache/judge-cache.jsonl.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PIPELINE = Path(__file__).resolve().parents[1]
REPO = PIPELINE.parent
sys.path.insert(0, str(PIPELINE))
import common  # noqa: E402
import compile_summary  # noqa: E402
import export_run  # noqa: E402
import freeze_policy  # noqa: E402

MEDICAL = REPO / 'runs/medical'
MEDICAL_POLICY = REPO / 'policies/medical-v2-20261108T165625Z'


def test_combined_predictions_reproduce_the_gpt41_rejudge_source():
    lines, files = common.combined_predictions(MEDICAL, 'medical')
    import hashlib
    assert hashlib.sha256(''.join(lines).encode()).hexdigest() == \
        json.loads((MEDICAL / 'gpt-4.1-rejudge/manifest.json').read_text())['source']['official_predictions_sha256']
    strict = json.loads((MEDICAL / 'gpt-4.1-strict-rejudge/manifest.json').read_text())['source_episode_files']
    assert files == strict


def test_compile_summary_reproduces_the_archived_medical_summary():
    new = compile_summary.compile_run(MEDICAL, compile_summary.DEFAULT_BASELINES['medical'],
                                      compile_summary.AS_RUN_SPLITS['medical-as-run'])
    old = json.loads((MEDICAL / 'summary.json').read_text())
    assert new['overall'] == old['overall'] and new['coverage'] == old['coverage']
    assert new['full_scorer_metrics'] == old['full_scorer_metrics']
    cmp_old, cmp_new = old['comparison_to_medical_domain_original'], new['comparison_to_baseline']
    assert cmp_new['baseline'] == cmp_old['original']
    assert cmp_new['delta_this_run_minus_baseline'] == cmp_old['delta_this_run_minus_original']
    assert cmp_new['checkpoint_changes'] == cmp_old['checkpoint_changes']
    assert new['by_backend']['soclaas'] == old['by_backend']['soclaas (episodes 001-006)']
    assert new['by_backend']['local'] == old['by_backend']['local-vllm (episodes 007-021)']
    strip = lambda rows: [{k: v for k, v in r.items() if k != 'backend'} for r in rows]
    assert strip(new['per_episode']) == strip(old['per_episode'])


def test_freezing_the_medical_policy_reproduces_the_autopilot_files(tmp_path):
    freeze_policy.freeze('medical', MEDICAL_POLICY, tmp_path)
    for name in ('role_policy.json', 'role_policy.json.sha256', 'role_policy.provenance.json', 'policy-comparison.md'):
        assert (tmp_path / name).read_bytes() == (MEDICAL / name).read_bytes(), name


def test_freeze_refuses_a_different_policy_in_an_existing_run(tmp_path):
    freeze_policy.freeze('medical', MEDICAL_POLICY, tmp_path)
    other = json.loads((MEDICAL_POLICY / 'policy.json').read_text())
    other['role_kinds']['billing'] = ['patient_identifiers']
    path = tmp_path / 'other.json'
    path.write_text(json.dumps(other))
    with pytest.raises(SystemExit, match='different policy'):
        freeze_policy.freeze('medical', path, tmp_path)


@pytest.mark.parametrize('change,message', [
    (lambda p: p.update(domain='office'), 'not a gatemem-role-policy-v2 policy'),
    (lambda p: p['role_kinds'].pop('billing'), 'not the medical job titles'),
    (lambda p: p['role_kinds'].update(billing=[]), 'could read nothing'),
])
def test_freeze_refuses_bad_policies(tmp_path, change, message):
    policy = json.loads((MEDICAL_POLICY / 'policy.json').read_text())
    change(policy)
    with pytest.raises(SystemExit, match=message):
        freeze_policy.check('medical', json.dumps(policy).encode())


def test_freeze_requires_authorities_for_entity_subjects():
    titles = freeze_policy.job_titles(common.DATA / 'bench/data/education/episodes.jsonl',
                                      set(freeze_policy.policy_excluded_roles('education')))
    policy = {'format': 'gatemem-role-policy-v2', 'domain': 'education', 'model': 'm', 'prompt_version': 2,
              'information_kinds': {'k': 'K.'}, 'role_descriptions': {t: 'd' for t in titles},
              'role_kinds': {t: ['k'] for t in titles}, 'role_kind_votes': {t: {'k': 5} for t in titles},
              'votes': 5, 'majority': 3, 'authorities': []}
    with pytest.raises(SystemExit, match='no authorities'):
        freeze_policy.check('education', json.dumps(policy).encode())
    freeze_policy.check('education', json.dumps(policy).encode(), allow_no_authorities=True)
    policy['authorities'] = ['department_admin']
    _, host = freeze_policy.check('education', json.dumps(policy).encode())
    assert host.subject_authority_roles == ('department_admin',)


def test_strict_rejudge_replays_the_medical_verdicts_byte_for_byte(tmp_path):
    (tmp_path / 'cache').mkdir()
    shutil.copy(MEDICAL / 'gpt-4.1-strict-rejudge/cache/judge-cache.jsonl', tmp_path / 'cache')
    subprocess.run([sys.executable, str(PIPELINE / 'rejudge_strict.py'), str(MEDICAL), '--replay-only',
                    '--out', str(tmp_path)], check=True, capture_output=True)
    assert (tmp_path / 'verdicts.jsonl').read_bytes() == (MEDICAL / 'gpt-4.1-strict-rejudge/verdicts.jsonl').read_bytes()
    new, old = (json.loads((d / 'summary.json').read_text()) for d in (tmp_path, MEDICAL / 'gpt-4.1-strict-rejudge'))
    for key in ('metrics', 'coverage', 'context_source', 'context_source_counts', 'judge', 'mode', 'complete'):
        assert new[key] == old[key], key


def test_replay_only_refuses_to_call_the_model(tmp_path):
    result = subprocess.run([sys.executable, str(PIPELINE / 'rejudge_strict.py'), str(MEDICAL), '--replay-only',
                             '--out', str(tmp_path)], capture_output=True, text=True)
    assert result.returncode != 0 and 'replay-only' in result.stderr


@pytest.mark.parametrize('folder', ['runs/medical', 'references/medical_domain_original', 'references/office_domain',
                                    'policies', 'data'])
def test_every_exported_file_matches_its_manifest(folder):
    assert export_run.verify(REPO / folder) == []


def test_export_leaves_out_state_and_credentials_and_records_them(tmp_path):
    src = tmp_path / 'src'
    (src / 'results/_001_').mkdir(parents=True)
    (src / 'results/_001_/scores.jsonl').write_text('{}\n')
    (src / 'results/_001_/memory.sqlite').write_bytes(b'x' * 10)
    (src / 'results/_001_/run.lock').write_text('')
    (src / 'model.env').write_text('AMEM_LLM_API_KEY=secret\n')
    (src / 'frozen-source').mkdir()
    (src / 'frozen-source/a.py').write_text('pass\n')
    m = export_run.export(src, tmp_path / 'dest', ('frozen-source',))
    assert [f['path'] for f in m['files']] == ['results/_001_/scores.jsonl']
    left = {f['path']: f for f in m['left_out']}
    assert set(left) == {'frozen-source/a.py', 'model.env', 'results/_001_/memory.sqlite', 'results/_001_/run.lock'}
    assert 'sha256' not in left['model.env'] and 'secret' not in json.dumps(m)
    assert not (tmp_path / 'dest/model.env').exists()


@pytest.mark.parametrize('domain', common.DOMAINS)
def test_episode_tags_are_unique_and_cover_every_episode(domain):
    tags = common.episode_tags(domain)
    assert len(tags) == len(set(tags)) == len(common.episodes(domain))


def test_status_runs_on_the_medical_run():
    out = subprocess.run([sys.executable, str(PIPELINE / 'status.py'), str(MEDICAL)], check=True,
                         capture_output=True, text=True).stdout
    assert 'checkpoints scored: 579/579' in out and 'utility:        181/210' in out
