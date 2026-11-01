"""Run the established nonmedical workflow through v16 and the real model."""
import os
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

from agentic_memory.privacy import PermissionUpdater,PrivacyMemorySystem
from agentic_memory.privacy.processors import LocalEmbedder
from evaluation.gatemem_medical import openrouter,runner

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'incremental-permissions-v15'))
import live_probe


class RemoteCompletion(live_probe.RecordedCompletion):
    def __init__(self,processor_id,base_url,model,api_key):
        super().__init__(processor_id,openrouter.BASE_URL,openrouter.MEMORY_MODEL,
                         os.environ['AMEM_OPENROUTER_API_KEY'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('--output',type=Path,required=True)
    args,_=parser.parse_known_args()
    openrouter.configure(runner.ROOT/'.env')
    live_probe.PermissionUpdater=lambda *a,**kw:PermissionUpdater(*a,version='16',**kw)
    live_probe.RecordedCompletion=RemoteCompletion
    live_probe.main()
    # A fail-closed quarantine can make a denial checkbox pass without actually
    # committing the requested revocation. Audit the final state and unrelated
    # owner access on a copy, preserving the original probe artifacts.
    db=sqlite3.connect(f'file:{args.output}/memory.privacy.sqlite?mode=ro',uri=True)
    statuses=dict(db.execute('SELECT status,count(*) FROM permission_jobs GROUP BY status'))
    assignments=[json.loads(r[0]) for r in db.execute("SELECT body FROM permission_access_state WHERE kind='assignment'")]
    audit={'all_sources_complete':statuses=={'complete':6},
           'no_active_control_holds':db.execute("SELECT count(*) FROM permission_control_holds WHERE state='active'").fetchone()[0]==0,
           'revocation_published':any(a['participant']=='coordinator' and a['status']=='revoked' for a in assignments)}
    db.close()
    with tempfile.TemporaryDirectory() as tmp:
        for name in ('memory.sqlite','memory.privacy.sqlite'):
            source=sqlite3.connect(f'file:{args.output/name}?mode=ro',uri=True)
            target=sqlite3.connect(str(Path(tmp)/name));source.backup(target);target.close();source.close()
        embedder=LocalEmbedder(os.getenv('AMEM_EMBEDDING_PATH') or None)
        core=PrivacyMemorySystem(Path(tmp)/'memory.sqlite',embedder)
        try:
            def evidence(principal):
                ctx=core.issue_session(principal,'amem-answerer',actions=['search','read','derive'],
                    compartments=['project'],processing_routes=[embedder.processor_id,'live:qwen'])
                return core.retrieve(ctx,'meeting time room',k=32)['evidence']
            audit['owner_access_survives_revocation']=bool(evidence('owner'))
            audit['coordinator_access_revoked']=not evidence('coordinator')
        finally:core.close()
    audit['passed']=all(audit.values())
    (args.output/'audit.json').write_text(json.dumps(audit,indent=2)+'\n')
    print(json.dumps(audit),flush=True)
    if not audit['passed']:raise SystemExit(1)
