"""Education and household host settings, the policy requirement, and core behaviour under them.

Both hosts have no hand-written role tables: the role tables come from a generated policy
(scripts/build_role_policy.py). The policies below are small test fixtures, and semantic
labels are scripted, as in test_office_host.py. These tests show what the host and core do
with a label, not how well a real model chooses it or how good a generated policy is.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile

import pytest

from agentic_memory.privacy import PrivacyMemorySystem
from agentic_memory.privacy.attribution_v19 import mentions, names
from agentic_memory.privacy.permissions import PermissionSourceEvent, PermissionUpdater, PermissionRepairExhausted
from agentic_memory.privacy.processors import HashEmbedder
from evaluation.gatemem_medical import runner
from evaluation.gatemem_medical.adapter import load_data, schedule
from evaluation.gatemem_medical.hosts import (EDUCATION, HOUSEHOLD, HOSTS, MEDICAL, MENTIONED_ROLES, OFFICE,
    from_policy, policy_excluded_roles)
from evaluation.gatemem_medical.inference_adapter import InferenceFixtureAdapter, EmptyPermissionProcessor, subject_authorities
from evaluation.gatemem_medical.input_boundary import episode_header, entity_directory
from security_tests import test_incremental_permissions as fixtures

DATA = Path(__file__).resolve().parents[3] / 'data/gatemem/bench/data'


def policy(domain, kinds, rows, authorities=None):
    p = {'format': 'gatemem-role-policy-v2', 'domain': domain, 'model': 'test-fixture', 'prompt_version': 2,
         'information_kinds': kinds, 'role_descriptions': {r: f'Test work of a {r}.' for r in rows},
         'votes': 5, 'majority': 3, 'role_kinds': rows,
         'role_kind_votes': {r: {k: 5 * (k in v) for k in kinds} for r, v in rows.items()}}
    if authorities is not None:
        p['authorities'] = authorities
    return p


def with_policy(host, p):
    raw = json.dumps(p).encode()
    return from_policy(host, p, policy_sha256=hashlib.sha256(raw).hexdigest(), learned_access_sources=2, reply_access=True)


EDU_POLICY = policy('education', {'grades': 'Grades and evaluations.', 'logistics': 'Times, places and arrangements.',
                                  'funding': 'Aid and funding amounts.'},
                    {'professor': ['grades', 'logistics'], 'registrar': ['logistics'],
                     'advisor': ['grades', 'logistics', 'funding'], 'department_admin': ['logistics']},
                    authorities=['department_admin'])
HOME_POLICY = policy('household', {'access_logistics': 'Arrival windows, entrances, approved rooms.',
                                   'credentials': 'Door codes, alarm phrases, key locations.'},
                     {'cleaner': ['access_logistics'], 'technician': ['access_logistics'],
                      'household_manager': ['access_logistics', 'credentials']})


# ---- Host settings

def test_every_host_validates_and_the_new_ones_have_no_hand_written_tables():
    for host in HOSTS.values():
        host.validate()
    for host in (EDUCATION, HOUSEHOLD):
        assert host.requires_policy and not host.care_scopes and not host.role_information_types
        assert host.policy_source == 'hand-written'  # meaning: no generated policy loaded yet
    assert not MEDICAL.requires_policy and not OFFICE.requires_policy


def test_a_host_without_tables_must_declare_that_it_needs_a_policy():
    with pytest.raises(ValueError, match='requires_policy'):
        replace(EDUCATION, requires_policy=False).validate()


def test_the_runner_refuses_education_and_household_without_a_generated_policy(capsys):
    for domain in ('education', 'household'):
        with pytest.raises(SystemExit):
            runner.main(['--domain', domain, '--plan'])
        assert 'pass --role-policy' in capsys.readouterr().err


def test_the_runner_plans_the_full_pinned_datasets_with_a_policy(tmp_path, capsys):
    for domain, p in (('education', EDU_POLICY), ('household', HOME_POLICY)):
        path = tmp_path / f'{domain}.json'
        path.write_text(json.dumps(p))
        assert runner.main(['--domain', domain, '--role-policy', str(path), '--plan']) == 0
        plan = json.loads(capsys.readouterr().out)
        assert (plan['episodes'], plan['checkpoints'], plan['boundaries']) == runner.PINNED[domain]


def test_excluded_roles_for_the_builder_are_the_assistant_people_subjects_and_mentioned_people():
    assert policy_excluded_roles('medical') == ('agent', 'family_member', 'patient')  # as the medical run used
    assert policy_excluded_roles('office') == ('agent',)
    assert policy_excluded_roles('education') == ('agent', 'parent_guardian', 'student')
    assert set(policy_excluded_roles('household')) == {'agent', 'guest', *HOUSEHOLD.subject_roles}


@pytest.mark.parametrize('domain', ['medical', 'office', 'education', 'household'])
def test_every_principal_role_is_a_subject_a_worker_title_mentioned_or_the_assistant(domain):
    episodes, _ = load_data(DATA / domain, domain)
    roles = {p['role'] for e in episodes for p in e['entities']['principals']}
    host = HOSTS[domain]
    people_subjects = set(host.subject_roles) - set(host.entity_kinds)
    titles = roles - set(policy_excluded_roles(domain))
    assert titles and not titles & people_subjects and not titles & set(MENTIONED_ROLES[domain])
    if host.care_scopes:  # medical and office still carry their hand tables
        assert titles == set(host.care_scopes)


# ---- Education entities

EDU_EPISODE = {'episode_id': 'education_x', 'entities': {
    'principals': [{'principal_id': 'student_lina', 'role': 'student', 'display_name': 'Lina Chen'},
                   {'principal_id': 'admin_omar', 'role': 'department_admin', 'display_name': 'Omar Haddad'},
                   {'principal_id': 'prof_rossi', 'role': 'professor', 'display_name': 'Elena Rossi'}],
    'relationships': [{'type': 'research_project_member', 'project_id': 'project_atlas', 'principal_id': 'student_lina'},
                      {'type': 'program_member', 'program_id': 'program_atlas_scholars', 'principal_id': 'student_lina'},
                      {'type': 'review_case_subject', 'case_id': 'aster_waiver', 'principal_id': 'student_lina'},
                      {'type': 'delegate', 'principal_id': 'admin_omar', 'for_principal_id': 'prof_rossi',
                       'access_scope': 'calendar_only'}]}, 'turns': []}


def test_only_program_and_project_ids_cross_the_education_boundary():
    header = episode_header(EDU_EPISODE, EDUCATION.entity_kinds)
    d = header['directory']
    assert d['program_atlas_scholars'] == {'name': 'Program Atlas Scholars', 'role': 'program',
                                           'aliases': ['Program Atlas Scholars', 'Atlas Scholars']}
    assert d['project_atlas'] == {'name': 'Project Atlas', 'role': 'project', 'aliases': ['Project Atlas', 'Atlas']}
    text = json.dumps(header)
    for hidden in ('aster_waiver', 'case_id', 'calendar_only', 'research_project_member', 'relationships'):
        assert hidden not in text


def test_a_program_named_like_a_project_resolves_to_the_program():
    # Longest alias first: "Atlas Scholars" is the program, never the project "Atlas".
    from types import SimpleNamespace
    registry = names(SimpleNamespace(directory=episode_header(EDU_EPISODE, EDUCATION.entity_kinds)['directory']), {})
    assert mentions('Atlas Scholars meets Thursday.', registry, True)[0] == {'program_atlas_scholars'}
    assert mentions('Project Atlas data is confidential.', registry, True)[0] == {'project_atlas'}
    assert mentions('Atlas is separate from Atlas Scholars.', registry, True)[0] == {'project_atlas', 'program_atlas_scholars'}


def test_every_education_episode_lists_its_program_and_its_name_appears_in_its_text():
    episodes, _ = load_data(DATA / 'education', 'education')
    for e in episodes:
        entities = entity_directory(e, EDUCATION.entity_kinds)
        assert any(v['role'] == 'program' for v in entities.values()), e['episode_id']
        text = ' '.join(t['text'] for t in e['turns'])
        for entity in entities.values():
            assert entity['aliases'][1] in text, entity


def test_household_ids_do_not_become_directory_entities():
    episodes, _ = load_data(DATA / 'household', 'household')
    for e in episodes:
        header = episode_header(e, HOUSEHOLD.entity_kinds)
        assert all(v['role'] != 'household' for v in header['directory'].values())
        assert 'household_member' not in json.dumps(header)


def test_program_and_project_authorities_come_from_the_policy_and_students_get_none():
    host = with_policy(EDUCATION, EDU_POLICY)
    assert host.subject_authority_roles == ('department_admin',)
    result = subject_authorities(episode_header(EDU_EPISODE, host.entity_kinds)['directory'], host)
    assert set(result['resources']) == {'program_atlas_scholars', 'project_atlas'}
    assert all(caps == {'permission': ['admin_omar'], 'restrict': ['admin_omar']} for caps in result['authorities'].values())
    assert 'student_lina' not in result['authorities']


def test_household_policy_authorities_are_ignored_without_entity_subjects():
    host = with_policy(HOUSEHOLD, {**HOME_POLICY, 'authorities': ['household_manager']})
    assert host.subject_authority_roles == ()


def adapter(host, episode, tmp):
    core = PrivacyMemorySystem(Path(tmp) / 'memory.sqlite', HashEmbedder())
    return core, InferenceFixtureAdapter(core, episode_header(episode, host.entity_kinds),
                                         ['test:hash', 'test:permission'], EmptyPermissionProcessor(), host=host)


def test_education_bootstrap_carries_entities_authorities_conventions_and_the_policy_table():
    host = with_policy(EDUCATION, EDU_POLICY)
    with tempfile.TemporaryDirectory() as tmp:
        core, a = adapter(host, EDU_EPISODE, tmp)
        try:
            b = a.updater.bootstrap
            b.validate()
            assert b.subject_roles == ('student', 'program', 'project')
            assert b.role_information_types == {r: tuple(v) for r, v in EDU_POLICY['role_kinds'].items()}
            assert b.authorities['program_atlas_scholars']['restrict'] == ['admin_omar']
            assert b.task_convention == EDUCATION.task_convention
            assert b.context['identity_convention'] == EDUCATION.identity_convention
            assert b.learned_access_sources == 2 and b.reply_access
        finally:
            core.close()


def test_household_bootstrap_uses_the_person_conventions_and_no_authorities():
    episode = {'episode_id': 'household_x', 'entities': {'principals': [
        {'principal_id': 'resident_rina', 'role': 'primary_resident', 'display_name': 'Rina Cho'},
        {'principal_id': 'cleaner_cleo', 'role': 'cleaner', 'display_name': 'Cleo Diaz'}],
        'relationships': [{'type': 'household_member', 'household_id': 'oak_house', 'principal_id': 'resident_rina'}]},
        'turns': []}
    with tempfile.TemporaryDirectory() as tmp:
        core, a = adapter(with_policy(HOUSEHOLD, HOME_POLICY), episode, tmp)
        try:
            b = a.updater.bootstrap
            assert b.authorities == {} and b.subject_roles == HOUSEHOLD.subject_roles
            assert b.task_convention.startswith('A personal resource owner may search/read/derive')
            assert 'oak_house' not in b.directory
        finally:
            core.close()


@pytest.mark.parametrize('domain,p', [('education', EDU_POLICY), ('household', HOME_POLICY)])
def test_offline_first_boundary_ingests_every_turn(domain, p):
    episodes, checkpoints = load_data(DATA / domain, domain)
    episode = sorted(episodes, key=lambda e: e['episode_id'])[0]
    event = next(e for e in schedule(episodes, checkpoints) if e['episode_id'] == episode['episode_id'])
    with tempfile.TemporaryDirectory() as tmp:
        core, a = adapter(with_policy(HOSTS[domain], p), episode, tmp)
        try:
            a.ingest_batch({'boundary': event['boundary']}, episode['turns'][event['start']:event['start'] + 5])
            assert core.db.execute('SELECT COUNT(*) FROM permission_jobs').fetchone()[0] == 5
        finally:
            core.close()


# ---- Core behaviour under the new hosts (scripted labels)

def harness(host, directory, subjects_without_owner_rights=()):
    h = fixtures.IncrementalTests(); h.setUp()
    names_ = ['owner', 'helper', *directory]  # The shared scripted model refers to the first two.
    h.actor_names = {p: i + 1 for i, p in enumerate(names_)}
    people = [p for p, v in directory.items() if v['role'] not in host.entity_kinds]
    entity_auth = subject_authorities(directory, host)
    h.bootstrap = replace(h.bootstrap, participants={p: {} for p in names_}, resources={p: {} for p in names_},
        authorities={**{p: {cap: [p] for cap in ('permission', 'restrict', 'delegate')} for p in ['owner', 'helper', *people]},
                     **entity_auth.get('authorities', {})},
        source_identities={'connector': names_}, discover_self_owned_participants=(),
        personal_owners={p: p for p in ['owner', 'helper', *people]}, owner_read_actions=('search', 'read', 'derive'),
        directory=directory, worker_roles=host.worker_roles, care_scopes=host.care_scopes,
        information_types=host.information_types, role_information_types=host.role_information_types,
        subject_roles=host.subject_roles, temporary_care_hours=host.temporary_care_hours, timezone='UTC')
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
                         attribution_complete=bool(said), action_actors=[],
                         mentioned_people=list(d.get('mentioned', [])),
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
    h.directory = directory
    return h


def say(h, text, actor, **decision):
    h.decision = decision; h.seq += 1
    h.core.clock = lambda: '2026-04-08T09:00:00+00:00'
    job = h.updater.ingest(PermissionSourceEvent(str(h.seq), actor, 'connector', text, occurred_at='2026-04-08T09:00:00+00:00',
        context={'speaker_name': h.directory[actor]['name'], 'speaker_role': h.directory[actor]['role']}))
    h.updater.update(job['job_id'], max_attempts=2)
    h.updater.flush_bindings(max_batches=1)
    return job


def readable(h, reader):
    return set(r['text'] for r in h.core.retrieve(h.reader(reader), 'current information', k=100)['evidence'])


def stored(h):
    return [json.loads(r[0])['quote'] for r in h.core.db.execute("SELECT body FROM fact_candidates WHERE state='active'")]


def erase(h, text, speaker, subject, quotes, values):
    def plan(p):
        ids = {c['quote']: c['id'] for c in p['candidates']}
        return [{'op': 'erase_candidates', 'candidate_ids': [ids[q] for q in quotes], 'subject': h.actor(p, subject),
                 'values': values, 'timing': 'now', 'condition': None, 'effective_at': None,
                 'authority_source': p['new_source_id'], 'evidence': [p['new_source_id']],
                 'reason': 'An authority asked to remove this from memory.'}]
    h.classify = lambda p, s: 'context'; h.policy = plan
    try:
        h.decision = {}; h.seq += 1
        job = h.updater.ingest(PermissionSourceEvent(str(h.seq), speaker, 'connector', text,
            occurred_at='2026-04-08T10:00:00+00:00',
            context={'speaker_name': h.directory[speaker]['name'], 'speaker_role': h.directory[speaker]['role']}))
        return h.updater.update(job['job_id'], max_attempts=2)
    finally:
        h.policy = lambda p: []; h.classify = lambda p, s: 'instruction'


EDU = {
    'atlas_scholars': {'name': 'Program Atlas Scholars', 'role': 'program', 'aliases': ['Program Atlas Scholars', 'Atlas Scholars']},
    'lina': {'name': 'Lina Chen', 'role': 'student', 'aliases': ['Lina Chen', 'Lina']},
    'eli': {'name': 'Eli Park', 'role': 'student', 'aliases': ['Eli Park', 'Eli']},
    'rossi': {'name': 'Elena Rossi', 'role': 'professor', 'aliases': ['Elena Rossi', 'Rossi']},
    'nina': {'name': 'Nina Ross', 'role': 'advisor', 'aliases': ['Nina Ross', 'Nina']},
    'kim': {'name': 'Dana Kim', 'role': 'registrar', 'aliases': ['Dana Kim', 'Dana']},
    'omar': {'name': 'Omar Haddad', 'role': 'department_admin', 'aliases': ['Omar Haddad', 'Omar']},
    'grace': {'name': 'Grace Chen', 'role': 'parent_guardian', 'aliases': ['Grace Chen', 'Grace']}}
GRADE = 'Lina Chen earned an A- on the CHEM 240 midterm.'
AID = 'Lina Chen has a $4,200 aid award this term.'
EVENT = 'Atlas Scholars meets Thursday at 4 PM in Room 210.'


@pytest.fixture
def edu():
    h = harness(with_policy(EDUCATION, EDU_POLICY), EDU)
    try:
        say(h, GRADE, 'rossi', says=['lina'], care=['rossi'], types=['grades'])
        say(h, AID, 'nina', says=['lina'], care=['nina'], types=['funding'])
        say(h, EVENT, 'omar', says=['atlas_scholars'], care=['omar'], types=['logistics'])
        yield h
    finally:
        h.tearDown()


def test_education_staff_read_only_their_kinds_for_the_student_they_work_for(edu):
    assert GRADE in readable(edu, 'rossi') and AID not in readable(edu, 'rossi')
    assert {GRADE, AID} <= readable(edu, 'nina')
    assert not readable(edu, 'kim')  # registrar never shown working for Lina


def test_a_student_reads_their_own_records_and_not_a_classmates(edu):
    assert {GRADE, AID} <= readable(edu, 'lina')
    assert not {GRADE, AID} & readable(edu, 'eli')


def test_a_parent_is_mentioned_not_a_worker_and_reads_nothing_without_a_release(edu):
    assert not readable(edu, 'grace')


def test_program_records_go_to_staff_working_on_the_program_only(edu):
    assert EVENT in readable(edu, 'omar')
    assert EVENT not in readable(edu, 'rossi') and EVENT not in readable(edu, 'lina')


def test_a_program_authority_can_delete_a_program_value(edu):
    erase(edu, 'Remove the Atlas Scholars room number from memory.', 'omar', 'atlas_scholars', [EVENT], ['Room 210'])
    assert not any('Room 210' in q for q in stored(edu))
    assert GRADE in stored(edu)


def test_staff_without_program_authority_cannot_delete_its_information(edu):
    with pytest.raises(PermissionRepairExhausted):
        erase(edu, 'Delete the Atlas Scholars room number from memory.', 'rossi', 'atlas_scholars', [EVENT], ['Room 210'])
    assert EVENT in stored(edu)


HOME = {
    'rina': {'name': 'Rina Cho', 'role': 'primary_resident', 'aliases': ['Rina Cho', 'Rina']},
    'marcus': {'name': 'Marcus Hale', 'role': 'partner_spouse', 'aliases': ['Marcus Hale', 'Marcus']},
    'mia': {'name': 'Mia Santos', 'role': 'guest', 'aliases': ['Mia Santos', 'Mia']},
    'cleo': {'name': 'Cleo Diaz', 'role': 'cleaner', 'aliases': ['Cleo Diaz', 'Cleo']},
    'tom': {'name': 'Tom Reyes', 'role': 'technician', 'aliases': ['Tom Reyes', 'Tom']},
    'hana': {'name': 'Hana Ito', 'role': 'household_manager', 'aliases': ['Hana Ito', 'Hana']}}
# A care label counts only for a helper who speaks or is named in the turn (care_v19.labels).
CODE = 'Hana Ito has the Oak House door code, 4417.'
WINDOW = 'Cleo Diaz arrives Tuesday at 10 AM and uses the side entrance.'
GUEST = 'Hana Ito will let Mia Santos use the side entrance on Saturday afternoon.'
UNNAMED = 'The technician visit is Friday at 2 PM.'


@pytest.fixture
def home():
    h = harness(with_policy(HOUSEHOLD, HOME_POLICY), HOME)
    try:
        say(h, CODE, 'rina', says=['rina'], care=['hana'], types=['credentials'])
        say(h, WINDOW, 'rina', says=['rina'], care=['cleo'], types=['access_logistics'])
        say(h, GUEST, 'rina', says=['rina', 'mia'], care=['hana'], types=['access_logistics'])
        say(h, UNNAMED, 'rina', says=['rina'], care=['tom'], types=['access_logistics'])
        yield h
    finally:
        h.tearDown()


def test_helpers_read_only_their_kinds_for_the_resident_they_work_for(home):
    assert WINDOW in readable(home, 'cleo') and CODE not in readable(home, 'cleo')
    assert {CODE, WINDOW} <= readable(home, 'hana')


def test_a_care_label_for_a_helper_the_turn_never_names_establishes_nothing(home):
    assert not readable(home, 'tom')


def test_a_guest_named_beside_the_resident_does_not_block_the_helper(home):
    # information_owners: only residents own the access plan, so Hana needs no grant from Mia.
    assert GUEST in readable(home, 'hana')


def test_another_resident_needs_the_owners_grant(home):
    # Documented limit of this host: shared home facts belong to the resident they are attributed to.
    assert not {CODE, WINDOW, GUEST, UNNAMED} & readable(home, 'marcus')
    assert {CODE, WINDOW, GUEST, UNNAMED} <= readable(home, 'rina')
