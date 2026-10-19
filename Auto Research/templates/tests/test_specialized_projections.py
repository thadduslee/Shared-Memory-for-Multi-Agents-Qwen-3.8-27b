"""Protected canonical provenance, source shredding and relationship behavior."""
import pytest

from memory_system.store import MemoryStore


def test_projection_requires_source_support_and_canonical_fact():
    store=MemoryStore()
    try:
        source=store.ingest_turn('e', {'turn_id':'t1','text':'The assembly weighs 12 kg.',
            'speaker':{'principal_id':'owner','role':'member'}}, 'owner')
        mid=store.conn.execute('SELECT memory_id FROM memories WHERE source_observation_id=?',(source,)).fetchone()[0]
        store.register_specialization('special_measurements_v1',{'mass':'INTEGER'},reason='typed magnitude retrieval')
        store.project_fact('special_measurements_v1',mid,{'mass':12})
        with pytest.raises(ValueError):
            store.project_fact('special_measurements_v1',mid,{'mass':99})
        store.tombstone(mid,'owner')
        assert not store.conn.execute('SELECT * FROM special_measurements_v1').fetchall()
        assert store.conn.execute('SELECT raw_text FROM observations WHERE observation_id=?',(source,)).fetchone()[0]==''
        assert not store.retrieve(requester_id='owner',owner_id='owner',episode_id='e',query='12 kg').allowed
    finally:
        store.close()
