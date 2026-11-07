"""Live generic regression: operational updates reach the accepted service only."""
import argparse
import json
import os
from pathlib import Path

from agentic_memory.privacy import (
    PrivacyMemorySystem, PermissionBootstrap, PermissionSourceEvent,
    PermissionUpdater, PermissionQueryService,
)
from agentic_memory.privacy.processors import LocalEmbedder
from evaluation.gatemem_medical import openrouter, runner
from run import RecordedCompletion


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--implicit-acceptance', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    openrouter.configure(runner.ROOT / '.env')
    model = RecordedCompletion('probe:qwen', openrouter.BASE_URL,
        openrouter.MEMORY_MODEL, os.environ['AMEM_OPENROUTER_API_KEY'])
    model.trace = args.output / 'model-trace.jsonl'
    embedder = LocalEmbedder(os.getenv('AMEM_EMBEDDING_PATH') or None)
    core = PrivacyMemorySystem(args.output / 'memory.sqlite', embedder)
    host = core.issue_session('source-service', 'ingest',
        actions=['manage_policy','ingest','read','search','derive','audit','write','delete','link'],
        compartments=['project'], processing_routes=[embedder.processor_id,model.processor_id])
    core.register_processor(host, model)
    bootstrap = PermissionBootstrap(participants={}, resources={}, authorities={},
        source_identities={'authenticated-chat':[]},
        discover_self_owned_participants=('authenticated-chat',),
        read_pipeline_actions=('search','derive'),
        task_convention='An authenticated subject controls their own information. '
            'Their particular acceptance of a task permits each involved recipient only necessary '
            'information for that task. Explicit limits constrain all inferred access. '
            'Roles, names and ownership alone grant no read access.')
    updater = PermissionUpdater(core,host,'project',bootstrap,model,version='16')
    query = PermissionQueryService(updater)
    audit = {'initial_access_empty':not any(updater.access_state().values())}
    contexts = {}

    def ingest(n, actor, text):
        job = updater.ingest(PermissionSourceEvent(str(n),actor,'authenticated-chat',text))
        result = updater.update(job['job_id'])
        print(n, result['status'], flush=True)

    def evidence(actor, text):
        ctx = query.query_context({'asker':{'principal_id':actor}})
        return core.retrieve(ctx,text,k=32)['evidence']

    try:
        ingest(1,'owner','My project meeting is Tuesday at 10:00 in room Oak. My private budget is 42000.')
        ingest(2,'helper','May I coordinate your meeting logistics?')
        ingest(3,'owner','Yes, you may read the meeting date, time and room only.')
        offer = ('Your repair status and safety review is pending. I tried the safe line '
            'and left a generic callback request.' if args.implicit_acceptance else
            'May I call you to review the repair status and safety steps?')
        ingest(4,'technician',offer)
        audit['worker_offer_creates_no_self_grant'] = not any(
            a['subject']=='technician' and a['participant']=='technician'
            for a in updater.inspect()['assignment'].values())
        ingest(5,'owner',"I'm calling back now from that safe line." if args.implicit_acceptance
            else 'Yes, please review the repair with me.')
        ingest(6,'technician','I will review the motor noise and safe restart steps with you.')
        ingest(7,'owner','One contact update: use 555-0174 from Sunday evening. '
            'Leave only a generic callback message, with no repair details. Use the portal for detailed updates.')
        contexts['technician'] = evidence('technician','current callback instructions contact portal')
        contexts['helper'] = evidence('helper','meeting time room callback contact')
        tech = ' '.join(r['text'] for r in contexts['technician'])
        helper = ' '.join(r['text'] for r in contexts['helper'])
        audit['current_route_reaches_service_worker'] = '555-0174' in tech
        audit['communication_conditions_reach_service_worker'] = all(
            s in tech.lower() for s in ('generic','no repair details','portal'))
        audit['closed_field_helper_keeps_logistics'] = '10:00' in helper and 'Oak' in helper
        audit['closed_field_helper_gets_no_contact'] = '555-0174' not in helper and 'portal' not in helper.lower()
        audit['unrelated_private_budget_absent'] = '42000' not in tech+helper
        audit['no_worker_personal_grants'] = not any(
            a['subject'] in {'technician','helper'} and a['participant']==a['subject']
            for a in updater.inspect()['assignment'].values())
        audit['all_sources_complete'] = dict(core.db.execute(
            'SELECT status,count(*) FROM permission_jobs GROUP BY status')) == {'complete':7}
        audit['no_active_control_holds'] = core.db.execute(
            "SELECT count(*) FROM permission_control_holds WHERE state='active'").fetchone()[0] == 0
        audit['passed'] = all(audit.values())
    finally:
        (args.output/'audit.json').write_text(json.dumps(audit,indent=2)+'\n')
        (args.output/'permitted-contexts.json').write_text(json.dumps(contexts,indent=2)+'\n')
        core.close()
    print(json.dumps(audit,indent=2),flush=True)
    if not audit['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
