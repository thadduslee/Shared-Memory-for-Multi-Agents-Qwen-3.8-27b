"""Protected negative cases run from the frozen baseline for every candidate."""
from memory_system.agent import GateMemAgent


def episode():
    return {'episode_id': 'case', 'domain': 'medical', 'entities': {'principals': [
        {'principal_id': 'patient', 'role': 'patient', 'display_name': 'River'},
        {'principal_id': 'clinician', 'role': 'clinician', 'display_name': 'Doctor Avery'},
        {'principal_id': 'delegate', 'role': 'helper', 'display_name': 'Jordan'},
        {'principal_id': 'foreign', 'role': 'patient', 'display_name': 'Morgan Vale'}],
        'relationships': []}}


def turn(tid, text, at='2026-01-01T00:00:00Z'):
    return {'turn_id': tid, 'text': text, 'timestamp': at,
            'turn_kind': 'message', 'speaker': {'principal_id': 'patient', 'role': 'patient'}}


def query(agent, requester, role='clinician', text='project review', tid='t1'):
    return agent.query({'episode_id': 'case', 'checkpoint_id': 'test', 'as_of_turn_id': tid,
                        'query_text': text, 'asker': {'principal_id': requester, 'role': role}})


def setup():
    agent = GateMemAgent(':memory:')
    agent.open_episode(episode())
    agent.ingest(turn('t1', 'The project review starts Monday at 10:00.'))
    return agent


def test_unassigned_role_alone_cannot_access_records():
    a = setup()
    try:
        assert not query(a, 'clinician')['evidence']
        source = a.store.conn.execute("SELECT observation_id FROM observations WHERE turn_id='t1'").fetchone()[0]
        a.store.upsert_relationship('assigned_clinician', 'clinician', 'patient',
                                    episode_id='case', source_observation_id=source)
        a.store.conn.commit()
        assert query(a, 'clinician')['evidence']
    finally:
        a.store.close()


def test_expired_and_revoked_consent_do_not_authorize():
    a = setup()
    try:
        source = a.store.conn.execute("SELECT observation_id FROM observations WHERE turn_id='t1'").fetchone()[0]
        pid = a.store.grant_permission('case', grantee_id='delegate', resource_owner_id='patient',
                scope=['project review'], source_observation_id=source,
                valid_from='2025-01-01', valid_until='2025-12-31')
        assert not query(a, 'delegate', 'helper')['evidence']
        a.store.conn.execute('UPDATE permissions SET valid_until=NULL WHERE permission_id=?', (pid,))
        a.store.conn.commit()
        assert query(a, 'delegate', 'helper')['evidence']
        a.store.revoke_permission(pid, source_observation_id=source)
        assert not query(a, 'delegate', 'helper')['evidence']
    finally:
        a.store.close()


def test_assignment_is_scoped_to_patient_and_time():
    a = setup()
    try:
        source = a.store.conn.execute("SELECT observation_id FROM observations WHERE turn_id='t1'").fetchone()[0]
        rel = a.store.upsert_relationship('assigned_clinician', 'clinician', 'foreign',
                                         episode_id='case', source_observation_id=source)
        assert not query(a, 'clinician')['evidence']
        a.store.conn.execute('UPDATE relationships SET object_id=?,valid_until=? WHERE relationship_id=?',
                             ('patient', '2025-12-31', rel))
        a.store.conn.commit()
        assert not query(a, 'clinician')['evidence']
        a.store.upsert_relationship('assigned_clinician', 'clinician', 'patient',
            episode_id='case', source_observation_id=source,
            attributes={'valid_from': '2027-01-01'})
        a.store.conn.commit()
        assert not query(a, 'clinician')['evidence']
    finally:
        a.store.close()


def test_deletion_blocks_owner_and_assigned_requester():
    a = setup()
    try:
        rid = query(a, 'patient', 'patient')['evidence'][0]['record_id']
        a.store.tombstone(rid, 'patient')
        assert not query(a, 'patient', 'patient')['evidence']
        assert '10:00' not in query(a, 'patient', 'patient')['fallback_answer']
    finally:
        a.store.close()
