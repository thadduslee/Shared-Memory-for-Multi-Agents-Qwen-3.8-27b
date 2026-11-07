"""Measure authorization on a disposable copy of v6, with no inference calls."""
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agentic_memory.privacy import PrivacyMemorySystem

OUT=Path(__file__).resolve().parent
SOURCE=ROOT/'runs/gatemem-medical-raw-stream-v6'


def main():
    with tempfile.TemporaryDirectory(prefix='v7-authorization-') as directory:
        for name in ['memory.sqlite','memory.privacy.sqlite']:
            src=sqlite3.connect((SOURCE/name).as_uri()+'?mode=ro',uri=True)
            target=sqlite3.connect(str(Path(directory)/name))
            src.backup(target);target.close();src.close()
        connection=sqlite3.connect(str(Path(directory)/'memory.sqlite'))
        space,vector=connection.execute('SELECT space,vector FROM representations LIMIT 1').fetchone()
        connection.close()
        class StoredVector:
            processor_id='local:minilm'
            space_id=space
            def encode(self,texts):return [json.loads(vector) for _ in texts]
        core=PrivacyMemorySystem(Path(directory)/'memory.sqlite',StoredVector())
        fixture_time=core.db.execute("SELECT value FROM metadata WHERE key LIKE 'fixture_time:%'").fetchone()[0]
        core.clock=lambda:fixture_time
        route,issuer=core.db.execute('SELECT compartment,issuer FROM permission_bootstrap').fetchone()
        host=core.issue_session(issuer,'profile',actions=['read','search','derive','manage_policy'],
            compartments=[route],processing_routes=['local:minilm'])
        queries=[];core.db.set_trace_callback(queries.append)
        started=time.monotonic();results=core.search(host,'What is the current plan?',k=16)
        elapsed=time.monotonic()-started
        core.db.set_trace_callback(None)
        sample=[r[0] for r in core.db.execute("SELECT id FROM resources WHERE kind='memory' ORDER BY created DESC LIMIT 3")]
        contexts=[host]+[core.issue_session(pid,'profile',actions=['read','search','derive'],
                    compartments=[route],processing_routes=['local:minilm'])
                    for pid in ['patient_elena_park','unknown-outsider']]
        def decisions():
            return [core.allowed(ctx,rid,action,processor='local:minilm' if action=='derive' else None)
                    for ctx in contexts for rid in sample for action in ['read','search','derive']]
        with core.transaction(write=False):cached=decisions()
        with patch.object(core,'snapshot_read',side_effect=lambda key,load:load()):
            with core.transaction(write=False):uncached=decisions()
        assert cached==uncached
        report={'mode':'disposable-v6-copy-authorization-profile','real_embedding_or_model_calls':0,
            'indexed_candidates':core.db.execute('SELECT COUNT(*) FROM representations').fetchone()[0],
            'full_authorized_search_seconds':elapsed,'returned_records':len(results),
            'ledger_load_queries_during_search':sum('SELECT p.kind,p.id,p.body' in q for q in queries),
            'cached_and_uncached_decisions_equal':True,'compared_decisions':len(cached),
            'limitations':'Stored vectors replace embedding execution to isolate authorization/search. No whole-run or model-quality speed claim.'}
        core.close()
    (OUT/'performance.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
