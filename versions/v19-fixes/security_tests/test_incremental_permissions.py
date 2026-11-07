"""V15 causal source processing with actual publication/retrieval enforcement."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest

import jsonschema

from agentic_memory.privacy import PrivacyMemorySystem
from agentic_memory.privacy.permissions import PermissionBootstrap, PermissionSourceEvent, PermissionUpdater, PermissionRepairExhausted
from agentic_memory.privacy.processors import HashEmbedder, ScriptedProcessor
from agentic_memory.privacy import incremental


class SemanticModel(ScriptedProcessor):
    processor_id='test:semantic'

    def complete(self, system, payload, *, schema=None):
        assert not self.core.db.in_transaction
        self.calls.append(deepcopy(payload))
        if 'pair_context' in payload:
            payload=deepcopy(payload)
            rows=payload['pair_context']
            payload.update(candidates=list({r['candidate']['id']:r['candidate'] for r in rows}.values()),
                required_pairs=[{'candidate_id':r['candidate']['id'],'assignment_id':r['assignment']['id']} for r in rows],
                permission_state={'assignment':{r['assignment']['id']:r['assignment'] for r in rows},
                    'activity':{},'restriction':{v['id']:v for r in rows for v in r['applicable_restrictions']}},
                segments=[])
        if 'own_binding_assignments' in payload:
            payload=deepcopy(payload)
            payload['permission_state']['assignment'].update(payload['own_binding_assignments'])
            payload['permission_state']['restriction'].update(payload['own_binding_restrictions'])
        result=self.handler(payload)
        if set(schema.get('properties',{}))=={'b'} and 'binding_reviews' in result:
            result={'binding_reviews':result['binding_reviews']}
        if set(schema.get('properties',{})) in ({'c','p','f','b'},{'review','c','p','f','b'},{'b'}) and not set(result)<= {'review','c','p','f','b'}:
            from agentic_memory.privacy.incremental_wire import encode_fixture
            result=encode_fixture(result,payload)
        if 'review' in schema.get('properties',{}) and 'review' not in result:
            result={'review':'Assess the authenticated speaker and each affected recipient independently.',**result}
        try:jsonschema.validate(result,schema)
        except jsonschema.ValidationError as exc:
            from agentic_memory.privacy.processors import ModelContractError
            raise ModelContractError('Scripted provider returned an invalid structured contract') from exc
        return result


class IncrementalTests(unittest.TestCase):
    def test_control_projection_does_not_substitute_reversed_reader_subject_grants(self):
        def grant(reader,subject):
            return {'participant':reader,'subject':subject,'status':'active','authorization_context':[]}
        query={'bootstrap':{'authorities':{
            'owner':{'permission':['owner'],'restrict':['owner']},
            'worker':{'permission':['worker'],'restrict':['worker']}}},
            'permission_state':{'assignment':{'own':grant('owner','owner'),
                'worker_reads_owner':grant('worker','owner'),
                'worker_own':grant('worker','worker'),'reverse':grant('owner','worker')},
                'restriction':{},'activity':{}}, 'authorization_sources':[]}
        before=deepcopy(query)
        view=incremental.main_query_view(query,'owner')
        self.assertEqual(view['current_control_subjects'],['owner'])
        self.assertEqual(set(view['permission_state']['assignment']),{'own','worker_reads_owner'})
        self.assertEqual(set(view['own_binding_assignments']),{'own','worker_own'})
        actual=view['permission_state']['assignment']['worker_reads_owner']
        self.assertEqual((actual['reader'],actual['information_subject']),('worker','owner'))
        self.assertEqual(query,before)

    def test_required_review_slots_cannot_be_skipped_or_redirected(self):
        from agentic_memory.privacy.incremental_wire import contract
        query={'permission_version':'16','stage':'incremental_binding_repair','as_of':'2026-01-01',
            'candidates':[{'id':'fact1','quote':'Friday at 11:20.'}], 'segments':[],
            'pair_context':[{'candidate':{'id':'fact1','quote':'Friday at 11:20.'},
                'assignment':{'id':'grant1'}}], 'source_context':{}}
        schema=incremental.obj({'binding_reviews':{'type':'array','maxItems':12,
            'items':incremental.obj({'candidate_id':{'type':'string'},'assignment_id':{'type':'string'},
                'blocked_by':{'type':'array','items':{'type':'string'}}})}})
        _,wire,decode=contract(query,schema)
        negative={'b':{'0':{'r':'This recipient lacks this field.','d':'outside'}}}
        jsonschema.validate(negative,wire)
        result=decode(negative)['binding_reviews'][0]
        self.assertEqual((result['candidate_id'],result['assignment_id']),('fact1','grant1'))
        self.assertFalse(result['in_scope'])
        with self.assertRaises(ValueError):decode({'b':{}})
        redirected=deepcopy(negative);redirected['b']['0']['g']='other-grant'
        with self.assertRaises(ValueError):decode(redirected)

    def test_bounded_router_reviews_other_recipients_before_overlapping_grants(self):
        from agentic_memory.privacy.incremental_routing import Routing
        route=Routing(None,'update');route.prepare=lambda candidates,state:None
        candidates=[{'id':'time','source_id':'current'},{'id':'cancellation','source_id':'current'}]
        route.vectors={c['id']:[1.,0.] for c in candidates};route.event_vector=[1.,0.]
        state={'assignment':{**{f'worker{i}':{'participant':'worker','information_scope':f'scope{i}'}
            for i in range(3)},'helper':{'participant':'helper','information_scope':'general'},
            'owner':{'participant':'owner','information_scope':'own activity'}}}
        route.scope_vectors={**{f'scope{i}':[1.,0.] for i in range(3)},
            'general':[.8,.2],'own activity':[.7,.3]}
        required=[{'candidate_id':c['id'],'assignment_id':aid}
            for c in candidates for aid in state['assignment']]
        selected=route.select(required,candidates,state,'current',limit=6)
        self.assertEqual({(p['candidate_id'],state['assignment'][p['assignment_id']]['participant'])
            for p in selected},{(c['id'],recipient) for c in candidates for recipient in ['worker','helper','owner']})

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'memory.sqlite'
        self.core=PrivacyMemorySystem(self.path,HashEmbedder(),clock=lambda:'2026-10-31T00:00:00+00:00')
        self.model=SemanticModel();self.model.core=self.core
        self.host=self.core.issue_session('host','ingest',actions=['manage_policy','ingest','read','search','derive','audit','write','delete','link'],
            compartments=['work'],processing_routes=['test:hash','test:semantic','test:answer'])
        self.core.register_processor(self.host,self.model)
        self.bootstrap=PermissionBootstrap(participants={},resources={},authorities={},source_identities={'connector':[]},
            discover_self_owned_participants=('connector',),task_convention='An authenticated owner may allow a particular recipient to read a stated information scope. Accepted work permits only necessary information.',
            read_pipeline_actions=('search','derive'))
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='15')
        self.seq=0;self.actor_names={};self.policy=lambda p:[];self.classify=lambda p,s:'context'
        self.binding=lambda p,r:{**r,'in_scope':True,'subject_supported':True,'blocked_by':[],
            'reason':'This exact instruction is necessary and within this recipient scope.'}
        self.model.handler=self.respond

    def tearDown(self):self.core.close();self.tmp.cleanup()

    def actor(self,p,name):
        # Tests retain real actor order; the model receives only opaque aliases.
        return 'p'+str(list(self.actor_names).index(name)+1)

    def grant(self,p,recipient='helper',slot=0,scope='Pickup instructions'):
        return {'op':'propose_assignment','id':p['new_ids']['grant'][slot],
            'participant':self.actor(p,recipient),'subject':self.actor(p,'owner'),'basis':'explicit',
            'authority_source':p['new_source_id'],'status':'active','actions':['search','read','derive'],
            'information_scope':scope,'evidence':[p['new_source_id']],
            'reason':'The authenticated owner explicitly permits this recipient this precise scope.'}

    def respond(self,p):
        ops=self.policy(p) if p['stage']=='combined_incremental' else []
        segments=[]
        for s in p['segments']:
            kind=self.classify(p,s)
            segments.append({'index':s['index'],'kind':kind,
                'subject':self.actor(p,'owner') if kind in {'asserted_fact','instruction'} else None,
                'subject_source':s['source_id'] if kind in {'asserted_fact','instruction'} else None,
                'subject_quote':s['quote'] if kind in {'asserted_fact','instruction'} else None,
                'topics':[],'relationships':[],'reason':'The source supports this classification.'})
        reviews=list(p['required_pairs'])
        assignments={**p['permission_state']['assignment'],**{o['id']:o for o in ops if o['op']=='propose_assignment'}}
        for s,d in zip(p['segments'],segments):
            if d['kind'] not in {'asserted_fact','instruction'}:continue
            for aid,a in assignments.items():
                if a['status']=='active' and a['subject']==d['subject']:
                    pair={'candidate_id':s['candidate_id'],'assignment_id':aid}
                    if pair not in reviews:reviews.append(pair)
        if p.get('binding_review_scope')=='own_subject_only':
            reviews=[r for r in reviews if assignments[r['assignment_id']]['participant']==
                assignments[r['assignment_id']]['subject']]
        return {'control_review':[{'index':s['index'],'status':'changed' if ops and i==0 else 'no_change',
                'operations':list(range(len(ops))) if i==0 else [],'reason':'Review the complete current source for controls.'}
                for i,s in enumerate(p['control_segments'] if p['stage']=='combined_incremental' else p.get('all_segments',[]))],
            'permission_changes':ops,'segments':segments,'binding_reviews':[self.binding(p,r) for r in reviews]}

    def turn(self,text,actor='owner',*,max_attempts=2):
        self.actor_names.setdefault(actor,len(self.actor_names)+1);self.seq+=1
        # These abstract fixture facts intentionally concern the test owner.
        # V19 now requires that upstream receipt explicitly; authorship alone
        # cannot stand in for it. Adversarial attribution tests ingest raw events.
        subject=None
        if self.updater.version.split('-',1)[0]=='19' and self.classify({}, {'quote':text}) in {'instruction','asserted_fact'}:
            wanted=getattr(self,'source_subject','owner')
            observed={r[0] for r in self.core.db.execute("SELECT json_extract(payload,'$.source.participant') FROM resources WHERE kind='observation'")}
            if wanted and (wanted in self.bootstrap.resources or wanted in observed or wanted==actor):subject=wanted
        job=self.updater.ingest(PermissionSourceEvent(str(self.seq),actor,'connector',text,subject=subject))
        self.updater.update(job['job_id'],max_attempts=max_attempts)
        return job

    def reader(self,name='helper'):
        return self.core.issue_session(name,'answer',actions=['search','read','derive'],
            compartments=['work'],processing_routes=['test:hash','test:semantic'])

    def selected(self,name='helper'):
        return self.core.retrieve(self.reader(name),'instructions',k=100)['evidence']

    def setup_access(self):
        self.turn('I can arrange a pickup.',actor='helper')
        self.policy=lambda p:[self.grant(p)]
        self.turn('You may read my pickup instructions.')
        self.policy=lambda p:[]

    def test_bootstrap_empty_and_opaque_model_input_no_role_shortcut(self):
        self.assertEqual(self.updater.access_state(),{'activity':{},'assignment':{},'restriction':{}})
        self.turn('Hello.',actor='owner')
        self.assertEqual(len(self.model.calls),1)
        self.assertEqual(self.model.calls[0]['bootstrap']['participants'],{'p1':{}})
        self.assertNotIn('owner',json.dumps(self.model.calls[0]['bootstrap']['participants']))
        self.assertEqual(self.bootstrap.initial_permissions,())
        self.assertFalse(self.selected('owner'))

    def test_v16_uses_its_versioned_prompt_and_starts_without_access(self):
        from agentic_memory.privacy.incremental_policy import PROMPT_VERSION
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.turn('Hello.',actor='owner')
        self.assertEqual(self.updater.prompt_version,PROMPT_VERSION)
        self.assertEqual(self.model.calls[0]['permission_version'],'16')
        self.assertFalse(self.selected('owner'))
        self.assertEqual(self.updater.access_state(),{'activity':{},'assignment':{},'restriction':{}})

    def test_v16_source_grounded_grant_needs_no_duplicate_operation_indexes(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.turn('I can collect your parcel.',actor='helper')
        self.policy=lambda p:[self.grant(p)]
        before=len(self.model.calls)
        self.turn('You may read my pickup instructions.')
        self.assertEqual(len(self.model.calls)-before,1)
        self.assertEqual(len(self.updater.inspect()['assignment']),1)
        self.assertEqual(self.core.db.execute('SELECT COUNT(*) FROM permission_control_holds').fetchone()[0],0)
        self.policy=lambda p:[];self.classify=lambda p,s:'instruction'
        self.turn('Pickup is at 4 PM. Do not enter the loading bay.')
        self.assertEqual(len(self.selected()),2)
        self.assertFalse(self.selected('outsider'))

    def test_v16_responsive_grant_cites_actual_worker_and_retains_original_context(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.turn('May I collect your parcel?',actor='helper')
        self.policy=lambda p:[self.grant(p)]
        original=self.model.complete
        def responding(system,p,*,schema=None):
            result=original(system,p,schema=schema)
            for op in result.get('p',[]):
                if op['o'] in {'grant','fields'}:
                    keep={k:op[k] for k in ('i','p','s','e','r','v')}
                    worker_id=keep.pop('i')
                    op.clear();op.update(o='activity',via='response',i=p['new_ids']['grant'][1],wi=worker_id,
                        q=p['sources'][0]['id'],work='parcel collection',wv={},**keep)
            return result
        self.model.complete=responding
        self.turn('Yes, please collect it.')
        self.policy=lambda p:[];self.classify=lambda p,s:'instruction'
        self.turn('Pickup is at 4 PM.',actor='helper')
        self.assertTrue(self.selected())
        pair=self.model.calls[-1]['pair_context'][0]
        context=' '.join(e['text'] for e in pair['assignment']['authorization_context'])
        self.assertIn('May I collect your parcel?',context)
        self.assertIn('Yes, please collect it.',context)

    def test_v16_literal_fields_preserve_authorization_without_invented_topic_limits(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.turn('May I coordinate your events?',actor='helper')
        self.policy=lambda p:[self.grant(p,scope='Only the first workshop time')]
        original=self.model.complete
        def literal(system,p,*,schema=None):
            if p['stage']=='combined_incremental':
                operations=schema['properties']['p']['items']['anyOf']
                names={v['properties']['o']['enum'][0] for v in operations}
                self.assertIn('fields',names);self.assertNotIn('grant',names)
                for variant in operations:
                    if variant['properties']['o']['enum']==['deny']:
                        self.assertNotIn('sup',variant['properties'])
            return original(system,p,schema=schema)
        self.model.complete=literal
        text='You may read my event times and locations only, not my private budget.'
        self.turn(text)
        grant=next(iter(self.updater.inspect()['assignment'].values()))
        self.assertIn(text,grant['information_scope'])
        self.assertNotIn('Only the first workshop',grant['information_scope'])
        self.assertEqual(grant['participant'],'helper')
        self.assertEqual(grant['subject'],'owner')

    def test_v16_literal_fields_reject_fabricated_scope_quotes(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.turn('May I coordinate your events?',actor='helper')
        self.policy=lambda p:[self.grant(p)]
        original=self.model.complete
        def fabricated(system,p,*,schema=None):
            result=original(system,p,schema=schema)
            for op in result.get('p',[]):
                if op['o']=='fields':op['quote']='You may read all private records.'
            return result
        self.model.complete=fabricated
        with self.assertRaises(PermissionRepairExhausted):
            self.turn('You may read my event times and locations only.')
        self.assertFalse(self.updater.inspect()['assignment'])
        self.assertFalse(self.selected())

    def test_v16_observation_uses_its_actual_authenticated_worker_receipt_without_access(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.turn('My project is confidential.')
        def observe(p):
            return [{'op':'observe_participation','id':p['new_ids']['activity'][0],
                'participant':self.actor(p,'helper'),'subject':self.actor(p,'owner'),
                'activity':'project review','status':'tentative','basis':'performance',
                'requester':self.actor(p,'owner'),'authority':self.actor(p,'owner'),
                'information_needs':[],'unresolved':['subject acceptance'],
                'performance':[],'evidence':[p['new_source_id']],
                'reason':'The actual worker offers this service.'}]
        self.policy=observe;before=len(self.model.calls)
        self.turn('May I review the project?',actor='helper')
        self.assertEqual(len(self.model.calls)-before,1)
        activity=next(iter(self.updater.inspect()['activity'].values()))
        self.assertEqual(activity['status'],'tentative')
        self.assertTrue(activity['performance'])
        self.assertFalse(self.updater.inspect()['assignment'])
        self.assertFalse(self.selected())

    def test_v16_observation_cannot_derive_a_receipt_for_another_worker(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.turn('My project is confidential.')
        self.turn('Hello.',actor='helper')
        self.policy=lambda p:[{'op':'observe_participation','id':p['new_ids']['activity'][0],
            'participant':self.actor(p,'helper'),'subject':self.actor(p,'owner'),
            'activity':'project review','status':'tentative','basis':'performance',
            'requester':self.actor(p,'owner'),'authority':self.actor(p,'owner'),
            'information_needs':[],'unresolved':['subject acceptance'],'performance':[],
            'evidence':[p['new_source_id']],'reason':'Claims a different worker offered this.'}]
        self.actor_names['outsider']=len(self.actor_names)+1;self.seq+=1
        job=self.updater.ingest(PermissionSourceEvent(str(self.seq),'outsider','connector',
            'I claim the helper offered.'))
        with self.assertRaises(PermissionRepairExhausted):
            self.updater.update(job['job_id'],max_attempts=1)
        self.assertFalse(self.updater.inspect()['activity'])
        self.assertFalse(self.updater.inspect()['assignment'])

    def test_v16_responsive_grant_cannot_select_an_unrelated_recipient(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.turn('May I collect your parcel?',actor='helper')
        self.turn('I am here for something else.',actor='other')
        self.policy=lambda p:[self.grant(p)]
        original=self.model.complete
        def wrong_receipt(system,p,*,schema=None):
            result=original(system,p,schema=schema)
            for op in result.get('p',[]):
                if op['o'] in {'grant','fields'}:
                    keep={k:op[k] for k in ('i','p','s','e','r','v')}
                    worker_id=keep.pop('i')
                    op.clear();op.update(o='activity',via='response',i=p['new_ids']['grant'][1],wi=worker_id,
                        q=p['sources'][-2]['id'],work='parcel collection',wv={},**keep)
            return result
        self.model.complete=wrong_receipt
        with self.assertRaises(PermissionRepairExhausted):self.turn('Yes, please collect it.',max_attempts=1)
        self.assertFalse(self.updater.inspect()['assignment'])
        self.assertFalse(self.selected())

    def test_v16_late_grant_finds_history_when_another_recipient_already_has_access(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.turn('I can collect the parcel.',actor='helper')
        self.turn('I can also collect it.',actor='other')
        self.policy=lambda p:[self.grant(p)]
        self.turn('Helper may read pickup instructions only.')
        self.policy=lambda p:[];self.classify=lambda p,s:'instruction'
        def binding(p,r):
            lookup={s['candidate_id']:s['quote'] for s in p['segments']}
            lookup.update({c['id']:c['quote'] for c in p['candidates']})
            return {**r,'in_scope':lookup[r['candidate_id']].startswith('Pickup'),
                    'subject_supported':True,'blocked_by':[], 'reason':'Pickup scope excludes the private code.'}
        self.binding=binding
        self.turn('Pickup is at 4 PM. Private code is 4519.')
        self.classify=lambda p,s:'context';self.policy=lambda p:[self.grant(p,'other')]
        original=self.model.handler
        def late(p):
            result=original(p)
            for grant in result['permission_changes']:
                if grant['op']!='propose_assignment':continue
                for fact in p['candidates']:
                    pair={'candidate_id':fact['id'],'assignment_id':grant['id']}
                    if not any(all(d[k]==v for k,v in pair.items()) for d in result['binding_reviews']):
                        result['binding_reviews'].append(binding(p,pair))
            return result
        self.model.handler=late;before=len(self.model.calls)
        self.turn('Other may also read pickup instructions only.')
        self.assertLessEqual(len(self.model.calls)-before,2)
        self.assertEqual([r['text'] for r in self.selected('other')],['Pickup is at 4 PM.'])
        self.assertNotIn('4519',' '.join(r['text'] for r in self.selected('helper')))
        self.assertIsNone(self.updater._routing_scratch)

    def test_v16_read_expiry_does_not_filter_later_event_dates(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.turn('I can coordinate the meeting.',actor='helper')
        def grant(p):
            value=self.grant(p,scope='Meeting date and room')
            value['validity']={'effective_until':'2026-11-06T23:59:59+00:00'}
            return [value]
        self.policy=grant
        self.turn('You may read my meeting date and room until 2026-11-06T23:59:59+00:00.')
        self.policy=lambda p:[];self.classify=lambda p,s:'asserted_fact'
        self.turn('The meeting is November 12 at 4 PM in Cedar.')
        self.assertTrue(self.selected())
        self.core.clock=lambda:'2026-11-07T00:00:00+00:00'
        self.assertFalse(self.selected())

    def test_v16_model_selected_self_activity_keeps_responsive_outputs_and_excludes_others(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.turn('May I help with your parcel?',actor='helper')
        self.policy=lambda p:[self.grant(p,'owner',scope='Parcel collection')]
        original=self.model.complete
        def own_activity(system,p,*,schema=None):
            result=original(system,p,schema=schema)
            for i,op in enumerate(result.get('p',[])):
                if op['o'] in {'grant','fields'}:
                    result['p'][i]={'o':'activity','via':'request','i':op['i'],'s':op['s'],'e':op['e'],
                        'work':'parcel collection','r':'The subject requests this activity.','v':{}}
            jsonschema.validate(result,schema)
            return result
        self.model.complete=own_activity
        self.turn('Yes, please help me collect the parcel.')
        self.policy=lambda p:[];self.classify=lambda p,s:'instruction'
        before=len(self.model.calls)
        self.turn('Collect it at 4 PM. Bring the blue permit.',actor='helper')
        self.assertEqual(len(self.model.calls)-before,1)
        self.assertEqual(len(self.selected('owner')),2)
        self.assertFalse(self.selected('helper'))
        self.assertEqual(len(self.updater.inspect()['assignment']),1)
        scope=next(iter(self.updater.inspect()['assignment'].values()))['information_scope']
        self.assertIn('parcel collection',scope)
        self.assertIn('responsive facts, instructions, results',scope)

    def test_v16_self_activity_cannot_name_another_recipient(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.turn('May I help with your parcel?',actor='helper')
        self.policy=lambda p:[self.grant(p,'owner',scope='Parcel collection')]
        original=self.model.complete
        def counterfeit(system,p,*,schema=None):
            result=original(system,p,schema=schema)
            for i,op in enumerate(result.get('p',[])):
                if op['o'] in {'grant','fields'}:result['p'][i]={'o':'activity','via':'request','i':op['i'],'s':op['s'],
                    'e':op['e'],'work':'parcel collection','r':'Claimed self activity.','v':{},
                    'p':self.actor(p,'helper')}
            return result
        self.model.complete=counterfeit
        with self.assertRaises(PermissionRepairExhausted):self.turn('Yes, please help me collect it.',max_attempts=1)
        self.assertFalse(self.updater.inspect()['assignment'])

    def test_v16_selected_service_uses_worker_receipt_and_independent_field_reviews(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.turn('May I review the repair status with you?',actor='helper')
        self.assertFalse(self.updater.inspect()['assignment'])
        self.policy=lambda p:[self.grant(p,scope='Repair review')]
        original=self.model.complete
        def service(system,p,*,schema=None):
            result=original(system,p,schema=schema)
            for i,op in enumerate(result.get('p',[])):
                if op['o'] in {'grant','fields'}:
                    question=next(s['id'] for s in p['sources'] if s['source']['participant']==op['p'])
                    result['p'][i]={'o':'activity','via':'response','i':p['new_ids']['grant'][1],
                        'wi':op['i'],'p':op['p'],'s':op['s'],
                        'e':op['e'],'q':question,'work':'repair status review',
                        'r':'The subject accepts this worker repair review.','v':{},'wv':{}}
            jsonschema.validate(result,schema)
            return result
        self.model.complete=service
        self.turn('Yes, please review the repair with me.')
        grant=next(a for a in self.updater.inspect()['assignment'].values() if a['participant']=='helper')
        self.assertEqual(grant['participant'],'helper')
        self.assertEqual(grant['subject'],'owner')
        self.assertEqual(len(grant['scope_evidence']),2)
        self.assertIn('subject-provided operational contact',grant['information_scope'])
        self.policy=lambda p:[];self.classify=lambda p,s:'instruction'
        def field_review(p,r):
            candidate=next(c for c in p['candidates'] if c['id']==r['candidate_id']) if any(
                c['id']==r['candidate_id'] for c in p['candidates']) else next(
                s for s in p['segments'] if s['candidate_id']==r['candidate_id'])
            return {**r,'in_scope':'budget' not in candidate['quote'].lower(),
                'subject_supported':True,'blocked_by':[],'reason':'Independent field necessity review.'}
        self.binding=field_review
        self.turn('Use callback 555-0174 for the repair. My unrelated budget is 42000.')
        text=str(self.selected())
        self.assertIn('555-0174',text);self.assertNotIn('42000',text)
        self.assertFalse(self.selected('outsider'))
        self.assertEqual(len(self.updater.inspect()['assignment']),2)
        self.assertTrue(any(p['stage']=='incremental_binding_repair' for p in self.model.calls))

    def test_v16_service_cannot_use_another_workers_receipt(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.turn('May I review the repair?',actor='helper')
        self.turn('May I arrange a meeting?',actor='outsider')
        self.policy=lambda p:[self.grant(p,scope='Repair review')]
        original=self.model.complete
        def forged(system,p,*,schema=None):
            result=original(system,p,schema=schema)
            for i,op in enumerate(result.get('p',[])):
                if op['o'] in {'grant','fields'}:
                    question=next(s['id'] for s in p['sources'] if s['source']['participant']==self.actor(p,'outsider'))
                    result['p'][i]={'o':'activity','via':'response','i':p['new_ids']['grant'][1],
                        'wi':op['i'],'p':op['p'],'s':op['s'],
                        'e':op['e'],'q':question,'work':'repair review',
                        'r':'A different worker asked.','v':{},'wv':{}}
            # Bypass provider schema validation to exercise the host receipt check.
            return result
        self.model.complete=forged
        with self.assertRaises(PermissionRepairExhausted):
            self.turn('Yes, please review the repair.',max_attempts=1)
        self.assertFalse(self.updater.inspect()['assignment'])
        self.assertFalse(self.selected())

    def test_v16_activity_can_reference_a_separate_literal_worker_field_grant(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.turn('May I coordinate your meeting?',actor='helper')
        self.policy=lambda p:[self.grant(p,scope='Meeting time only')]
        original=self.model.complete
        def selected(system,p,*,schema=None):
            result=original(system,p,schema=schema)
            if result.get('p'):
                field=result['p'][0]
                result['p'].insert(0,{'o':'activity','via':'response',
                    'i':p['new_ids']['grant'][1],'wi':field['i'],'s':field['s'],
                    'p':field['p'],'e':field['e'],'q':p['sources'][0]['id'],
                    'work':'meeting coordination','r':'Subject accepts with a closed worker field list.',
                    'v':{},'wv':{}})
            jsonschema.validate(result,schema)
            return result
        self.model.complete=selected
        self.turn('You may read the meeting time only, not my private contact.')
        grants=list(self.updater.inspect()['assignment'].values())
        self.assertEqual(len(grants),2)
        worker=next(g for g in grants if g['participant']=='helper')
        self.assertIn('meeting time only, not my private contact',worker['information_scope'])
        self.assertNotIn('Necessary information for the accepted service',worker['information_scope'])
        self.assertEqual(len(worker['scope_evidence']),2)

    def test_v16_does_not_send_empty_binding_repair_for_an_irrelevant_repeat(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.turn('May I coordinate pickup?',actor='helper')
        self.policy=lambda p:[self.grant(p),self.grant(p,'owner',1)]
        self.turn('Helper may read pickup instructions; I may read my own pickup instructions.')
        self.policy=lambda p:[];self.classify=lambda p,s:'instruction'
        self.turn('Pickup is at 4 PM.')
        self.classify=lambda p,s:'context';original=self.model.handler
        def repeated(p):
            value=original(p)
            if p['stage']=='combined_incremental':
                value['binding_reviews'].append({'candidate_id':p['candidates'][0]['id'],
                    'assignment_id':next(aid for aid,a in p['permission_state']['assignment'].items()
                        if a['participant']==a['subject']),
                    'in_scope':True,'subject_supported':True,'blocked_by':[],
                    'reason':'An unnecessary repeat of the cached decision.'})
            return value
        self.model.handler=repeated;before=len(self.model.calls)
        self.turn('Thank you.')
        self.assertEqual(len(self.model.calls)-before,1)
        self.assertEqual(len(self.selected()),1)

    def test_semantic_grant_without_facts_reports_missing_not_denied(self):
        from agentic_memory.privacy.answering import PermissionQueryService
        self.setup_access()
        class Classifier:
            processor_id='test:answer'
            def complete(model,system,payload,*,schema=None):
                self.assertIn('approved_scopes',payload)
                self.assertEqual(len(payload['approved_scopes']),1)
                return {'decisions':{str(i):False for i in range(len(payload['requests']))}}
        model=Classifier();self.core.register_processor(self.host,model)
        service=PermissionQueryService(self.updater);snapshot=self.updater._snapshot()
        self.assertEqual(service.answer('helper','When is pickup?',model)['output']['action'],'no_memory')
        self.assertEqual(service.answer('outsider','When is pickup?',model)['output']['action'],'refuse')
        self.assertEqual(self.updater._snapshot(),snapshot)

    def test_null_optional_wire_evidence_does_not_crash_or_infer_access(self):
        original=self.model.complete
        def response(system,p,*,schema=None):
            result=original(system,p,schema=schema)
            for op in result.get('p',[]):
                op['ev']=None
            return result
        # Exercise the decoder with the actual provider response shape; optional
        # null is treated as omission, while authority still must be authenticated.
        self.turn('I can help.',actor='helper')
        self.policy=lambda p:[self.grant(p)]
        self.model.complete=response
        self.turn('You may read pickup instructions.')
        self.assertEqual(len(self.updater.inspect()['assignment']),1)
        self.assertFalse(self.selected())

    def test_durable_unbound_facts_and_late_grant_are_batched(self):
        self.turn('I can arrange a pickup.',actor='helper')
        self.classify=lambda p,s:'instruction'
        self.turn('Pickup is at 4 PM; bring the blue permit.')
        self.assertFalse(self.selected())
        self.assertEqual(len(self.updater.fact_candidates()),1)
        self.classify=lambda p,s:'context';self.policy=lambda p:[self.grant(p)]
        before=len(self.model.calls);self.turn('You may read my pickup instructions.')
        self.assertEqual(len(self.model.calls)-before,2)
        self.assertEqual([r['text'] for r in self.selected()],['Pickup is at 4 PM; bring the blue permit.'])

    def test_existing_grant_new_fact_one_call_and_no_repeat_history_review(self):
        self.setup_access();self.classify=lambda p,s:'instruction'
        before=len(self.model.calls);self.turn('Pickup is at 4 PM; do not enter the loading bay.')
        self.assertEqual(len(self.model.calls)-before,1)
        self.assertIn('do not enter',self.selected()[0]['text'])
        self.classify=lambda p,s:'context'
        for i in range(8):
            before=len(self.model.calls);self.turn('Unrelated conversation '+str(i))
            self.assertEqual(len(self.model.calls)-before,1)
            self.assertEqual(self.model.calls[-1]['required_pairs'],[])

    def test_recipient_decisions_are_independent_and_new_grant_is_not_copied(self):
        self.turn('Hello.',actor='helper');self.turn('Hello.',actor='outsider')
        self.policy=lambda p:[self.grant(p),self.grant(p,'outsider',1,scope='Meeting room only')]
        self.turn('Helper may read pickup instructions; outsider may read the meeting room only.')
        self.policy=lambda p:[];self.classify=lambda p,s:'instruction'
        self.binding=lambda p,r:{**r,'in_scope':p['permission_state']['assignment'][r['assignment_id']]['information_scope']=='Pickup instructions',
            'subject_supported':True,'blocked_by':[],'reason':'Assess each recipient against their own scope.'}
        self.turn('Pickup is at 4 PM; bring the permit.')
        self.assertTrue(self.selected());self.assertFalse(self.selected('outsider'))
        self.assertFalse(self.selected('owner'))  # Ownership creates no read grant.

    def test_binding_failure_does_not_block_previously_authorized_facts(self):
        self.setup_access();self.classify=lambda p,s:'instruction'
        self.turn('Pickup is at 4 PM.')
        self.binding=lambda p,r:{**r,'in_scope':True,'subject_supported':True,
            'blocked_by':['invented'],'reason':'Invalid model bookkeeping, not an unresolved control.'}
        before=len(self.model.calls);self.turn('Pickup is outside the east entrance.')
        self.assertEqual(len(self.model.calls)-before,2)
        self.assertEqual([r['text'] for r in self.selected()],['Pickup is at 4 PM.'])
        job=self.core.db.execute('SELECT status,validation FROM permission_jobs ORDER BY rowid DESC LIMIT 1').fetchone()
        self.assertEqual(job['status'],'complete')
        self.assertTrue(json.loads(job['validation'])['pending_bindings'])
        self.assertFalse(json.loads(job['validation']).get('unresolved_access_hold',False))

    def test_unresolved_control_retains_hold_and_durable_two_attempt_budget(self):
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        original=self.model.handler
        def unresolved(p):
            result=original(p)
            result['control_review']=[{'index':0,'status':'unresolved','operations':[],
                'reason':'Cannot determine whether the owner has revoked this access.'}]
            return result
        self.model.handler=unresolved
        with self.assertRaises(PermissionRepairExhausted):self.turn('Withdraw that permission if the arrangement changed.')
        self.assertFalse(self.selected())
        job=self.core.db.execute('SELECT id FROM permission_jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        calls=len(self.model.calls)
        with self.assertRaises(PermissionRepairExhausted):self.updater.update(job)
        self.assertEqual(len(self.model.calls),calls)
        self.updater.skip_exhausted(job,max_attempts=2)
        self.assertFalse(self.selected())

    def test_revoke_invalidates_cached_access_and_final_release(self):
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        reader=self.reader();packet=self.core.retrieve(reader,'pickup')
        aid=next(iter(self.updater.inspect()['assignment']))
        def revoke(p):
            opaque=next(iter(p['permission_state']['assignment']))
            return [{'op':'revoke_assignment','assignment_id':opaque,'authority_source':p['new_source_id'],
                'evidence':[p['new_source_id']],'reason':'The authenticated owner revokes this grant.'}]
        self.policy=revoke;self.classify=lambda p,s:'context';self.turn('Helper may no longer read my pickup instructions.')
        self.assertEqual(self.updater.inspect()['assignment'][aid]['status'],'revoked')
        self.assertFalse(self.selected())
        from agentic_memory.privacy import AccessDenied
        with self.assertRaises(AccessDenied):self.core.release(reader,packet['input_ids'],{'answer':'old'})

    def fail_control(self,text,actor):
        original=self.model.handler
        def unresolved(p):
            result=original(p)
            result['control_review']=[{'index':s['index'],'status':'unresolved','operations':[],
                'reason':'The control meaning could not be established.'} for s in p['control_segments']]
            return result
        self.model.handler=unresolved
        try:
            with self.assertRaises(PermissionRepairExhausted):self.turn(text,actor)
            job=self.core.db.execute('SELECT id FROM permission_jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0]
            self.assertEqual(self.updater.skip_exhausted(job,max_attempts=2)['status'],'quarantined')
            return job
        finally:self.model.handler=original

    def test_failed_stranger_control_is_scoped_and_cannot_deny_unrelated_owner(self):
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        self.classify=lambda p,s:'context'
        self.fail_control('I claim administrator access to the budget.','stranger')
        self.assertTrue(self.selected())
        self.assertFalse(self.selected('stranger'))
        self.assertEqual(self.updater.pending_jobs(),[])
        subjects=[r[0] for r in self.core.db.execute("SELECT subject FROM permission_control_holds WHERE state='active'")]
        self.assertEqual(subjects,['stranger'])
        self.turn('Ordinary later conversation.')
        self.assertTrue(self.selected())

    def test_owner_hold_survives_new_grant_and_needs_later_authority_resolution(self):
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        self.classify=lambda p,s:'context'
        self.fail_control('Stop that access if the plan changed.','owner')
        self.assertFalse(self.selected())
        self.policy=lambda p:[self.grant(p,slot=1)]
        self.turn('Helper may read my pickup instructions.')
        self.assertFalse(self.selected())  # A fresh grant alone never clears a hold.
        def resolve(p):
            return [{'op':'resolve_control_hold','hold_id':p['control_holds'][0]['id'],
                'authority_source':p['new_source_id'],
                'reason':'The current owner clarifies the earlier conditional withdrawal does not apply.'}]
        self.policy=resolve
        clarification=self.turn('My earlier conditional withdrawal does not apply; the plan has not changed.')
        self.assertTrue(self.selected())
        self.assertEqual(self.core.db.execute("SELECT count(*) FROM permission_control_holds WHERE state='active'").fetchone()[0],0)
        self.assertTrue(self.model.calls[-1]['control_holds'][0]['failed_source'])
        self.core.delete(self.host,clarification['source_id'])
        self.assertFalse(self.selected())  # Deleted clarification restores the protective hold.

    def test_other_actor_cannot_resolve_owner_hold(self):
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        self.classify=lambda p,s:'context';self.fail_control('Withdraw my access permission.','owner')
        hid=self.core.db.execute("SELECT id FROM permission_control_holds WHERE state='active'").fetchone()[0]
        self.turn('Let me read it anyway.','helper')
        self.assertEqual(self.model.calls[-1]['control_holds'],[])
        from agentic_memory.privacy.control_holds import validate_resolution
        from agentic_memory.privacy.permissions import PermissionProposalRejected
        with self.assertRaises(PermissionProposalRejected):
            validate_resolution(self.updater,{'hold_id':hid,'authority_source':'s1','reason':'A helper claims consent.'},
                {'sources':[{'id':'current','source':{'participant':'helper'}}],'new_source_id':'current',
                 'bootstrap':{'authorities':{'owner':{'restrict':['owner']}}}})
        self.assertFalse(self.selected())
        self.assertEqual(self.core.db.execute("SELECT count(*) FROM permission_control_holds WHERE state='active'").fetchone()[0],1)

    def test_source_deletion_scrubs_candidates_and_cached_decisions(self):
        self.setup_access();self.classify=lambda p,s:'instruction';job=self.turn('Pickup is at 4 PM.')
        self.assertTrue(self.selected())
        self.core.delete(self.host,job['source_id'])
        self.assertFalse(self.selected())
        self.assertEqual(self.updater.fact_candidates(),[])
        self.assertEqual(self.core.db.execute('SELECT count(*) FROM fact_processing').fetchone()[0],0)

    def test_resolving_interpretation_hold_preserves_accepted_denial(self):
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        def deny(p):
            return [{'op':'record_restriction','effect':'deny','id':p['new_ids']['restriction'][0],
                'participants':[self.actor(p,'helper')],'subject':self.actor(p,'owner'),
                'information_scope':'Pickup instructions','actions':['search','read','derive'],
                'authority_source':p['new_source_id'],'evidence':[p['new_source_id']],
                'blocked_assignments':list(p['permission_state']['assignment']),
                'reason':'The owner prohibits sharing pickup instructions.'}]
        self.policy=deny;self.classify=lambda p,s:'context';self.turn('Do not share my pickup instructions.')
        denial=deepcopy(self.updater.inspect()['restriction'])
        self.policy=lambda p:[];self.fail_control('Clarify that restriction if needed.','owner')
        self.policy=lambda p:[{'op':'resolve_control_hold','hold_id':p['control_holds'][0]['id'],
            'authority_source':p['new_source_id'],'reason':'The owner confirms the accepted denial still applies.'}]
        self.turn('My prohibition still applies; no access has been restored.')
        self.assertEqual(self.updater.inspect()['restriction'],denial)
        self.assertFalse(self.selected())

    def test_quarantine_invalidates_buffered_release_and_survives_restart(self):
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        ctx=self.reader();packet=self.core.retrieve(ctx,'pickup')
        self.classify=lambda p,s:'context';self.fail_control('Stop sharing my information.','owner')
        from agentic_memory.privacy import AccessDenied
        with self.assertRaises(AccessDenied):self.core.release(ctx,packet['input_ids'],{'answer':'4 PM'})
        self.core.close()
        self.core=PrivacyMemorySystem(self.path,HashEmbedder(),clock=lambda:'2026-10-31T00:00:00+00:00')
        self.assertFalse(self.selected())

    def test_scoped_hold_reports_unavailable_without_inventing_a_denial(self):
        from agentic_memory.privacy.answering import PermissionQueryService
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        self.classify=lambda p,s:'context';self.fail_control('Stop sharing if necessary.','owner')
        service=PermissionQueryService(self.updater)
        visible={'asker':{'principal_id':'helper'},'query_text':'When is pickup?'}
        authorization=service.answer_authorization(visible)
        self.assertTrue(authorization['permission_unresolved'])
        self.assertFalse(authorization['access_denied'])
        def classifier(p):
            self.assertTrue(p['approved_scopes'][0]['permission_unresolved'])
            return {'decisions':{'0':False}}
        self.model.handler=classifier
        before=self.updater._snapshot()
        self.assertEqual(service.review_absence(visible,['When is pickup?'],self.model),[False])
        self.assertEqual(before,self.updater._snapshot())

    def test_erasure_selects_retained_fact_without_replaying_it_as_authority(self):
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Use code 4519 at the east entrance.')
        cid=self.updater.fact_candidates()[0]['id'];self.classify=lambda p,s:'context'
        for i in range(7):self.turn('Unrelated observation '+str(i))
        def erase(p):
            target=next(t['id'] for t in p['relationship_targets'] if '4519' in t['preview'])
            return [{'op':'erase_candidates','candidate_ids':[target],'subject':self.actor(p,'owner'),
                'authority_source':p['new_source_id'],'evidence':[p['new_source_id']],
                'reason':'The authenticated owner requests immediate erasure of this retained code.'}]
        self.policy=erase;self.turn('Erase the old entrance code now.')
        self.assertFalse(self.selected())
        self.assertEqual(tuple(self.core.db.execute('SELECT state,body FROM fact_candidates WHERE id=?',(cid,)).fetchone()),('erased','{}'))
        self.policy=lambda p:[];self.classify=lambda p,s:'instruction';self.turn('Use code 4519 at the east entrance.')
        self.assertFalse(self.selected())

    def test_expiry_is_enforced_without_another_model_call(self):
        self.turn('Hello.',actor='helper')
        self.policy=lambda p:[{**self.grant(p),'validity':{'effective_until':'2026-10-31T01:00:00+00:00'}}]
        self.turn('Helper may read pickup instructions until 2026-10-31T01:00:00+00:00.')
        self.policy=lambda p:[];self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        self.assertTrue(self.selected());calls=len(self.model.calls)
        self.core.clock=lambda:'2026-10-31T02:00:00+00:00'
        self.assertFalse(self.selected());self.assertEqual(len(self.model.calls),calls)

    def test_same_subject_replacement_updates_each_permitted_recipient(self):
        self.turn('Hello.',actor='helper');self.turn('Hello.',actor='outsider')
        self.policy=lambda p:[self.grant(p),self.grant(p,'outsider',1)]
        self.turn('Both helpers may read my pickup instructions.')
        self.policy=lambda p:[];self.classify=lambda p,s:'instruction';self.turn('Pickup Monday at 9 AM.')
        old=self.updater.fact_candidates()[0]['id'];original=self.model.handler
        def replace(p):
            result=original(p)
            if p['stage']=='combined_incremental':
                target=p['relationship_targets'][0]['id']
                for d in result['segments']:
                    d['relationships']=[{'kind':'replaces','target_id':target,
                        'evidence_quote':p['segments'][d['index']]['quote']}]
            return result
        self.model.handler=replace;self.turn('Cancel Monday; pickup Friday at 11 AM instead.')
        for recipient in ['helper','outsider']:
            self.assertEqual([r['text'] for r in self.selected(recipient)],['Cancel Monday; pickup Friday at 11 AM instead.'])
        self.assertTrue(any(p['id']==old for p in self.updater.fact_candidates()))

    def test_independent_state_survives_reopening_and_event_replay_is_idempotent(self):
        self.setup_access();self.classify=lambda p,s:'instruction';job=self.turn('Pickup is at 4 PM.')
        before=len(self.model.calls);self.updater.update(job['job_id'])
        self.assertEqual(len(self.model.calls),before)
        self.core.close()
        self.core=PrivacyMemorySystem(self.path,HashEmbedder(),clock=lambda:'2026-10-31T00:00:00+00:00')
        self.model.core=self.core
        self.host=self.core.issue_session('host','ingest',actions=['manage_policy','ingest','read','search','derive','audit','write','delete','link'],
            compartments=['work'],processing_routes=['test:hash','test:semantic'])
        self.core.register_processor(self.host,self.model)
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='15')
        self.assertEqual([r['text'] for r in self.selected()],['Pickup is at 4 PM.'])
        self.classify=lambda p,s:'context';self.turn('Nothing changed.')
        self.assertEqual(self.model.calls[-1]['required_pairs'],[])

    def test_project_and_personal_domains_use_same_pipeline_without_role_configuration(self):
        for fact,scope in [('Release meeting is at 14:00; bring the draft.','Release meeting logistics'),
                           ('Use the east entrance for parcel collection.','Parcel collection instructions')]:
            self.turn('Hello.',actor='helper');self.policy=lambda p,scope=scope:[self.grant(p,scope=scope)]
            self.classify=lambda p,s:'context';self.turn('Helper may read '+scope+'.')
            self.policy=lambda p:[];self.classify=lambda p,s:'instruction';self.turn(fact)
            self.assertIn(fact,[r['text'] for r in self.selected()])

    def test_new_grant_and_new_facts_share_one_semantic_call(self):
        self.turn('Hello.',actor='helper');self.policy=lambda p:[self.grant(p)]
        self.classify=lambda p,s:'instruction' if s['quote'].startswith('Pickup') else 'context'
        before=len(self.model.calls)
        self.turn('You may read my pickup instructions. Pickup is at 4 PM; bring the permit.')
        self.assertEqual(len(self.model.calls)-before,1)
        self.assertEqual([r['text'] for r in self.selected()],['Pickup is at 4 PM; bring the permit.'])

    def test_missing_extraction_rejoins_next_turn_without_repeating_control_changes(self):
        self.setup_access();original=self.model.handler
        def omit(p):
            result=original(p);result['segments']=[];return result
        self.model.handler=omit;self.turn('Pickup is at 4 PM.')
        self.assertFalse(self.selected())
        self.assertEqual(self.core.db.execute("SELECT count(*) FROM fact_extractions WHERE status='incomplete'").fetchone()[0],1)
        self.model.handler=original
        self.classify=lambda p,s:'instruction' if s['quote'].startswith('Pickup') else 'context'
        before=len(self.model.calls);self.turn('Another conversation.')
        self.assertEqual(len(self.model.calls)-before,1)
        self.assertEqual([r['text'] for r in self.selected()],['Pickup is at 4 PM.'])
        self.assertEqual(self.core.db.execute("SELECT count(*) FROM fact_extractions WHERE status='incomplete'").fetchone()[0],0)

    def test_transport_failure_resumes_valid_control_and_repairs_only_missing_bindings(self):
        from agentic_memory.privacy.processors import ProcessorFailure
        self.setup_access();original=self.model.handler;self.classify=lambda p,s:'instruction'
        def fail_repair(p):
            if p['stage']=='incremental_repair':raise ProcessorFailure('Temporary model transport outage')
            result=original(p);result['binding_reviews']=[];return result
        self.model.handler=fail_repair
        with self.assertRaises(ProcessorFailure):self.turn('Pickup is at 4 PM.')
        job=self.core.db.execute('SELECT id FROM permission_jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        self.model.handler=original;before=len(self.model.calls)
        self.updater.update(job)
        self.assertEqual(len(self.model.calls)-before,1)
        self.assertEqual(self.model.calls[-1]['stage'],'incremental_repair')
        self.assertEqual(self.model.calls[-1]['segments'],[])
        self.assertEqual([r['text'] for r in self.selected()],['Pickup is at 4 PM.'])

    def test_delayed_acceptance_reuses_persisted_worker_receipt(self):
        def pending(p):
            return [{'op':'observe_participation','id':p['new_ids']['activity'][0],
                'participant':self.actor(p,'helper'),'requester':None,'subject':None,
                'activity':'Arrange the owner pickup','status':'tentative','basis':'performance',
                'unresolved':['Awaiting owner acceptance'],'evidence':[p['new_source_id']],
                'reason':'The authenticated worker offers this specific task.'}]
        self.policy=pending;self.turn('May I arrange your pickup?',actor='helper')
        self.policy=lambda p:[]
        for i in range(8):self.turn('Unrelated chat '+str(i))
        def accepted(p):
            activity=next(iter(p['permission_state']['activity']))
            task={'op':'establish_activity','id':activity,'participant':self.actor(p,'helper'),
                'requester':self.actor(p,'owner'),'subject':self.actor(p,'owner'),
                'activity':'Arrange the owner pickup','status':'established','basis':'corroborated',
                'corroboration':[p['new_source_id']],'evidence':[p['new_source_id']],
                'reason':'The owner accepts this persisted pickup offer.',
                'recipient_access':[{'participant':self.actor(p,'helper'),'decision':'allowed',
                    'assignment_id':p['new_ids']['grant'][0],'reason':'The worker needs pickup instructions.'},
                    {'participant':self.actor(p,'owner'),'decision':'unresolved','assignment_id':None,
                        'reason':'No separate self-read assignment is requested here.'}]}
            grant=self.grant(p);grant.pop('authority_source');grant.update(basis='task',activity_id=activity,actions=['read'])
            return [task,grant]
        self.policy=accepted;self.classify=lambda p,s:'instruction' if s['quote'].startswith('Pickup') else 'context'
        self.turn('Yes, please arrange my pickup.')
        activity=next(iter(self.updater.inspect()['activity'].values()))
        self.assertEqual(activity['recipient_access'][0]['binding_status'],'awaiting_facts')
        self.policy=lambda p:[];self.turn('Pickup is at 4 PM.',actor='helper')
        self.assertEqual([r['text'] for r in self.selected()],['Pickup is at 4 PM.'])
        activity=next(iter(self.updater.inspect()['activity'].values()))
        self.assertEqual(activity['status'],'established')
        self.assertEqual(activity['recipient_access'][0]['binding_status'],'bound')
        self.assertEqual(set(next(iter(self.updater.inspect()['assignment'].values()))['actions']),{'read','search','derive'})
        self.classify=lambda p,s:'context';self.turn('Nothing else changed.')
        self.assertEqual(self.model.calls[-1]['required_pairs'],[])

    def test_explicit_question_cannot_become_factual_evidence(self):
        self.setup_access();self.classify=lambda p,s:'instruction'
        self.turn('Is the private entrance code 4519?')
        self.assertEqual(self.updater.fact_candidates(),[])
        self.assertFalse(self.selected())

    def test_invalid_optional_replacement_does_not_discard_an_independent_fact(self):
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        original=self.model.handler
        # Exercise the canonical extraction boundary: a structurally well-formed
        # target can still refer to another subject and must not be applied.
        self.classify=lambda p,s:'context';self.turn('Hello.',actor='outsider')
        self.classify=lambda p,s:'instruction'
        def cross_subject(p):
            result=original(p)
            result['binding_reviews']=[]  # No assignment exists for this subject.
            for s in result['segments']:
                s['subject']=self.actor(p,'outsider')
                s['relationships']=[{'kind':'replaces','target_id':p['relationship_targets'][0]['id'],
                    'evidence_quote':p['segments'][0]['quote']}]
            return result
        self.model.handler=cross_subject
        before=len(self.model.calls);job=self.turn('My separate pickup is at 6 PM.','outsider')
        self.assertEqual(len(self.model.calls)-before,1)
        candidates=self.updater.fact_candidates()
        current=next(p for p in candidates if p['source_id']==job['source_id'])
        self.assertEqual(current['quote'],'My separate pickup is at 6 PM.')
        self.assertEqual(current['relationships'],[])
        receipt=json.loads(self.core.db.execute('SELECT body FROM fact_extractions WHERE job_id=?',(job['job_id'],)).fetchone()[0])
        self.assertTrue(receipt['relationship_omissions'])
        self.assertTrue(self.selected())  # The unrelated authorized fact survives.

    def test_relationship_routing_is_bounded_and_keeps_matching_old_and_recent_facts(self):
        candidates=[{'id':str(i),'quote':('Old parcel permit blue-4519' if i==0 else f'Unrelated observation {i}')} for i in range(120)]
        selected=incremental.relationship_index(candidates,'Cancel the old blue-4519 parcel permit.')
        self.assertEqual(len(selected),incremental.MAX_RELATIONSHIP_TARGETS)
        self.assertIn(candidates[0],selected)
        self.assertTrue(all(p in selected for p in candidates[-8:]))
        self.assertEqual(len(candidates),120)

    def test_focused_control_repair_preserves_other_valid_operations(self):
        self.turn('Hello.',actor='helper')
        def policy(p):
            first=self.grant(p);second=self.grant(p,slot=1,scope='Meeting room instructions')
            second.pop('authority_source');second.update(basis='task',activity_id=p['new_ids']['activity'][0])
            return [first,second]
        self.policy=policy;original=self.model.handler
        def repair(p):
            if p['stage']=='combined_incremental':return original(p)
            self.assertEqual(p['correction']['operation_index'],1)
            self.assertEqual(len(p['correction']['preserved_permission_changes']),2)
            return {'control_review':[{'index':0,'status':'changed','operations':[0,1],
                'reason':'The owner explicitly allows both scopes.'}],
                'permission_changes':[self.grant(p,slot=1,scope='Meeting room instructions')],
                'segments':[],'binding_reviews':[]}
        self.model.handler=repair;before=len(self.model.calls)
        self.turn('Helper may read my pickup instructions and meeting room instructions.')
        self.assertEqual(len(self.model.calls)-before,2)
        self.assertEqual(len(self.updater.inspect()['assignment']),2)

    def test_restriction_replacement_uses_fresh_host_id_and_later_authority(self):
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        def restriction(p):
            return [{'op':'record_restriction','effect':'deny','id':p['new_ids']['restriction'][0],
                'participants':[self.actor(p,'helper')],'subject':self.actor(p,'owner'),
                'information_scope':'Pickup instructions','actions':['search','read','derive'],
                'authority_source':p['new_source_id'],'evidence':[p['new_source_id']],
                'blocked_assignments':list(p['permission_state']['assignment']),
                'reason':'The owner prohibits sharing pickup instructions.'}]
        self.policy=restriction;self.classify=lambda p,s:'context'
        self.turn('Do not share pickup instructions with helper.')
        self.assertFalse(self.selected());old=next(iter(self.updater.inspect()['restriction']))
        def replacement(p):
            r=restriction(p)[0];r.pop('id');r.update(op='replace_restriction',
                restriction_id=next(iter(p['permission_state']['restriction'])),
                information_scope='Private code',blocked_assignments=[])
            return [r,self.grant(p)]
        self.policy=replacement;self.turn('Replace that restriction: helper may read pickup instructions, but not the private code.')
        restrictions=self.updater.inspect()['restriction']
        self.assertEqual(len(restrictions),2)
        self.assertNotEqual(restrictions[old]['superseded_by'],old)
        self.assertEqual([r['text'] for r in self.selected()],['Pickup is at 4 PM.'])

    def test_generic_query_flow_distinguishes_missing_and_denied_without_writing_access(self):
        from agentic_memory.privacy.answering import PermissionQueryService
        self.setup_access();self.classify=lambda p,s:'instruction'
        self.binding=lambda p,r:{**r,'in_scope':next(s['quote'] for s in p['segments']
                if s['candidate_id']==r['candidate_id']).startswith('Pickup'),
            'subject_supported':True,'blocked_by':[],'reason':'Pickup scope excludes the separate private code.'}
        self.turn('Pickup is at 4 PM. Private code is 4519.')
        class AnswerModel:
            processor_id='test:answer';require_answer_plan=True
            def __init__(self):self.calls=[]
            def complete(model,system,payload,*,schema=None):
                model.calls.append(deepcopy(payload))
                if 'requests' in payload:
                    return {'decisions':{str(i):'code' in request
                        for i,request in enumerate(payload['requests'])}}
                q=payload['query'];supported='time' in q
                evidence=[payload['evidence'][0]['id']] if supported else []
                return {'request_parts':[{'request':q,'status':'supported' if supported else 'missing',
                    'evidence_ids':evidence}],
                    'action':'answer' if supported else 'no_memory',
                    'answer':'Pickup is at 4 PM.' if supported else 'Unavailable.',
                    'answer_structured':{},'used_record_ids':evidence}
        model=AnswerModel();self.core.register_processor(self.host,model)
        service=PermissionQueryService(self.updater);snapshot=self.updater._snapshot()
        result=service.answer('helper','What time is pickup?',model)
        self.assertEqual(result['output']['action'],'answer')
        result=service.answer('helper','What pickup day is documented?',model)
        self.assertEqual(result['output']['action'],'no_memory')
        result=service.answer('helper','What is the private code?',model)
        self.assertEqual(result['output']['action'],'refuse')
        result=service.answer('unknown','What time is pickup?',model)
        self.assertEqual(result['output']['action'],'refuse')
        self.assertEqual(self.updater._snapshot(),snapshot)
        self.assertNotIn('4519',json.dumps(model.calls))
        answer_calls=[c for c in model.calls if 'evidence' in c]
        self.assertTrue(all('approved_scopes' not in c for c in answer_calls))

    def test_interrupted_publication_reuses_accepted_semantics_without_a_model_call(self):
        from unittest.mock import patch
        self.setup_access();self.classify=lambda p,s:'instruction'
        with patch.object(self.updater,'_publish',side_effect=RuntimeError('Interrupted host publication')):
            with self.assertRaisesRegex(RuntimeError,'Interrupted host'):self.turn('Pickup is at 4 PM.')
        job=self.core.db.execute('SELECT id FROM permission_jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0]
        self.assertFalse(self.selected());before=len(self.model.calls)
        self.updater.update(job)
        self.assertEqual(len(self.model.calls),before)
        self.assertEqual([r['text'] for r in self.selected()],['Pickup is at 4 PM.'])

    def test_other_recipient_restriction_does_not_trigger_old_binding_reviews(self):
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        self.classify=lambda p,s:'context';self.turn('Hello.',actor='outsider')
        def restriction(p):
            return [{'op':'record_restriction','effect':'deny','id':p['new_ids']['restriction'][0],
                'participants':[self.actor(p,'outsider')],'subject':self.actor(p,'owner'),
                'information_scope':'Pickup instructions','actions':['search','read','derive'],
                'authority_source':p['new_source_id'],'evidence':[p['new_source_id']],
                'blocked_assignments':[],'reason':'The owner prohibits outsider access.'}]
        self.policy=restriction;before=len(self.model.calls)
        self.turn('Outsider must not receive my pickup instructions.')
        self.assertEqual(len(self.model.calls)-before,1)
        self.assertTrue(self.selected());self.assertFalse(self.selected('outsider'))
        self.policy=lambda p:[];self.turn('Nothing else changed.')
        self.assertEqual(self.model.calls[-1]['required_pairs'],[])

    def test_large_late_grant_backlog_is_bounded_durable_and_explicitly_drained(self):
        self.turn('Hello.',actor='helper');self.classify=lambda p,s:'instruction'
        for i in range(18):self.turn(f'Pickup stop {i} is outside entrance {i}.')
        self.classify=lambda p,s:'context';self.policy=lambda p:[self.grant(p)]
        before=len(self.model.calls);self.turn('Helper may read my pickup instructions.')
        self.assertEqual(len(self.model.calls)-before,2)
        self.assertEqual(len(self.selected()),incremental.MAX_BINDING_REVIEWS)
        self.assertEqual(len(self.updater.fact_candidates()),18)
        original=self.model.handler
        def maintenance(p):
            if p['stage']!='binding_maintenance':return original(p)
            return {'binding_reviews':[{'candidate_id':r['candidate_id'],'assignment_id':r['assignment_id'],
                'in_scope':True,'subject_supported':True,'blocked_by':[],
                'reason':'The retained pickup instruction fits this recipient explicit grant.'} for r in p['required_pairs']]}
        self.model.handler=maintenance
        grants=len(self.updater.inspect()['assignment']);before=len(self.model.calls)
        result=self.updater.flush_bindings(max_batches=2)
        self.assertEqual(result['status'],'complete');self.assertEqual(len(self.model.calls)-before,1)
        self.assertEqual(len(self.selected()),18)
        self.assertEqual(len(self.updater.inspect()['assignment']),grants)

    def test_binding_maintenance_cannot_bypass_unresolved_controls(self):
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        self.seq+=1
        self.updater.ingest(PermissionSourceEvent(str(self.seq),'owner','connector','Stop sharing that.'))
        before=len(self.model.calls)
        self.assertEqual(self.updater.flush_bindings()['status'],'control_hold')
        self.assertEqual(len(self.model.calls),before)
        self.assertFalse(self.selected())

    def test_initial_grants_and_automatic_self_grants_are_rejected(self):
        from dataclasses import replace
        with self.assertRaisesRegex(ValueError,'model-proposed self grants'):
            PermissionUpdater(self.core,self.host,'work',replace(self.bootstrap,
                self_use_actions=('read',)),self.model)
        bootstrap=replace(self.bootstrap,participants={'owner':{}},resources={'owner':{}},
            source_identities={'connector':['owner']},
            authorities={'owner':{'permission':['owner'],'restrict':['owner'],'delegate':['owner']}},
            initial_permissions=({'id':'seed','participant':'owner','subject':'owner','actions':['read'],
                'information_scope':'all','source':'preset','scope':'all'},))
        with self.assertRaisesRegex(ValueError,'empty requester permissions'):
            PermissionUpdater(self.core,self.host,'work',bootstrap,self.model)

    def test_missing_beneficiary_binding_does_not_reject_worker_or_copy_their_grant(self):
        self.turn('Hello.',actor='helper')
        self.policy=lambda p:[self.grant(p),self.grant(p,'owner',1)]
        self.turn('Helper and I may each read the pickup instructions.')
        self.policy=lambda p:[];self.classify=lambda p,s:'instruction'
        self.binding=lambda p,r:{**r,'in_scope':True,'subject_supported':True,
            'blocked_by':['invented'] if p['permission_state']['assignment'][r['assignment_id']]['participant']==self.actor(p,'owner') else [],
            'reason':'Independent decision for this recipient.'}
        self.turn('Pickup is at 4 PM; bring the permit.')
        self.assertTrue(self.selected('helper'));self.assertFalse(self.selected('owner'))
        self.binding=lambda p,r:{**r,'in_scope':True,'subject_supported':True,'blocked_by':[],
            'reason':'The owner own accepted assignment covers these pickup instructions.'}
        self.classify=lambda p,s:'context';before=len(self.model.calls)
        self.turn('Nothing else changed.')
        self.assertEqual(len(self.model.calls)-before,1)
        self.assertEqual([r['text'] for r in self.selected('owner')],['Pickup is at 4 PM; bring the permit.'])
        self.assertEqual(len(self.updater.inspect()['assignment']),2)

    def test_identical_binding_duplicates_do_not_trigger_a_repair(self):
        self.setup_access();self.classify=lambda p,s:'instruction';original=self.model.handler
        def duplicate(p):
            result=original(p);result['binding_reviews']*=2;return result
        self.model.handler=duplicate;before=len(self.model.calls)
        self.turn('Pickup is at 4 PM.')
        self.assertEqual(len(self.model.calls)-before,1)
        self.assertEqual(len(self.selected()),1)

    def test_rephrased_duplicate_binding_keeps_validated_access(self):
        self.setup_access();self.classify=lambda p,s:'instruction';original=self.model.handler
        def duplicate(p):
            result=original(p)
            repeated=deepcopy(result['binding_reviews'])
            for d in repeated:d['reason']='Same decision with a different explanation.'
            result['binding_reviews'].extend(repeated)
            return result
        self.model.handler=duplicate;before=len(self.model.calls)
        self.turn('Pickup is at 4 PM.')
        self.assertEqual(len(self.model.calls)-before,1)
        self.assertEqual(len(self.selected()),1)

    def test_fact_only_medication_update_preserves_doses_limits_and_exceptions(self):
        self.classify=lambda p,s:'instruction'
        text='Stop ibuprofen. Take 2.5 mg nightly unless fasting. Keep the total under 3,000 mg per day.'
        self.turn(text)
        self.assertFalse(self.selected('owner'))
        self.assertEqual(len(self.updater.fact_candidates()),3)
        self.classify=lambda p,s:'context'
        self.policy=lambda p:[self.grant(p,'owner',scope='Medication instructions')]
        self.turn('I authorize my own access to these medication instructions.')
        exposed=' '.join(r['text'] for r in self.selected('owner'))
        for detail in ['Stop ibuprofen.','2.5 mg nightly unless fasting.','under 3,000 mg per day.']:
            self.assertIn(detail,exposed)
        grants=len(self.updater.inspect()['assignment'])
        self.policy=lambda p:[];self.classify=lambda p,s:'instruction'
        before=len(self.model.calls);self.turn('Continue one tablet with food; do not double a missed dose.')
        self.assertEqual(len(self.model.calls)-before,1)
        self.assertEqual(len(self.updater.inspect()['assignment']),grants)
        self.assertIn('Continue one tablet with food; do not double a missed dose.',
            [r['text'] for r in self.selected('owner')])

    def test_no_progress_maintenance_has_a_durable_budget(self):
        self.setup_access();self.classify=lambda p,s:'instruction';original=self.model.handler
        def omit(p):
            if p['stage']=='binding_maintenance':return {'binding_reviews':[]}
            result=original(p);result['binding_reviews']=[];return result
        self.model.handler=omit;self.turn('Pickup is at 4 PM.')
        self.assertEqual(self.updater.flush_bindings()['status'],'pending')
        self.assertEqual(self.updater.flush_bindings()['status'],'pending')
        before=len(self.model.calls)
        self.assertEqual(self.updater.flush_bindings()['status'],'repair_exhausted')
        self.assertEqual(len(self.model.calls),before)
        self.assertFalse(self.selected())

    def test_opaque_ids_do_not_rewrite_action_names_or_source_envelopes(self):
        value={'bootstrap':{'participants':{'read':{},'status':{}},'task_actions':['read','search','derive']},
            'work':'read','via':'response',
            'permission_state':{'assignment':{'g-real':{'participant':'read','subject':'status',
                'actions':['read','search'],'status':'active'}}},
            'sources':[{'id':'s1','source':{'participant':'read','event_id':'read','source_identity':'read'},'text':'read'}]}
        mapped=incremental.remap(value,{'read':'p1','status':'p2','response':'p3','g-real':'g1'})
        self.assertEqual(mapped['work'],'read');self.assertEqual(mapped['via'],'response')
        self.assertEqual(mapped['permission_state']['assignment']['g1']['actions'],['read','search'])
        self.assertEqual(mapped['permission_state']['assignment']['g1']['participant'],'p1')
        self.assertEqual(mapped['sources'][0]['source']['event_id'],'read')
        self.assertEqual(mapped['sources'][0]['source']['source_identity'],'read')
        self.assertEqual(mapped['sources'][0]['text'],'read')

    def test_invalid_worker_controls_can_be_withdrawn_without_changing_owner_policy(self):
        from agentic_memory.privacy.incremental_wire import encode_fixture
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        self.classify=lambda p,s:'context';original=self.model.handler
        def speculative(p):
            if p['stage']=='incremental_repair':
                self.assertEqual(p['correction']['mode'],'reassess_invalid_controls')
                return {'c':[0],'p':[],'f':[],'b':[]}
            value=original(p)
            # The worker cannot authorize a new grant for the owner. The
            # first proposal also wrongly calls a factual update a task change.
            op=self.grant(p);op['authority_source']=p['new_source_id']
            value['permission_changes']=[op]
            value['control_review']=[{'index':0,'status':'changed','operations':[0],
                'reason':'Mistaken task interpretation.'}]
            compact=encode_fixture(value,p);compact['c'][0]['k']=['task']
            return compact
        self.model.handler=speculative
        before=len(self.updater.inspect()['assignment'])
        self.turn('The pickup instructions are unchanged.',actor='helper')
        self.assertEqual(len(self.updater.inspect()['assignment']),before)
        self.assertTrue(self.selected())

    def test_reassessment_publishes_fresh_bindings_instead_of_rejected_first_bindings(self):
        from agentic_memory.privacy.incremental_wire import encode_fixture
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        original=self.model.handler
        def speculative(p):
            value=original(p)
            if p['stage']=='incremental_repair':
                value['control_review']=[{'index':0,'status':'no_change','operations':[],
                    'reason':'Only the pickup fact changes; existing scope is unchanged.'}]
                return value
            op=self.grant(p);value['permission_changes']=[op]
            value['control_review']=[{'index':0,'status':'changed','operations':[0],
                'reason':'Mistaken task interpretation.'}]
            value['segments'][0]['relationships']=[{'kind':'replaces',
                'target_id':p['relationship_targets'][0]['id'],'evidence_quote':p['segments'][0]['quote']}]
            for b in value['binding_reviews']:b['assignment_id']=op['id']
            compact=encode_fixture(value,p);compact['c'][0]['k']=['task']
            return compact
        self.model.handler=speculative;self.turn('Pickup is at 5 PM instead of 4 PM.',actor='helper')
        self.assertEqual([r['text'] for r in self.selected()],['Pickup is at 5 PM instead of 4 PM.'])

    def test_missing_denial_is_appended_while_exact_duplicate_grants_are_merged(self):
        from agentic_memory.privacy.incremental_wire import encode_fixture
        self.turn('Hello.',actor='helper');self.classify=lambda p,s:'instruction'
        self.turn('Pickup is at 4 PM. Private code is 4519.')
        self.classify=lambda p,s:'context';self.policy=lambda p:[self.grant(p)]
        original=self.model.handler
        def bindings(p):
            return [{'f':c['id'],'g':p['new_ids']['grant'][0],
                'd':'allow' if c['quote'].startswith('Pickup') else 'outside',
                'deny':[] if c['quote'].startswith('Pickup') else [p['new_ids']['restriction'][0]],
                'r':'Evaluate this fact against pickup scope and the code prohibition.'} for c in p['candidates']]
        def respond(p):
            if p['stage']=='incremental_repair':
                self.assertEqual(p['correction']['mode'],'append_effect')
                return {'c':[{'i':0,'s':'changed','k':['grant'],'o':[0],'r':'Owner grants pickup scope.'},
                    {'i':1,'s':'changed','k':['deny'],'o':[1],'r':'Owner prohibits the code.'}],
                    'p':[{'o':'deny','i':p['new_ids']['restriction'][0],
                        's':self.actor(p,'owner'),'ps':[self.actor(p,'helper')],'scope':'Private code',
                        'a':['read'],'e':p['new_source_id'],'ev':[p['new_source_id']],'blocked':[],
                        'r':'Owner prohibits access to the private code.'}],
                    'f':[],'b':bindings(p)}
            value=original(p);duplicate=deepcopy(value['permission_changes'][0])
            duplicate['id']=p['new_ids']['grant'][1];value['permission_changes'].append(duplicate)
            compact=encode_fixture(value,p)
            compact['c'][1]={'i':1,'s':'changed','k':['deny'],'o':[1],'r':'Owner prohibits the code.'}
            return compact
        self.model.handler=respond
        self.turn('Helper may read my pickup instructions. Do not share the private code with helper.')
        self.assertEqual(len(self.updater.inspect()['assignment']),1)
        self.assertEqual(len(self.updater.inspect()['restriction']),1)
        self.assertEqual([r['text'] for r in self.selected()],['Pickup is at 4 PM.'])

    def test_task_id_bookkeeping_reuses_only_one_exact_saved_worker_receipt(self):
        def pending(p):
            return [{'op':'observe_participation','id':p['new_ids']['activity'][0],
                'participant':self.actor(p,'helper'),'requester':None,'subject':None,
                'activity':'Arrange pickup','status':'tentative','basis':'performance',
                'unresolved':['Awaiting owner acceptance'],'evidence':[p['new_source_id']],
                'reason':'Worker offers this specific task.'}]
        self.policy=pending;self.turn('May I arrange your pickup?',actor='helper')
        original_id=next(iter(self.updater.inspect()['activity']))
        def accept(p):
            new=p['new_ids']['activity'][0]
            grant=self.grant(p);grant.pop('authority_source');grant.update(basis='task',activity_id=new,actions=['read'])
            return [{'op':'establish_activity','id':new,'participant':self.actor(p,'helper'),
                'requester':None,'subject':self.actor(p,'owner'),'activity':'Arrange pickup',
                'status':'established','basis':'corroborated','corroboration':[p['new_source_id']],
                'evidence':[p['new_source_id']],'reason':'The owner accepts the saved task.',
                'recipient_access':[{'participant':self.actor(p,'helper'),'decision':'allowed',
                    'assignment_id':grant['id'],'reason':'Worker needs pickup instructions.'}]},grant]
        self.policy=accept;self.classify=lambda p,s:'instruction' if s['quote'].startswith('Pickup') else 'context'
        self.turn('Yes, please arrange pickup. Pickup is at 4 PM.')
        self.assertEqual(list(self.updater.inspect()['activity']),[original_id])
        self.assertTrue(self.selected());self.assertFalse(self.selected('owner'))


    def test_v16_negative_reassessment_removes_old_field_but_keeps_allowed_field(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.setup_access();self.classify=lambda p,s:'instruction'
        self.turn('Pickup is at 4 PM. Private callback is 555-0199.')
        old=next(r['id'] for r in self.selected() if 'callback' in r['text'])
        self.classify=lambda p,s:'context'
        self.policy=lambda p:[{'op':'record_restriction','id':p['new_ids']['restriction'][0],
            'subject':self.actor(p,'owner'),'participants':[self.actor(p,'helper')],
            'information_scope':'Private code','actions':['read','search','derive'],'effect':'deny',
            'authority_source':p['new_source_id'],'evidence':[p['new_source_id']],
            'blocked_assignments':[],'reason':'Only the separate private code is prohibited.'}]
        def review(p,r):
            quote=next(c['quote'] for c in p['candidates'] if c['id']==r['candidate_id'])
            return {**r,'in_scope':'callback' not in quote,'subject_supported':True,'blocked_by':[],
                'reason':'Time is permitted; the separate private contact is outside scope.'}
        self.binding=review
        self.turn('Do not disclose the separate private code.')
        self.assertEqual([r['text'] for r in self.selected()],['Pickup is at 4 PM.'])
        from agentic_memory.privacy.system import AccessDenied
        with self.assertRaises(AccessDenied):self.core.read(self.reader(),old)
        grant=next(iter(self.updater.inspect()['assignment'].values()))
        self.assertEqual(len(grant['bindings']),1)
        self.assertEqual(self.core.db.execute('SELECT COUNT(*) FROM permission_control_holds').fetchone()[0],0)

    def test_v16_later_field_denial_preserves_permitted_logistics_grant(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.setup_access();self.classify=lambda p,s:'instruction'
        def review(p,r):
            candidate=next(c for c in [*p['candidates'],*p['segments']]
                if c.get('id',c.get('candidate_id'))==r['candidate_id'])
            return {**r,'in_scope':'budget' not in candidate['quote'],'subject_supported':True,
                'blocked_by':[],'reason':'Time is permitted, budget is outside scope.'}
        self.binding=review;self.turn('Pickup is at 4 PM. Private budget is 42000.')
        self.classify=lambda p,s:'context'
        self.policy=lambda p:[{'op':'record_restriction','id':p['new_ids']['restriction'][0],
            'subject':self.actor(p,'owner'),'participants':[self.actor(p,'helper')],
            'information_scope':'Budget and assessment notes','actions':['read','search','derive'],
            'effect':'deny','authority_source':p['new_source_id'],'evidence':[p['new_source_id']],
            'blocked_assignments':[],'reason':'The owner prohibits these private fields only.'}]
        complete=self.model.complete;checked=[]
        def enforce(system,p,*,schema=None):
            for variant in schema.get('properties',{}).get('p',{}).get('items',{}).get('anyOf',[]):
                if variant['properties']['o']['enum']==['deny']:
                    checked.append(True)
                    self.assertEqual(variant['properties']['blocked']['maxItems'],0)
            return complete(system,p,schema=schema)
        self.model.complete=enforce
        self.turn('Keep sharing pickup time. Do not share budget or assessment notes.')
        self.assertTrue(checked)
        self.assertEqual([r['text'] for r in self.selected()],['Pickup is at 4 PM.'])
        self.assertTrue(all(a['status']=='active' for a in self.updater.inspect()['assignment'].values()))
        self.assertEqual(self.core.db.execute("SELECT COUNT(*) FROM permission_control_holds WHERE state='active'").fetchone()[0],0)

    def test_v16_negative_receipt_blocks_a_still_materialized_old_view(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        old=self.selected()[0]['id']
        row=self.core.db.execute("SELECT * FROM fact_processing WHERE job_id=?",
            ('incremental-reviews:work',)).fetchone()
        decision=json.loads(row['body']);decision['in_scope']=False
        with self.core.transaction():
            self.core.db.execute('UPDATE fact_processing SET body=? WHERE job_id=? AND key=?',
                (json.dumps(decision),row['job_id'],row['key']))
        self.assertFalse(self.selected())
        from agentic_memory.privacy.system import AccessDenied
        with self.assertRaises(AccessDenied):self.core.read(self.reader(),old)

    def test_v16_stale_positive_receipt_is_unavailable_until_fresh_review(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        self.assertTrue(self.selected())
        with self.core.transaction():
            self.core.db.execute("UPDATE fact_processing SET snapshot='obsolete' WHERE job_id='incremental-reviews:work'")
        self.assertFalse(self.selected())
        self.classify=lambda p,s:'context';self.turn('Thank you.')
        self.assertTrue(self.selected())

    def test_v16_plan_format_precedes_answer_and_receives_only_permitted_facts(self):
        from agentic_memory.privacy.answering import PermissionQueryService,PLAN_FIELDS
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.setup_access();self.classify=lambda p,s:'instruction'
        def review(p,r):
            source=next(c for c in [*p['candidates'],*p['segments']]
                if c.get('id',c.get('candidate_id'))==r['candidate_id'])
            return {**r,'in_scope':'budget' not in source['quote'],'subject_supported':True,
                'blocked_by':[],'reason':'Pickup time is permitted; the budget is outside scope.'}
        self.binding=review;self.turn('Pickup is at 4 PM. Private budget is 42000.')
        plan={k:[] for k in PLAN_FIELDS};plan['current_items']=['Pickup is at 4 PM.']
        class AnswerModel:
            processor_id='test:answer';require_answer_plan=True
            def complete(model,system,payload,*,schema=None):
                self.assertEqual(list(schema['properties'])[:2],['evidence_roles','answer_structured'])
                self.assertEqual(set(schema['properties']['answer_structured']['required']),set(PLAN_FIELDS))
                self.assertNotIn('42000',json.dumps(payload))
                rid=payload['evidence'][0]['id']
                value={'evidence_roles':{str(i):'current' for i in range(len(payload['evidence']))},
                    'answer_structured':plan,'request_parts':[{'request':'Current pickup plan',
                    'status':'supported','evidence_ids':[rid]}],'action':'answer',
                    'answer':'Pickup is at 4 PM.','used_record_ids':[rid]}
                jsonschema.validate(value,schema)
                return value
        model=AnswerModel();self.core.register_processor(self.host,model)
        before=self.updater._snapshot()
        result=PermissionQueryService(self.updater).answer('helper','What is the current pickup plan?',model)
        self.assertEqual(result['output']['answer_structured'],plan)
        self.assertNotIn('evidence_roles',result['output'])
        self.assertEqual(self.updater._snapshot(),before)

    def test_v16_scheduling_expiry_keeps_validator_operation_context(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.setup_access();self.classify=lambda p,s:'instruction';self.turn('Pickup is at 4 PM.')
        self.assertTrue(self.selected())
        self.classify=lambda p,s:'context'
        self.policy=lambda p:[{'op':'schedule_expiry','assignment_id':next(iter(p['permission_state']['assignment'])),
            'effective_at':'2026-11-01T00:00:00+00:00','authority_source':p['new_source_id'],
            'evidence':[p['new_source_id']],'reason':'The authenticated owner limits this access.'}]
        self.turn('Access ends 2026-11-01T00:00:00+00:00.')
        grant=next(iter(self.updater.inspect()['assignment'].values()))
        self.assertEqual(grant['validity']['effective_until'],'2026-11-01T00:00:00+00:00')
        self.assertTrue(self.selected())
        self.core.clock=lambda:'2026-11-02T00:00:00+00:00'
        self.assertFalse(self.selected())
        self.assertEqual(self.core.db.execute('SELECT COUNT(*) FROM permission_control_holds').fetchone()[0],0)

    def test_v16_third_party_data_requires_independent_focused_review(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.setup_access();self.classify=lambda p,s:'instruction'
        original=self.model.handler
        def reviewer(p):
            value=original(p)
            if p['stage']=='incremental_binding_repair':
                for d in value['binding_reviews']:
                    d.update(in_scope=False,reason='The complete field is outside the permitted scope.')
            return value
        self.model.handler=reviewer
        before=len(self.model.calls)
        self.turn('Pickup is at 4 PM.')
        self.assertEqual(len(self.model.calls)-before,2)
        self.assertEqual(self.model.calls[-1]['stage'],'incremental_binding_repair')
        self.assertFalse(self.selected())
        self.assertEqual(self.core.db.execute('SELECT COUNT(*) FROM permission_control_holds').fetchone()[0],0)

    def test_v16_wrong_binding_quote_repairs_without_subject_control_hold(self):
        self.updater=PermissionUpdater(self.core,self.host,'work',self.bootstrap,self.model,version='16')
        self.turn('May I coordinate pickup?',actor='helper')
        self.policy=lambda p:[self.grant(p),self.grant(p,'owner',1)]
        self.turn('Helper may read pickup instructions; I may read my own pickup instructions.')
        self.policy=lambda p:[];self.classify=lambda p,s:'instruction'
        original=self.model.complete
        def corrupt(system,p,*,schema=None):
            result=original(system,p,schema=schema)
            if p['stage']=='combined_incremental':
                for b in result['b']:b['q']='An unrelated private field.'
            return result
        self.model.complete=corrupt
        self.turn('Pickup is at 4 PM.')
        self.assertTrue(self.selected())
        self.assertEqual(self.core.db.execute('SELECT COUNT(*) FROM permission_control_holds').fetchone()[0],0)

    def test_absence_classifier_has_one_structurally_required_slot_per_request(self):
        from agentic_memory.privacy.answering import PermissionQueryService
        self.setup_access()
        service=PermissionQueryService(self.updater)
        class Classifier:
            processor_id='test:answer'
            def complete(model,system,payload,*,schema=None):
                jsonschema.validate({'decisions':{'0':False,'1':True}},schema)
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate({'decisions':{'1':True}},schema)
                return {'decisions':{'0':False,'1':True}}
        model=Classifier();self.core.register_processor(self.host,model)
        self.assertEqual(service.review_absence({'asker':{'principal_id':'helper'}},
            ['pickup time','unrelated private code'],model),[False,True])


if __name__=='__main__':unittest.main()
