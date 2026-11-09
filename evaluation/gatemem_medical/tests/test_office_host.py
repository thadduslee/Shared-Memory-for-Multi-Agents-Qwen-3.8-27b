"""Office host settings: projects as subjects, the role table, and project authorities.

Semantic labels are scripted fixture data, as in security_tests/test_stage_fixes_v19.py.
These tests show what the host and core do with a label, not how well a real model
chooses it.
"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile

import pytest

from agentic_memory.privacy import PrivacyMemorySystem
from agentic_memory.privacy.permissions import PermissionSourceEvent, PermissionUpdater, PermissionRepairExhausted
from agentic_memory.privacy.processors import HashEmbedder
from evaluation.gatemem_medical.adapter import load_data, schedule
from evaluation.gatemem_medical.hosts import MEDICAL, OFFICE, HOSTS, Host
from evaluation.gatemem_medical.inference_adapter import (InferenceFixtureAdapter, EmptyPermissionProcessor,
    subject_authorities, CARE_SCOPES, ROLE_INFORMATION_TYPES, SUBJECT_ROLES)
from evaluation.gatemem_medical.input_boundary import episode_header, entity_directory
from security_tests import test_incremental_permissions as fixtures

OFFICE_DATA = Path(__file__).resolve().parents[3]/'data/gatemem'/'bench/data/office'
needs_data = pytest.mark.skipif(not OFFICE_DATA.is_dir(), reason='GateMem office fixture not present')


# ---- Host tables

def test_both_hosts_validate_and_the_office_table_uses_only_its_types():
    for host in HOSTS.values():
        host.validate()
    assert {t for row in OFFICE.role_information_types.values() for t in row} <= set(OFFICE.information_types)


def test_medical_names_kept_for_importers_are_unchanged():
    assert SUBJECT_ROLES == ('patient',) and MEDICAL.temporary_care_hours == 24
    assert CARE_SCOPES is MEDICAL.care_scopes and ROLE_INFORMATION_TYPES is MEDICAL.role_information_types
    assert MEDICAL.worker_roles == (*MEDICAL.care_scopes, 'agent')
    assert not MEDICAL.entity_kinds and not MEDICAL.subject_authority_roles and not MEDICAL.task_convention


def test_a_table_with_an_unknown_type_is_refused():
    with pytest.raises(ValueError, match='unknown information types'):
        replace(OFFICE, role_information_types={**OFFICE.role_information_types, 'sre': ('pager',)}).validate()


def test_no_office_reader_gets_credentials_with_customer_identity():
    # The two most sensitive types never meet in one role.
    assert not [r for r, row in OFFICE.role_information_types.items() if {'credential', 'customer'} <= set(row)]


@needs_data
def test_every_office_role_in_the_fixture_is_a_worker_or_subject():
    episodes, _ = load_data(OFFICE_DATA, 'office')
    roles = {p['role'] for e in episodes for p in e['entities']['principals']}
    assert roles <= set(OFFICE.worker_roles), roles - set(OFFICE.worker_roles)


# ---- Input boundary

EPISODE = {'episode_id': 'office_x', 'entities': {
    'principals': [{'principal_id': 'pm_ava', 'role': 'product_manager', 'display_name': 'Ava Lin'},
                   {'principal_id': 'sec_pri', 'role': 'security', 'display_name': 'Priya Rao'},
                   {'principal_id': 'mgr_alex', 'role': 'manager', 'display_name': 'Alex Romero'},
                   {'principal_id': 'eng_noah', 'role': 'engineer', 'display_name': 'Noah Reed'}],
    'relationships': [{'type': 'project_member', 'project_id': 'project_maple', 'principal_id': 'pm_ava'},
                      {'type': 'contractor_for_project', 'project_id': 'project_maplemark', 'principal_id': 'eng_noah'},
                      {'type': 'executive_delegate', 'principal_id': 'eng_noah', 'for_principal_id': 'pm_ava',
                       'access_scope': 'scheduling_only'}]}, 'turns': []}


def test_only_project_ids_cross_the_boundary():
    header = episode_header(EPISODE, ('project',))
    assert header['directory']['project_maple'] == {'name': 'Project Maple', 'role': 'project',
                                                    'aliases': ['Project Maple', 'Maple']}
    text = json.dumps(header)
    for hidden in ('project_member', 'contractor_for_project', 'scheduling_only', 'for_principal_id', 'relationships'):
        assert hidden not in text


def test_medical_header_is_unchanged_without_entity_kinds():
    assert episode_header(EPISODE) == {'episode_id': 'office_x', 'directory': {
        pid: v for pid, v in episode_header(EPISODE, ('project',))['directory'].items() if not pid.startswith('project_')}}


def test_entity_ids_that_collide_with_people_are_refused():
    clash = {**EPISODE, 'entities': {**EPISODE['entities'], 'principals': EPISODE['entities']['principals'] +
             [{'principal_id': 'project_maple', 'role': 'employee', 'display_name': 'Maple'}]}}
    with pytest.raises(ValueError, match='collide'):
        episode_header(clash, ('project',))


@needs_data
def test_every_office_project_name_appears_in_its_episode_text():
    episodes, _ = load_data(OFFICE_DATA, 'office')
    for e in episodes:
        projects = entity_directory(e, ('project',))
        assert len(projects) == 2
        text = ' '.join(t['text'] for t in e['turns'])
        for person in projects.values():
            assert person['aliases'][1] in text, person


# ---- Project authorities

def test_authorities_go_to_configured_roles_for_every_project():
    directory = episode_header(EPISODE, ('project',))['directory']
    result = subject_authorities(directory, OFFICE)
    assert set(result['resources']) == {'project_maple', 'project_maplemark'}
    for caps in result['authorities'].values():
        assert caps == {'permission': ['mgr_alex', 'pm_ava', 'sec_pri'], 'restrict': ['mgr_alex', 'pm_ava', 'sec_pri']}
    assert set(result['participants']) == {'mgr_alex', 'pm_ava', 'sec_pri'}


def test_person_subjects_get_no_host_authority():
    directory = {'pat': {'name': 'Pat', 'role': 'patient', 'aliases': ['Pat']},
                 'nia': {'name': 'Nia', 'role': 'nurse', 'aliases': ['Nia']}}
    assert subject_authorities(directory, replace(MEDICAL, subject_authority_roles=('nurse',))) == {}


def adapter(host, episode, tmp):
    core = PrivacyMemorySystem(Path(tmp) / 'memory.sqlite', HashEmbedder())
    return core, InferenceFixtureAdapter(core, episode_header(episode, host.entity_kinds),
                                         ['test:hash', 'test:permission'], EmptyPermissionProcessor(), host=host)


def test_office_adapter_bootstrap_carries_projects_authorities_and_table():
    with tempfile.TemporaryDirectory() as tmp:
        core, a = adapter(OFFICE, EPISODE, tmp)
        try:
            b = a.updater.bootstrap
            b.validate()
            assert b.subject_roles == ('project',) and b.role_information_types is OFFICE.role_information_types
            assert b.authorities['project_maple']['restrict'] == ['mgr_alex', 'pm_ava', 'sec_pri']
            assert 'project_maple' not in b.personal_owners
            assert b.task_convention == OFFICE.task_convention
            assert b.context['identity_convention'] == OFFICE.identity_convention
        finally:
            core.close()


def test_medical_adapter_bootstrap_has_no_host_authorities():
    episode = {'episode_id': 'med_x', 'entities': {'principals': [
        {'principal_id': 'pat', 'role': 'patient', 'display_name': 'Pat Lane'}], 'relationships': []}, 'turns': []}
    with tempfile.TemporaryDirectory() as tmp:
        core, a = adapter(MEDICAL, episode, tmp)
        try:
            b = a.updater.bootstrap
            assert b.authorities == {} and b.resources == {} and b.subject_roles == ('patient',)
            assert b.task_convention.startswith('A personal resource owner may search/read/derive')
        finally:
            core.close()


@needs_data
def test_offline_office_episode_ingests_every_turn_of_its_first_boundary():
    episodes, checkpoints = load_data(OFFICE_DATA, 'office')
    episode = sorted(episodes, key=lambda e: e['episode_id'])[0]
    event = next(e for e in schedule(episodes, checkpoints) if e['episode_id'] == episode['episode_id'])
    with tempfile.TemporaryDirectory() as tmp:
        core, a = adapter(OFFICE, episode, tmp)
        try:
            turns = episode['turns'][event['start']:event['start'] + 5]
            a.ingest_batch({'boundary': event['boundary']}, turns)
            jobs = core.db.execute('SELECT COUNT(*) FROM permission_jobs').fetchone()[0]
            assert jobs == 5
        finally:
            core.close()


# ---- Core behaviour under the office settings (scripted labels)

DIRECTORY = {
    'maple': {'name': 'Project Maple', 'role': 'project', 'aliases': ['Project Maple', 'Maple']},
    'maplemark': {'name': 'Project Maplemark', 'role': 'project', 'aliases': ['Project Maplemark', 'Maplemark']},
    'ava': {'name': 'Ava Lin', 'role': 'product_manager', 'aliases': ['Ava Lin', 'Ava']},
    'pri': {'name': 'Priya Rao', 'role': 'security', 'aliases': ['Priya Rao', 'Priya']},
    'noah': {'name': 'Noah Reed', 'role': 'engineer', 'aliases': ['Noah Reed', 'Noah']},
    'rae': {'name': 'Rachel Ng', 'role': 'sales', 'aliases': ['Rachel Ng', 'Rachel']},
    'erin': {'name': 'Erin Blake', 'role': 'executive_assistant', 'aliases': ['Erin Blake', 'Erin']},
    'alex': {'name': 'Alex Romero', 'role': 'manager', 'aliases': ['Alex Romero', 'Alex']},
    'leo': {'name': 'Leo Park', 'role': 'contractor', 'aliases': ['Leo Park', 'Leo']}}
PEOPLE = [p for p, v in DIRECTORY.items() if v['role'] != 'project']
TOKEN = 'The Maple staging token is mp_stg_4R2N-K8QM-7T1C.'
DIAGNOSIS = 'The Maple diagnosis is stale environment-variable sync.'
CUSTOMER = 'The Maple customer is Granite Mutual.'
DATE = 'Maple targets July 31, 2026.'


def harness(project_authorities=True):
    h = fixtures.IncrementalTests(); h.setUp()
    names = ['owner', 'helper', *DIRECTORY]  # The shared scripted model refers to the first two.
    h.actor_names = {p: i + 1 for i, p in enumerate(names)}
    projects = subject_authorities(DIRECTORY, OFFICE) if project_authorities else {'authorities': {}}
    h.bootstrap = replace(h.bootstrap, participants={p: {} for p in names}, resources={p: {} for p in names},
        authorities={**{p: {cap: [p] for cap in ('permission', 'restrict', 'delegate')} for p in ['owner', 'helper', *PEOPLE]},
                     **projects['authorities']},
        source_identities={'connector': names}, discover_self_owned_participants=(),
        personal_owners={p: p for p in ['owner', 'helper', *PEOPLE]}, owner_read_actions=('search', 'read', 'derive'),
        directory=DIRECTORY, worker_roles=OFFICE.worker_roles, care_scopes=OFFICE.care_scopes,
        information_types=OFFICE.information_types, role_information_types=OFFICE.role_information_types,
        subject_roles=OFFICE.subject_roles, temporary_care_hours=OFFICE.temporary_care_hours, timezone='UTC')
    h.updater = PermissionUpdater(h.core, h.host, 'work', h.bootstrap, h.model, version='19')
    h.classify = lambda p, s: 'instruction'; h.source_subject = None; h.decision = {}
    original = h.model.complete

    def reasoned(system, payload, *, schema=None):
        response = original(system, payload, schema=schema)
        if 'f' not in response:
            return response
        d = h.decision
        for f in response['f'].values():
            if f['kind'] in {'instruction', 'asserted_fact'}:
                said = [h.actor(payload, p) for p in d.get('says', [])]
                f.update(information_subject=said[0] if len(said) == 1 else None, information_subjects=said,
                         attribution_complete=bool(said), action_actors=[], mentioned_people=[],
                         care_participants=[h.actor(payload, p) for p in d.get('care', [])],
                         information_types=list(d.get('types', [])))
                if payload.get('temporary_care'):
                    f.update(temporary_workers=[], ended_workers=[])
        return response
    h.model.complete = reasoned
    handler = h.model.handler

    def without_unrequested_reviews(p):
        result = handler(p)
        if p.get('stage') == 'combined_incremental' and not p.get('required_pairs'):
            result['binding_reviews'] = []
        return result
    h.model.handler = without_unrequested_reviews
    return h


@pytest.fixture
def h():
    h = harness()
    try:
        yield h
    finally:
        h.tearDown()


@pytest.fixture
def h_without_authorities():
    h = harness(project_authorities=False)
    try:
        yield h
    finally:
        h.tearDown()


def say(h, text, actor, **decision):
    h.decision = decision; h.seq += 1
    h.core.clock = lambda: '2026-04-06T09:00:00+00:00'
    job = h.updater.ingest(PermissionSourceEvent(str(h.seq), actor, 'connector', text, occurred_at='2026-04-06T09:00:00+00:00',
        context={'speaker_name': DIRECTORY[actor]['name'], 'speaker_role': DIRECTORY[actor]['role']}))
    h.updater.update(job['job_id'], max_attempts=2)
    h.updater.flush_bindings(max_batches=1)
    return job


def readable(h, reader):
    return set(r['text'] for r in h.core.retrieve(h.reader(reader), 'current information', k=100)['evidence'])


def stored(h):
    return [json.loads(r[0])['quote'] for r in h.core.db.execute("SELECT body FROM fact_candidates WHERE state='active'")]


def seed(h):
    say(h, TOKEN, 'pri', says=['maple'], care=['pri'], types=['credential'])
    say(h, DIAGNOSIS, 'noah', says=['maple'], care=['noah'], types=['incident'])
    say(h, CUSTOMER, 'rae', says=['maple'], care=['rae'], types=['customer'])
    say(h, DATE, 'ava', says=['maple'], care=['ava'], types=['schedule'])
    say(h, 'Maple has a sponsor briefing on Friday.', 'erin', says=['maple'], care=['erin'], types=['schedule'])
    say(h, 'Maplemark demo moves to Monday.', 'alex', says=['maplemark'], care=['alex'], types=['schedule'])


def erase(h, text, speaker, quotes, values):
    def policy(p):
        ids = {c['quote']: c['id'] for c in p['candidates']}
        return [{'op': 'erase_candidates', 'candidate_ids': [ids[q] for q in quotes], 'subject': h.actor(p, 'maple'),
                 'values': values, 'timing': 'now', 'condition': None, 'effective_at': None,
                 'authority_source': p['new_source_id'], 'evidence': [p['new_source_id']],
                 'reason': 'A project authority asked to remove this from memory.'}]
    h.classify = lambda p, s: 'context'; h.policy = policy
    try:
        h.decision = {}; h.seq += 1
        job = h.updater.ingest(PermissionSourceEvent(str(h.seq), speaker, 'connector', text,
            occurred_at='2026-04-06T10:00:00+00:00',
            context={'speaker_name': DIRECTORY[speaker]['name'], 'speaker_role': DIRECTORY[speaker]['role']}))
        return h.updater.update(job['job_id'], max_attempts=2)
    finally:
        h.policy = lambda p: []; h.classify = lambda p, s: 'instruction'


def test_each_role_reads_only_its_types_on_the_project_it_works_on(h):
    seed(h)
    assert {TOKEN, DIAGNOSIS} <= readable(h, 'pri') and CUSTOMER not in readable(h, 'pri')
    assert {TOKEN, DIAGNOSIS} <= readable(h, 'noah') and CUSTOMER not in readable(h, 'noah')
    assert CUSTOMER in readable(h, 'rae') and not {TOKEN, DIAGNOSIS} & readable(h, 'rae')
    assert {CUSTOMER, DIAGNOSIS, DATE} <= readable(h, 'ava') and TOKEN not in readable(h, 'ava')
    assert DATE in readable(h, 'erin') and not {TOKEN, DIAGNOSIS, CUSTOMER} & readable(h, 'erin')


def test_working_on_the_sibling_project_reads_nothing_of_this_one(h):
    seed(h)
    seen = readable(h, 'alex')
    assert 'Maplemark demo moves to Monday.' in seen
    assert not {TOKEN, DIAGNOSIS, CUSTOMER, DATE} & seen


def test_staff_never_shown_working_on_a_project_read_nothing(h):
    seed(h)
    assert not readable(h, 'leo')


def test_a_project_authority_can_delete_a_project_value(h):
    seed(h)
    erase(h, 'Remove the exact Maple staging token from memory.', 'pri', [TOKEN], ['mp_stg_4R2N-K8QM-7T1C'])
    assert not any('mp_stg_4R2N' in q for q in stored(h))
    assert not any('mp_stg_4R2N' in t for t in readable(h, 'noah'))
    assert DIAGNOSIS in readable(h, 'noah') and CUSTOMER in stored(h)


def test_staff_without_project_authority_cannot_delete_its_information(h):
    seed(h)
    with pytest.raises(PermissionRepairExhausted):
        erase(h, 'Delete the Maple staging token from memory.', 'noah', [TOKEN], ['mp_stg_4R2N-K8QM-7T1C'])
    assert TOKEN in stored(h)


def test_without_project_authorities_even_security_cannot_delete_project_information(h_without_authorities):
    # Control for the test above: the configured authority is what makes deletion possible.
    h = h_without_authorities
    seed(h)
    with pytest.raises(PermissionRepairExhausted):
        erase(h, 'Remove the exact Maple staging token from memory.', 'pri', [TOKEN], ['mp_stg_4R2N-K8QM-7T1C'])
    assert TOKEN in stored(h)
