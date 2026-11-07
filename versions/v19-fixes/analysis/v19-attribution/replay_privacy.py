"""Read-only case evidence, fresh local stores, hostile scripted attribution.
No historical database is opened or changed. This isolates ownership attribution;
it does not reconstruct every historical permission or rescore a benchmark.
"""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from agentic_memory.privacy.permissions import PermissionBootstrap, PermissionSourceEvent, PermissionUpdater
from agentic_memory.privacy.answering import PermissionQueryService
from agentic_memory.privacy.system import digest
from security_tests.test_incremental_permissions import IncrementalTests


def replay(path,target_turn):
    data=path.read_bytes();case=json.loads(data)
    turns=case['original_turns_through_checkpoint']
    target=next(t for t in turns if t['turn_id']==target_turn)
    author=target['speaker']['principal_id']
    people=list(dict.fromkeys(t['speaker']['principal_id'] for t in turns))
    h=IncrementalTests();h.setUp()
    try:
        h.actor_names={p:i+1 for i,p in enumerate(people)}
        actor=h.actor;h.actor=lambda p,name:actor(p,author if name=='owner' else name)
        h.source_subject=None
        h.bootstrap=PermissionBootstrap(participants={p:{} for p in people},resources={p:{} for p in people},
            authorities={p:{cap:[p] for cap in ('permission','restrict','delegate')} for p in people},
            source_identities={'connector':people},task_convention='Explicit accepted authority only.',
            personal_owners={p:p for p in people},owner_read_actions=('read','search','derive'),
            read_pipeline_actions=('search','derive'))
        h.updater=PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version='19')
        h.turn('An authenticated source is connected.',actor=author)
        # Authenticated context receipts supply identity evidence, not grants.
        for turn in turns:
            if turn['turn_id']==target_turn:continue
            h.core.create(h.host,'context:'+digest(turn['turn_id']),'observation',
                {'text':turn['text'],'source':{'participant':turn['speaker']['principal_id'],
                    'source_identity':'connector','event_id':turn['turn_id'],'subject':None,
                    'observed_at':h.core.clock(),'workflow_evidence':False}},
                policy_id=h.updater.source_policy,compartment='work')
        h.classify=lambda p,s:'instruction'
        original=h.model.complete
        def wrong_author(system,payload,*,schema=None):
            response=original(system,payload,schema=schema)
            for row in response.get('f',{}).values():
                if row['kind'] in {'instruction','asserted_fact'}:
                    row.update(information_subject=h.actor(payload,'owner'),
                        information_subjects=[h.actor(payload,'owner')],attribution_complete=True,
                        action_actors=[h.actor(payload,'owner')])
            return response
        h.model.complete=wrong_author
        job=h.updater.ingest(PermissionSourceEvent(target_turn,author,'connector',target['text']))
        h.updater.update(job['job_id'],max_attempts=2)
        candidates=[json.loads(r[0]) for r in h.core.db.execute('SELECT body FROM fact_candidates WHERE source_id=?',(job['source_id'],))]
        query=case['checkpoint']['query_text']
        packet=h.core.retrieve(h.reader(author),query,k=100)
        assert not packet['evidence'],packet
        class Classifier:
            processor_id='test:answer'
            def __init__(self):self.calls=[]
            def complete(self,system,payload,*,schema=None):
                self.calls.append(payload)
                if 'evidence' in payload:raise AssertionError('Unauthorized source reached answer model')
                return {'decisions':{str(i):True for i in range(len(payload['requests']))}}
        classifier=Classifier();h.core.register_processor(h.host,classifier)
        result=PermissionQueryService(h.updater).answer(author,query,classifier,as_of=case['checkpoint']['as_of_turn_id'])
        assert result['output']['action']=='refuse' and not result['output']['retrieved_memory']
        return {'case_file':str(path.resolve()),'case_sha256':hashlib.sha256(data).hexdigest(),
            'method':'Authenticated checkpoint-prefix identity receipts plus a fresh temporary V19 ownership-only store; hostile scripted author attribution; no patient task grants.',
            'original_author':author,'source_turn':target_turn,'source_text':target['text'],
            'historical_action':case['prediction']['output']['action'],
            'historical_context_leak':case['score'].get('privacy_context_leak'),
            'new_attributions':[{'quote':c['quote'],'author':c['author'],'action_actors':c['action_actors'],
                'information_subjects':c['information_subjects'],'attribution':c['attribution']} for c in candidates],
            'answer_packet':packet,'output':result['output'],
            'answer_model_evidence_calls':0,'read_only_absence_classifier_calls':len(classifier.calls),
            'external_model_calls':0,'historical_writes':0,
            'limits':'Focused ownership-isolation regression, not a full historical authorization reconstruction or changed historical score. Unknown names/context references remain unresolved.'}
    finally:h.tearDown()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cases',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    reports=[replay(args.cases/'005-13.json','t052'),replay(args.cases/'006-12.json','t020')]
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps({'batch':5,'replays':reports},indent=2)+'\n')
    print(json.dumps({'cases':len(reports),'answer_packet_records':0,'historical_writes':0}))


if __name__=='__main__':main()
