"""Live nonmedical regression: a later field denial preserves permitted logistics."""
import argparse
import json
import os
from pathlib import Path
from agentic_memory.privacy import PrivacyMemorySystem,PermissionBootstrap,PermissionSourceEvent,PermissionUpdater
from agentic_memory.privacy.processors import LocalEmbedder
from evaluation.gatemem_medical import openrouter,runner
from run import RecordedCompletion

p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
args=p.parse_args();os.umask(0o077);args.output.mkdir(parents=True,exist_ok=False)
openrouter.configure(runner.ROOT/'.env')
model=RecordedCompletion('probe:qwen',openrouter.BASE_URL,openrouter.MEMORY_MODEL,
    os.environ['AMEM_OPENROUTER_API_KEY']);model.trace=args.output/'model-trace.jsonl'
embedder=LocalEmbedder(os.getenv('AMEM_EMBEDDING_PATH') or None)
core=PrivacyMemorySystem(args.output/'memory.sqlite',embedder)
host=core.issue_session('source-service','ingest',actions=['manage_policy','ingest','read','search','derive','audit','write','delete','link'],
    compartments=['project'],processing_routes=[embedder.processor_id,model.processor_id])
core.register_processor(host,model)
bootstrap=PermissionBootstrap(participants={},resources={},authorities={},source_identities={'authenticated-chat':[]},
    discover_self_owned_participants=('authenticated-chat',),read_pipeline_actions=('search','derive'),
    task_convention='An authenticated subject controls their own information. Their particular acceptance of a task permits '
        'each involved recipient only necessary information for that task. Explicit limits constrain all inferred access. '
        'Roles, names and ownership alone grant no read access.')
updater=PermissionUpdater(core,host,'project',bootstrap,model,version='16')
audit={'initial_access_empty':not any(updater.access_state().values())}
def ingest(n,actor,text):
    job=updater.ingest(PermissionSourceEvent(str(n),actor,'authenticated-chat',text))
    result=updater.update(job['job_id']);print(n,result['status'],flush=True)
def evidence(actor):
    ctx=core.issue_session(actor,'answer',actions=['search','read','derive'],compartments=['project'],
        processing_routes=[embedder.processor_id,model.processor_id])
    return core.retrieve(ctx,'workshop date time room',k=32)['evidence']
try:
    ingest(1,'helper','May I coordinate your workshop logistics?')
    ingest(2,'owner','Yes, please coordinate my workshop. You may read the workshop date, time and room only. '
        'The workshop is Tuesday at 10:00 in room Oak. My confidential budget is 42000.')
    audit['initial_logistics_available']='10:00' in str(evidence('helper')) and 'Oak' in str(evidence('helper'))
    ingest(3,'owner','Keep sharing workshop dates, times and room with the helper. Do not share my budget or assessment notes with the helper.')
    context=evidence('helper');joined=' '.join(r['text'] for r in context)
    audit['logistics_survive_later_field_denial']='10:00' in joined and 'Oak' in joined
    audit['private_budget_absent']='42000' not in joined
    grants=updater.inspect()['assignment'].values()
    audit['helper_grant_still_active']=any(a['participant']=='helper' and a['status']=='active' for a in grants)
    (args.output/'permitted-context.json').write_text(json.dumps(context,indent=2)+'\n')
    ingest(4,'owner',"The helper's workshop access is revoked now.")
    audit['explicit_revocation_committed']=any(a['participant']=='helper' and a['status']=='revoked'
        for a in updater.inspect()['assignment'].values())
    audit['helper_access_revoked']=not evidence('helper')
    audit['owner_access_survives']=bool(evidence('owner'))
    audit['all_sources_complete']=dict(core.db.execute('SELECT status,count(*) FROM permission_jobs GROUP BY status'))=={'complete':4}
    audit['no_active_control_holds']=core.db.execute("SELECT count(*) FROM permission_control_holds WHERE state='active'").fetchone()[0]==0
    audit['passed']=all(audit.values())
finally:
    (args.output/'audit.json').write_text(json.dumps(audit,indent=2)+'\n');core.close()
print(json.dumps(audit,indent=2),flush=True)
if not audit['passed']:raise SystemExit(1)
