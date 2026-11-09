"""Six-event inference, committed grants and separate current policy/audit stores."""
from copy import deepcopy
import json
import unittest

import jsonschema

from security_tests import test_permission_inference as fixtures
from agentic_memory.privacy.permissions import PermissionUpdater, PermissionProposalRejected
from agentic_memory.privacy.permission_state import access_state, context_view, PROMPT_VERSION


class SlidingPermissionTests(unittest.TestCase):
    def setUp(self):
        self.c=fixtures.PermissionInference();self.c.setUp()
        c=self.c
        c.updater=PermissionUpdater(c.core,c.host,'conversation',c.bootstrap,c.model,version='11')
        original=c.binding
        def binding(*args,**kwargs):
            value=original(*args,**kwargs)
            value['evidence'].pop('authorization')
            return value
        c.binding=binding

    def tearDown(self):self.c.tearDown()

    def history(self,n=10):
        c=self.c
        for i in range(n):c.apply(c.ingest(f'Unrelated observation {i}.',actor='outsider'))

    def test_default_is_v11_and_a_strict_window_including_new_event(self):
        c=self.c;c.establish();self.history(30)
        payload=c.model.calls[-1]
        self.assertEqual(c.updater.version,'11')
        self.assertEqual(len(payload['sources']),6)
        self.assertEqual([s['text'] for s in payload['sources']],
                         [f'Unrelated observation {i}.' for i in range(24,30)])
        self.assertEqual(payload['new_source_id'],payload['sources'][-1]['id'])
        self.assertEqual(payload['context']['mode'],'sliding-window')
        self.assertNotIn('ledger_index',payload['context'])
        self.assertNotIn('binding_authorizations',payload)
        self.assertEqual(c.core.db.execute('SELECT prompt_version FROM permission_jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0],PROMPT_VERSION)

    def test_deletion_does_not_backfill_the_window_with_older_events(self):
        sources={str(i):{'text':str(i)} for i in range(10)}
        del sources['5']
        selected,_,_=context_view(sources,{'activity':{},'assignment':{},'restriction':{}},'9',list(map(str,range(10))))
        self.assertEqual(list(selected),['4','6','7','8','9'])

    def test_permission_state_is_evidence_free_and_contains_no_binding_history(self):
        c=self.c;c.establish();self.history()
        state=c.model.calls[-1]['permission_state']
        forbidden={'evidence','scope_evidence','explanation','self_check','bindings','binding_count',
                   'performance','corroboration','policy_hash','inference_version','necessity'}
        def check(value):
            if isinstance(value,dict):
                self.assertFalse(forbidden & value.keys())
                for child in value.values():check(child)
            elif isinstance(value,list):
                for child in value:check(child)
        check(state)
        grant=state['assignment']['access']
        self.assertEqual(grant['participant'],'helper')
        self.assertEqual(grant['subject'],'owner')
        self.assertEqual(grant['information_scope'],'pickup')
        self.assertEqual(grant['validity'],{'end_activity':'ride'})
        self.assertEqual(state,c.updater.access_state())
        rows=c.core.db.execute('SELECT kind,id,body FROM permission_access_state').fetchall()
        stored={kind:{} for kind in state}
        for row in rows:stored[row['kind']][row['id']]=json.loads(row['body'])
        self.assertEqual(stored,state)
        self.assertIn('scope_evidence',c.updater.inspect()['assignment']['access'])

    def test_new_binding_reuses_old_committed_grant_without_original_evidence_in_prompt(self):
        c=self.c;_,old=c.establish();self.history()
        job=c.ingest('Pickup is at 4 PM.')
        calls=len(c.model.calls)
        c.apply(job,[c.binding(job['source_id'])])
        self.assertEqual(len(c.model.calls),calls+1)
        self.assertNotIn(old['source_id'],str(c.model.calls[-1]))
        binding=c.updater.inspect()['assignment']['access']['bindings'][-1]
        self.assertEqual(binding['evidence']['authorization'],[old['source_id']])
        self.assertIn('Pickup is at 4 PM',[r['text'] for r in c.selected()])

    def test_history_and_bindings_do_not_inflate_the_next_policy_prompt(self):
        c=self.c;c.establish()
        for i in range(15):
            job=c.ingest(f'Pickup is at {i:02d} AM.')
            c.apply(job,[c.binding(job['source_id'],spans=[[0,18]])])
        self.assertEqual(len(c.updater.inspect()['assignment']['access']['bindings']),16)
        self.assertNotIn('bindings',str(c.model.calls[-1]['permission_state']))
        before=json.dumps(c.updater.access_state(),sort_keys=True)
        self.history(20)
        self.assertEqual(json.dumps(c.updater.access_state(),sort_keys=True),before)

    def test_model_cannot_supply_its_own_authorization_or_bind_an_old_source(self):
        c=self.c;_,old=c.establish();self.history()
        job=c.ingest('Pickup is at 4 PM.')
        forged=c.binding(job['source_id']);forged['evidence']['authorization']=[job['source_id']]
        with self.assertRaisesRegex(PermissionProposalRejected,'host resolves'):c.apply(job,[forged])
        with self.assertRaisesRegex(PermissionProposalRejected,'six-turn window'):
            c.apply(job,[c.binding(old['source_id'])])
        self.assertFalse(c.selected())
        c.apply(job,[c.binding(job['source_id'])])
        self.assertTrue(c.selected())

    def test_retrieval_is_rejected_without_loading_history(self):
        c=self.c;c.establish();self.history();job=c.ingest('Change the old arrangement.')
        c.model.handler=lambda p:{'revision':p['revision'],'operations':[],
            'context_requests':[{'kind':'search','reference':'arrangement','offset':0}]}
        with self.assertRaisesRegex(PermissionProposalRejected,'cannot retrieve older history'):
            c.updater.run(job['job_id'])
        self.assertEqual(len(c.model.calls[-1]['sources']),6)

    def test_other_recipient_can_be_revoked_after_leaving_the_window(self):
        c=self.c;c.establish();self.history()
        job=c.ingest('Helper must no longer receive my pickup information.')
        c.apply(job,[fixtures.operation('revoke_assignment',
            c.evidence(job['source_id'],'assignment_id','issuer','reason'),
            assignment_id='access',issuer='owner',reason='Owner revoked pickup access.')])
        self.assertEqual(c.updater.access_state()['assignment']['access']['status'],'revoked')
        self.assertFalse(c.selected())

    def test_task_end_expires_committed_grant_without_old_evidence(self):
        c=self.c;c.establish();self.history()
        job=c.ingest('The ride is complete.')
        c.apply(job,[fixtures.operation('end_activity',c.evidence(job['source_id'],'activity_id','reason'),
            activity_id='ride',reason='Ride completed.')])
        self.assertEqual(c.updater.access_state()['assignment']['access']['status'],'expired')
        self.assertFalse(c.selected())

    def test_new_acceptance_can_complete_an_old_committed_pending_task(self):
        c=self.c
        first=c.ingest('I can arrange your ride.',actor='helper')
        c.apply(first,[c.task(first['source_id'],first['source_id'],status='tentative')])
        self.history()
        job=c.ingest('Yes, please arrange it. Pickup is at 4 PM.')
        activity=c.task(first['source_id'],job['source_id']);activity.pop('performance')
        # New decision evidence is current; the prior information need remains
        # available as policy meaning, without asking the model for old IDs.
        activity['information_needs'][0]['evidence']=[job['source_id']]
        c.apply(job,[activity,c.assignment(job['source_id'])])
        self.assertEqual(c.updater.inspect()['activity']['ride']['performance'],[first['source_id']])
        self.assertNotIn(first['source_id'],str(c.model.calls[-1]))

    def test_deleted_original_authority_suspends_grant_without_replaying_it(self):
        c=self.c;_,old=c.establish();self.history()
        c.core.delete(c.host,old['source_id'])
        self.assertFalse(c.selected())
        pending=c.updater.pending_jobs()[0]
        c.model.handler=lambda p:{'revision':p['revision'],'considered_restrictions':[], 'operations':[]}
        c.updater.run(pending)
        self.assertEqual(c.updater.access_state()['assignment']['access']['status'],'suspended')
        self.assertNotIn(old['source_id'],str(c.model.calls[-1]))

    def restriction(self,job,applies=False):
        c=self.c
        value=c.restriction(job['source_id'],scope='diagnosis')
        value['assignment_decisions']=[{'assignment_id':'access','applies':applies,
            'explanation':'Assessed against committed pickup scope.','evidence':[job['source_id']]}]
        return value

    def test_new_disjoint_restriction_preserves_committed_access(self):
        c=self.c;c.establish();self.history();job=c.ingest('Keep diagnosis private; pickup sharing continues.')
        c.apply(job,[self.restriction(job)])
        self.assertTrue(c.selected())
        self.assertNotIn('assignment_decisions',str(c.updater.access_state()))

    def test_new_overlapping_restriction_cannot_leave_old_fields_accessible(self):
        c=self.c;c.establish();self.history();job=c.ingest('Stop sharing pickup instructions.')
        restriction=self.restriction(job,True)
        with self.assertRaisesRegex(PermissionProposalRejected,'Suspend or revoke'):
            c.apply(job,[restriction])
        c.apply(job,[restriction,fixtures.operation('suspend_assignment',
            c.evidence(job['source_id'],'assignment_id','reason'),assignment_id='access',reason='New restriction.')])
        self.assertFalse(c.selected())

    def test_new_restriction_must_review_other_recipients_historical_grants(self):
        c=self.c;c.establish();self.history();job=c.ingest('Keep my diagnosis private.')
        restriction=c.restriction(job['source_id'],scope='diagnosis')
        with self.assertRaisesRegex(PermissionProposalRejected,'assess affected committed'):
            c.apply(job,[restriction])

    def test_old_restriction_remains_enforced_without_replaying_its_source(self):
        c=self.c;job=c.ingest('Do not share pickup instructions with helper.')
        restriction=c.restriction(job['source_id']);restriction['assignment_decisions']=[]
        c.apply(job,[restriction]);self.history()
        c.establish()
        self.assertFalse(c.selected())
        payload=c.model.calls[-1]
        self.assertIn('private',payload['permission_state']['restriction'])
        self.assertNotIn(job['source_id'],str(payload))

    def test_committed_grant_does_not_let_an_outsider_expand_its_scope(self):
        c=self.c;c.establish();self.history()
        job=c.ingest('Helper can read every private record.',actor='outsider')
        grant=c.assignment(job['source_id'],basis='explicit',aid='expanded')
        grant['information_scope']='all private records'
        with self.assertRaisesRegex(PermissionProposalRejected,'authenticated statement'):
            c.apply(job,[grant])
        self.assertNotIn('expanded',c.updater.access_state()['assignment'])
        self.assertFalse(c.selected())

    def test_updating_pending_task_retains_its_committed_expiry_and_information_needs(self):
        c=self.c;job=c.ingest('I can arrange the ride until 13:00.',actor='helper',timestamp='2026-10-26T12:00:00Z')
        task=c.task(job['source_id'],job['source_id'],status='tentative')
        task['validity']={'effective_until':'2026-10-26T13:00:00Z'}
        task['evidence']['validity']=[job['source_id']]
        c.apply(job,[task]);self.history()
        accepted=c.ingest('Yes, please arrange it.')
        update=c.task(job['source_id'],accepted['source_id']);update.pop('performance');update.pop('information_needs')
        c.apply(accepted,[update,c.assignment(accepted['source_id'])])
        state=c.updater.access_state()
        self.assertEqual(state['activity']['ride']['validity'],task['validity'])
        self.assertEqual(state['activity']['ride']['information_needs'],[{'description':'Pickup instructions for this ride'}])
        c.core.clock=lambda:'2026-10-26T14:00:00Z'
        later=c.ingest('Pickup is at 4 PM.');c.apply(later,[c.binding(later['source_id'])])
        self.assertFalse(c.selected())

    def test_compact_contract_and_repair_do_not_leak_original_authorization(self):
        c=self.c;c.establish();self.history();job=c.ingest('Pickup is at 4 PM.')
        c.model.compact_permission_input=True
        captured=[]
        original=c.model.complete
        def complete(system,payload,*,schema=None):
            captured.append((deepcopy(payload),deepcopy(schema)))
            return original(system,payload,schema=schema)
        c.model.complete=complete
        def respond(payload):
            source=next(s for s in payload['sources'] if s['text']=='Pickup is at 4 PM.')
            sid=source['id'];checks=fixtures.checks()
            binding={'op':'bind_information','assignment_id':'access','source_id':sid,
                'quote':'Pickup is at 4 PM' if 'correction' in payload else 'WRONG SOURCE TEXT',
                'necessity':'Pickup coordination.','evidence_kind':'instruction','restriction_decisions':[],
                'evidence':{'necessity':[sid],'subject':[sid]},
                **{k:checks[k] for k in ('scope_supported','context_preserved','metadata_safe')}}
            return {'revision':payload['revision'],'considered_restrictions':[], 'operations':[binding]}
        c.model.handler=respond;c.updater.update(job['job_id'])
        payload,schema=captured[-1]
        self.assertEqual(payload['contract_notes']['wire'],'sliding-window-delta-v1')
        self.assertNotIn('context_requests',schema['properties'])
        self.assertNotIn('authorization',str(payload['correction']['previous_proposal']))
        self.assertFalse(any('binding_quotes' in s for s in payload['sources']))
        branches=[v for v in schema['properties']['operations']['items']['anyOf']
                  if v['properties']['op']['enum']==['bind_information']]
        self.assertEqual(len(branches),1)
        self.assertNotIn('authorization',branches[0]['properties']['evidence']['properties'])
        # Validate the actual compact response against its advertised schema.
        jsonschema.validate(respond(payload),schema)
        self.assertTrue(c.selected())

    def test_projection_drops_unknown_diagnostic_fields_without_mutating_audit(self):
        c=self.c;c.establish();state=c.updater.inspect();original=deepcopy(state)
        state['assignment']['access']['future_debug_notes']='PRIVATE OLD REASONING'
        projected=access_state(state)
        self.assertNotIn('PRIVATE OLD REASONING',str(projected))
        self.assertEqual(state['assignment']['access']['bindings'],original['assignment']['access']['bindings'])

    def test_publication_failure_rolls_back_current_acl_and_audit_together(self):
        c=self.c;job=c.ingest('Pickup is at 9 AM.')
        publish=c.core._publication_hook
        c.core._publication_hook=lambda _:(_ for _ in ()).throw(RuntimeError('injected failure'))
        binding=c.binding(job['source_id'])
        with self.assertRaisesRegex(RuntimeError,'injected failure'):
            c.apply(job,[c.assignment(job['source_id'],basis='explicit'),binding])
        self.assertFalse(c.core.db.execute('SELECT 1 FROM permission_access_state').fetchone())
        self.assertEqual(c.updater.inspect()['assignment'],{})
        self.assertNotIn('authorization',binding['evidence'])
        c.core._publication_hook=publish
        c.apply(job,[c.assignment(job['source_id'],basis='explicit'),binding])
        self.assertTrue(c.selected())

    def test_existing_legacy_grants_cannot_silently_lose_their_scope_limits(self):
        c=fixtures.PermissionInference();c.setUp()
        try:
            c.establish()
            with self.assertRaisesRegex(ValueError,'fresh compartment/store'):
                PermissionUpdater(c.core,c.host,'conversation',c.bootstrap,c.model)
            self.assertTrue(c.selected())
            self.assertFalse(c.core.db.execute("SELECT 1 FROM permission_configurations WHERE version='11'").fetchone())
        finally:c.tearDown()

    def test_current_state_and_window_survive_updater_reconstruction(self):
        c=self.c;c.establish();self.history()
        before=c.updater.access_state()
        c.updater=PermissionUpdater(c.core,c.host,'conversation',c.bootstrap,c.model,version='11')
        self.assertEqual(c.updater.access_state(),before)
        job=c.ingest('Pickup is at 4 PM.');c.apply(job,[c.binding(job['source_id'])])
        self.assertEqual(len(c.model.calls[-1]['sources']),6)
        self.assertTrue(c.selected())

    def test_deletion_of_the_entire_window_can_invalidate_grants_without_backfill(self):
        c=self.c;c.establish()
        ids=[r[0] for r in c.core.db.execute("SELECT id FROM resources WHERE kind='observation'")]
        for sid in ids:c.core.delete(c.host,sid)
        c.model.compact_permission_input=True;c.model.require_permission_coverage=True
        def respond(payload):
            self.assertEqual(payload['sources'],[])
            return {'revision':payload['revision'],'considered_restrictions':[], 'operations':[],
                'coverage':{'reviewed':payload['coverage_required'],'evidence':[],'unresolved':[]}}
        c.model.handler=respond
        for jid in c.updater.pending_jobs():c.updater.run(jid)
        self.assertFalse(c.updater.pending_jobs())
        self.assertEqual(c.updater.access_state()['assignment']['access']['status'],'suspended')
        self.assertFalse(c.selected())


if __name__=='__main__':unittest.main()
