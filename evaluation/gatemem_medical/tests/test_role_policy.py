"""Generated role policies (scripts/build_role_policy.py) and reply metadata at the input boundary."""
import importlib.util
import json
from pathlib import Path

import pytest

from evaluation.gatemem_medical.hosts import MEDICAL, OFFICE, POLICY_FORMAT, from_policy
from evaluation.gatemem_medical.input_boundary import source_turn

ROOT = Path(__file__).resolve().parents[3]
GATEMEM = Path(__file__).resolve().parents[3]/'data/gatemem'
spec = importlib.util.spec_from_file_location('build_role_policy', ROOT / 'scripts/build_role_policy.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def policy(domain, roles, **extra):
    return {'format': POLICY_FORMAT, 'domain': domain,
            'information_kinds': {'appointments': 'Visits.', 'medications': 'Doses.'},
            'role_descriptions': {r: f'{r} work.' for r in roles},
            'role_kinds': {r: ['appointments'] for r in roles}, **extra}


def test_policy_replaces_the_hand_tables_and_keeps_the_host_conventions():
    roles = list(MEDICAL.care_scopes)
    host = from_policy(MEDICAL, policy('medical', roles, authorities=['clinician']), policy_sha256='ab' * 32,
                       learned_access_sources=2, reply_access=True)
    assert host.information_types == {'appointments': 'Visits.', 'medications': 'Doses.'}
    assert host.role_information_types == {r: ('appointments',) for r in roles}
    assert host.care_scopes == {r: f'{r} work.' for r in roles}
    assert host.subject_roles == MEDICAL.subject_roles and host.temporary_care_hours == MEDICAL.temporary_care_hours
    # A patient controls their own records, so a staff authority list is not used.
    assert host.subject_authority_roles == ()
    assert (host.learned_access_sources, host.reply_access, host.policy_source) == (2, True, 'policy-sha256:' + 'ab' * 32)


def test_project_subjects_take_their_authorities_from_the_policy():
    roles = list(OFFICE.care_scopes)
    host = from_policy(OFFICE, policy('office', roles, authorities=['security', 'manager']), policy_sha256='cd' * 32)
    assert host.subject_authority_roles == ('security', 'manager')
    assert host.task_convention == OFFICE.task_convention and host.entity_kinds == ('project',)


def test_a_policy_for_another_domain_or_format_is_refused():
    with pytest.raises(ValueError):
        from_policy(MEDICAL, policy('office', list(MEDICAL.care_scopes)), policy_sha256='0' * 64)
    with pytest.raises(ValueError):
        from_policy(MEDICAL, {**policy('medical', list(MEDICAL.care_scopes)), 'format': 'v1'}, policy_sha256='0' * 64)


def test_a_row_naming_an_undefined_kind_is_refused():
    p = policy('medical', list(MEDICAL.care_scopes))
    p['role_kinds']['nurse'] = ['appointments', 'billing']
    with pytest.raises(ValueError):
        from_policy(MEDICAL, p, policy_sha256='0' * 64)


def test_hand_written_hosts_are_unchanged_and_learning_is_off():
    for host in (MEDICAL, OFFICE):
        assert host.policy_source == 'hand-written' and host.learned_access_sources == 0 and host.reply_access is False


def test_a_strict_majority_of_votes_opens_a_cell():
    titles, kinds = ['nurse', 'scheduler'], {'appointments': '', 'medications': ''}
    votes = [{'role_kinds': {'nurse': ['appointments', 'medications'], 'scheduler': ['appointments']}, 'authorities': ['nurse']},
             {'role_kinds': {'nurse': ['medications'], 'scheduler': ['appointments', 'medications']}, 'authorities': []},
             {'role_kinds': {'nurse': ['medications'], 'scheduler': []}, 'authorities': ['nurse']}]
    result = builder.tally(votes, titles, kinds, authorities=True)
    assert result['majority'] == 2
    assert result['role_kinds'] == {'nurse': ['medications'], 'scheduler': ['appointments']}
    assert result['role_kind_votes']['nurse'] == {'appointments': 1, 'medications': 3}
    assert result['authorities'] == ['nurse']


def test_vote_replies_must_cover_every_title_once_with_defined_kinds():
    kinds = {'appointments': '', 'medications': ''}
    ok = {'roles': [{'role': 'nurse', 'kinds': ['medications'], 'reason': 'r'},
                    {'role': 'scheduler', 'kinds': ['appointments'], 'reason': 'r'}]}
    assert builder.check_step2(ok, ['nurse', 'scheduler'], kinds, False)['role_kinds']['nurse'] == ['medications']
    for bad in ({'roles': ok['roles'][:1]}, {'roles': ok['roles'] + ok['roles'][:1]},
                {'roles': [{**ok['roles'][0], 'kinds': ['billing']}, ok['roles'][1]]}):
        with pytest.raises(ValueError):
            builder.check_step2(bad, ['nurse', 'scheduler'], kinds, False)


def test_reply_metadata_crosses_the_boundary_only_when_a_turn_has_it():
    turn = {'turn_id': 't2', 'speaker': {'principal_id': 'a'}, 'text': 'x', 'timestamp': '2026-01-01T09:00'}
    assert 'reply_to' not in source_turn(turn)
    assert source_turn({**turn, 'reply_to': 't1'})['reply_to'] == 't1'
    with pytest.raises(ValueError):
        source_turn({**turn, 'reply_to': 1})


@pytest.mark.skipif(not GATEMEM.exists(), reason='GateMem checkout not present')
@pytest.mark.parametrize('domain', ['medical', 'office', 'education', 'household'])
def test_no_gatemem_turn_carries_reply_metadata_so_reply_access_is_inactive(domain):
    turns = 0
    for line in (GATEMEM / 'bench/data' / domain / 'episodes.jsonl').open():
        for turn in json.loads(line)['turns']:
            turns += 1
            assert 'reply_to' not in turn and 'reply_to' not in source_turn(turn)
    assert turns > 1000
