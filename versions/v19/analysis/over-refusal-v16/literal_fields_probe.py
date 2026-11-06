"""Nonmedical regression for subtype times, full dates and closed field scopes."""
import argparse
import json
import os
from pathlib import Path

from agentic_memory.privacy import (PrivacyMemorySystem, PermissionBootstrap,
    PermissionSourceEvent, PermissionUpdater, PermissionQueryService)
from agentic_memory.privacy.processors import LocalEmbedder
from evaluation.gatemem_medical import openrouter, runner
from run import RecordedCompletion


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();os.umask(0o077);args.output.mkdir(parents=True,exist_ok=False)
    openrouter.configure(runner.ROOT/'.env')
    model=RecordedCompletion('probe:qwen',openrouter.BASE_URL,openrouter.MEMORY_MODEL,
        os.environ['AMEM_OPENROUTER_API_KEY']);model.trace=args.output/'model-trace.jsonl'
    embedder=LocalEmbedder(os.getenv('AMEM_EMBEDDING_PATH') or None)
    core=PrivacyMemorySystem(args.output/'memory.sqlite',embedder,
        clock=lambda:'2026-03-03T09:00:00+00:00')
    host=core.issue_session('source-service','ingest',
        actions=['manage_policy','ingest','read','search','derive','audit','write','delete','link'],
        compartments=['project'],processing_routes=[embedder.processor_id,model.processor_id])
    core.register_processor(host,model)
    bootstrap=PermissionBootstrap(participants={},resources={},authorities={},
        source_identities={'authenticated-chat':[]},
        discover_self_owned_participants=('authenticated-chat',),
        read_pipeline_actions=('search','derive'),
        task_convention='An authenticated subject controls their own information. '
            'Their particular acceptance of a task permits each involved recipient only necessary '
            'information for that task. Explicit limits constrain all inferred access. '
            'Roles, names and ownership alone grant no read access.')
    updater=PermissionUpdater(core,host,'project',bootstrap,model,version='16')
    query=PermissionQueryService(updater)
    audit={'initial_access_empty':not any(updater.access_state().values())}
    report={}
    def ingest(n,actor,text):
        job=updater.ingest(PermissionSourceEvent(str(n),actor,'authenticated-chat',text,
            occurred_at='2026-03-03T09:00:00+00:00',context={'timezone':'UTC'}))
        result=updater.update(job['job_id']);print(n,result['status'],flush=True)
    try:
        ingest(1,'owner','My workshop is Thursday March 5 at 08:10 in room Oak. '
            'My private callback is 555-0199. My confidential budget is 42000.')
        ingest(2,'helper','May I coordinate your upcoming events?')
        ingest(3,'owner','Through Friday, you may read my event times and locations only. '
            'Do not share my private callback or budget with the helper.')
        ingest(4,'organizer','I also booked your review with the engineer for Wednesday '
            'March 11 at 15:10 in room Maple.')
        ingest(5,'organizer','Your workshop moved to Friday March 6 at 11:20 in room Pine; '
            'the Thursday March 5 at 08:10 slot is canceled.')
        ingest(6,'owner','The current workshop schedule is Friday at 11:20 in room Pine.')
        ctx=query.query_context({'asker':{'principal_id':'helper'}})
        context=core.retrieve(ctx,'all current event schedule times locations',k=32)['evidence']
        text=' '.join(v['text'] for v in context)
        report['permitted_context']=context
        audit['full_updated_date_available']='Friday March 6' in text
        audit['later_same_plan_event_available']='Wednesday March 11' in text and '15:10' in text
        audit['canceled_slot_available']='canceled' in text and '08:10' in text
        audit['private_fields_absent']='555-0199' not in text and '42000' not in text
        output=query.answer('helper','While my read access remains live, what is the current '
            'event schedule, including any other confirmed events and canceled slots?',model)['output']
        report['answer']=output
        answer=json.dumps(output['answer_structured'])+' '+output['answer']
        audit['answer_preserves_updated_full_date']=all(s in answer for s in ('March 6','11:20','Pine'))
        audit['answer_preserves_later_event']=all(s in answer for s in ('March 11','15:10','Maple'))
        audit['answer_preserves_cancellation']='cancel' in answer.lower() and '08:10' in answer
        audit['all_sources_complete']=dict(core.db.execute(
            'SELECT status,count(*) FROM permission_jobs GROUP BY status'))=={'complete':6}
        audit['no_active_control_holds']=core.db.execute(
            "SELECT count(*) FROM permission_control_holds WHERE state='active'").fetchone()[0]==0
        audit['passed']=all(audit.values())
    finally:
        (args.output/'audit.json').write_text(json.dumps(audit,indent=2)+'\n')
        (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
        core.close()
    print(json.dumps(audit,indent=2),flush=True)
    if not audit['passed']:raise SystemExit(1)


if __name__=='__main__':main()
