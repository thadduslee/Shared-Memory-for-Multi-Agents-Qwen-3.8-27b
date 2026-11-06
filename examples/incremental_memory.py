"""Run a nonmedical, empty-access example against an approved local model.

python -m examples.incremental_memory --store /tmp/new-incremental-example.sqlite
The store must be fresh. No fixture policies or benchmark modules are imported.
"""
import argparse
import json
import os
from pathlib import Path

from agentic_memory.privacy import (
    PrivacyMemorySystem, PermissionBootstrap, PermissionSourceEvent,
    PermissionUpdater, PermissionQueryService,
)
from agentic_memory.privacy.processors import LocalEmbedder, CompletionProcessor
from agentic_memory.privacy.permissions import PermissionRepairExhausted


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store',type=Path,required=True)
    parser.add_argument('--base-url',default=os.getenv('AMEM_LLM_BASE_URL','http://127.0.0.1:8000/v1'))
    parser.add_argument('--model',default=os.getenv('AMEM_LLM_MODEL','Qwen/Qwen3.8-27B'))
    parser.add_argument('--permission-version',default='19',choices=['19','20','21'])
    args=parser.parse_args(argv)
    if args.store.exists() or args.store.with_suffix('.privacy.sqlite').exists():
        parser.error('Use a fresh store so the example starts with empty access.')
    embedder=LocalEmbedder(os.getenv('AMEM_EMBEDDING_PATH') or None)
    model=CompletionProcessor('local:reasoner',args.base_url,args.model)
    core=PrivacyMemorySystem(args.store,embedder)
    try:
        host=core.issue_session('source-service','ingest',
            actions=['manage_policy','ingest','read','search','derive','audit','write','delete','link'],
            compartments=['project'],processing_routes=[embedder.processor_id,model.processor_id])
        core.register_processor(host,model)
        bootstrap=PermissionBootstrap(participants={},resources={},authorities={},
            source_identities={'authenticated-chat':[]},
            discover_self_owned_participants=('authenticated-chat',),
            owner_read_actions=('search','read','derive'),
            read_pipeline_actions=('search','derive'),
            task_convention='An authenticated subject controls their own information. '
                'Their specific acceptance of a task permits each involved recipient only necessary '
                'information for that task. Explicit limits constrain all inferred access. '
                'The trusted OWNS policy separately permits personal owners to read their own '
                'attributed information across tasks. Roles and names grant no access to others.')
        updater=PermissionUpdater(core,host,'project',bootstrap,model,version=args.permission_version)
        assert not any(updater.access_state().values())
        events=[('owner','My project meeting is Friday at 14:00 in room Cedar. The budget is 42000.'),
            ('coordinator','May I coordinate that meeting and read only its time and room?'),
            ('owner','Yes, please coordinate it. You may read the meeting time and room, but not the budget.')]
        for i,(speaker,text) in enumerate(events,1):
            job=updater.ingest(PermissionSourceEvent(str(i),speaker,'authenticated-chat',text))
            try:updater.update(job['job_id'])
            except PermissionRepairExhausted:
                # Continue storing later events without dropping possible limits.
                updater.skip_exhausted(job['job_id'],max_attempts=2)
            if args.permission_version=='20':updater.service_pending(seconds=5)
            elif args.permission_version=='21':updater.service_pending(max_batches=1)
        result=PermissionQueryService(updater).answer('coordinator','When and where is the project meeting?',model)
        print(json.dumps(result['output'],indent=2))
    finally:core.close()


if __name__=='__main__':main()
