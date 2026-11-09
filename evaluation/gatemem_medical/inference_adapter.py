"""Thin GateMem source translation for the domain-independent updater.

No role-only grants, keyword scopes, episode-ID subject maps or checkpoint inputs.
The old MedicalFixtureAdapter remains an explicitly selectable baseline.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import time
import json

from agentic_memory.privacy.answer_validation_v19 import VERSION as ANSWER_VERSION
from agentic_memory.privacy.permissions import PermissionBootstrap, PermissionSourceEvent, PermissionUpdater, PermissionRepairExhausted
from agentic_memory.privacy.processors import ScriptedProcessor, ModelContractError
from agentic_memory.privacy.system import AccessDenied, digest
from .adapter import ACTIONS
from .input_boundary import INPUT_CONTRACT, episode_header, source_turn
from .evolution_queue import EvolutionQueue
from agentic_memory.privacy.task_tracking import empty_reviews
from agentic_memory.privacy.answering import PermissionQueryService


class EmptyPermissionProcessor(ScriptedProcessor):
    """Explicit offline smoke double; never claims semantic inference quality."""
    processor_id='test:permission'
    def complete(self, system, payload, *, schema=None):
        self.calls.append(payload)
        if payload.get('stage') in {'combined_incremental','incremental_repair'}:
            result={'control_review':[{'index':s['index'],'status':'no_change','operations':[],
                'reason':'Offline double does not infer controls.'} for s in payload['control_segments']],
                'permission_changes':[],
                'segments':[{'index':s['index'],'kind':'context','subject':None,'subject_source':None,
                    'subject_quote':None,'topics':[],'relationships':[],
                    'reason':'Offline double does not infer assertions.'} for s in payload['segments']],
                'binding_reviews':[]}
            from agentic_memory.privacy.incremental_wire import encode_fixture
            return encode_fixture(result,payload)
        if payload.get('stage')=='extract_candidates':
            return {'segments':[{'index':s['index'],'kind':'context','subject':None,
                'subject_source':None,'subject_quote':None,'topics':[],'relationships':[],
                'reason':'Offline smoke double does not infer factual assertions.'} for s in payload['segments']]}
        if payload.get('context',{}).get('mode')=='single-pass':return {'permission_changes':[],'new_facts':[]}
        result={'revision':payload['revision'],
                'considered_restrictions':list(payload['permission_state']['restriction']),
                'operations':[]}
        if 'task_candidates' in payload:result['task_reviews']=empty_reviews(payload)
        return result


# Host settings live in hosts.py. These names stay for callers that import them.
from .hosts import MEDICAL, HOSTS
CARE_SCOPES=MEDICAL.care_scopes
WORKER_ROLES=MEDICAL.worker_roles
TEMPORARY_CARE_HOURS=MEDICAL.temporary_care_hours
SUBJECT_ROLES=MEDICAL.subject_roles
INFORMATION_TYPES=MEDICAL.information_types
ROLE_INFORMATION_TYPES=MEDICAL.role_information_types


def subject_authorities(directory, host):
    """Who may grant, restrict or delete records of subjects that never speak.

    Only directory entities of a subject role get an entry, and only staff whose
    directory role is in host.subject_authority_roles hold it. Those staff are
    known participants from the start so that their first statement can act.
    Person subjects are untouched: they still control their own resource.
    """
    subjects=sorted(pid for pid,person in directory.items()
        if person.get('role') in host.subject_roles and person.get('role') in host.entity_kinds)
    holders=sorted(pid for pid,person in directory.items() if person.get('role') in host.subject_authority_roles)
    if not subjects or not holders:return {}
    return {'participants':{pid:{} for pid in holders},'resources':{s:{} for s in subjects},
        'authorities':{s:{'permission':list(holders),'restrict':list(holders)} for s in subjects}}


class InferenceFixtureAdapter(PermissionQueryService):
    def __init__(self, core, episode, processors, permission_processor, *, permission_version='19', progress=None, retry=None, answer_validation_mode='enforce', answer_validation_observer=None, subject_roles=None, host=MEDICAL):
        if answer_validation_mode not in {'enforce','shadow'}:raise ValueError('Invalid answer validation mode')
        self.host=host
        if subject_roles is None:subject_roles=host.subject_roles
        self.answer_validation_mode=answer_validation_mode
        self.answer_validation_observer=answer_validation_observer
        # Apply the boundary even for callers outside the runner. Only the identity
        # directory crosses it; never scan turns to bootstrap authority in advance.
        header=episode_header(episode,host.entity_kinds)
        self.core=core;self.eid=header['episode_id']
        self.compartment='encounter:'+digest(self.eid)[:24]
        self.processors=processors
        self.progress=progress
        self.retry=retry or (lambda operation,stage:operation())
        self.issuer='fixture-ingestor:'+self.eid
        self.service=core.issue_session(self.issuer,'amem-ingestion',actions=ACTIONS+['ingest','manage_policy','audit'],
            compartments=[self.compartment],expires_in=7*86400,processing_routes=processors)
        self.source_identity='gatemem:'+digest(self.eid)[:24]
        self.source_timezone='UTC'
        family=permission_version.split('-',1)[0]
        incremental=family.isdigit() and int(family)>=15
        ownership=family.isdigit() and int(family)>=19
        self.directory=header['directory'] if ownership else {}
        bootstrap=PermissionBootstrap(participants={},resources={},
            source_identities={self.source_identity:[]},authorities={},
            discover_self_owned_participants=(self.source_identity,),
            self_use_actions=() if incremental else ('search','read','derive'),
            owner_read_actions=('search','read','derive') if ownership else (),
            read_pipeline_actions=('search','derive'),
            task_convention='Authenticated task performance with particular subject/authority acceptance or trusted workflow corroboration, or an authorized handoff, permits only information necessary to that activity. A subject requesting or accepting help also authorizes their own access to responsive facts and instructions for that specific activity; cite their authenticated request/acceptance as authority for a narrow explicit self-assignment. Specific explicit scope limits constrain inferred task access. Roles, kinship, and ownership alone grant nothing.',
            timezone=self.source_timezone,
            context={'input_contract':INPUT_CONTRACT,
                     'identity_convention':'Only speakers observed in the source prefix are known. Each identity controls its own information resource; this supplies no read grant. Infer subjects, relationships, tasks and permission scope from source text. Identity spelling is not permission evidence.',
                     'mediated_read_convention':'Permission to read a field through this assistant includes searching that same field and processing it through the host-approved answer route. It does not grant writes, exports, forwarding, delegation, or additional fields.',
                     'timestamp_convention':'Naive GateMem timestamps use UTC; no clock is invented for missing timestamps.'})
        if ownership:
            from dataclasses import replace
            bootstrap=replace(bootstrap,directory=self.directory,worker_roles=host.worker_roles,care_scopes=host.care_scopes,
                information_types=host.information_types,role_information_types=host.role_information_types,
                temporary_care_hours=host.temporary_care_hours,subject_roles=subject_roles,
                learned_access_sources=host.learned_access_sources,reply_access=host.reply_access,
                task_convention='A personal resource owner may search/read/derive their own attributed information across tasks under the trusted OWNS policy. A worker shown doing care or service work for a subject gets host-compiled access limited to their role, reported through care_participants; never propose a grant for this. Other recipients require authenticated authority or accepted work and only necessary fields. Roles alone, kinship and administrator authority do not confer ownership or read access.',
                context={**bootstrap.context,'identity_convention':'Speakers and people named in the source prefix are known, with the name and role in bootstrap.participants and each turn\'s source.context. Each actor owns only their own personal resource. OWNS grants configured personal read/search/derive across tasks, never access to other actors or shared/company resources. Infer the actual information subject from source evidence; do not assume speaker equals subject. Names and roles identify people; they are not permission evidence.'})
        if ownership and host.task_convention:
            bootstrap=replace(bootstrap,task_convention=host.task_convention,
                context={**bootstrap.context,**host.extra_context,
                    **({'identity_convention':host.identity_convention} if host.identity_convention else {})})
        if ownership and host.subject_authority_roles:
            bootstrap=replace(bootstrap,**subject_authorities(self.directory,host))
        core.register_processor(self.service,permission_processor)
        self.updater=PermissionUpdater(core,self.service,self.compartment,bootstrap,permission_processor,version=permission_version)
        self.evolution_queue=EvolutionQueue(core,self.service,self.compartment)

    def flush_evolution(self, processor, *, max_batches=1, flush=False):
        return self.evolution_queue.drain(processor,max_batches=max_batches,flush=flush)

    def skipped_permissions(self):
        return self.core.db.execute("SELECT COUNT(*) FROM permission_jobs WHERE compartment=? AND status='skipped'",
                                    (self.compartment,)).fetchone()[0]

    def processing_metrics(self):
        from agentic_memory.privacy.fact_candidates import metrics
        result=metrics(self.updater)
        if self.updater.version.split('-',1)[0]=='19':
            result['answer_validation']={'mode':self.answer_validation_mode,'version':ANSWER_VERSION}
            result['task_contracts_v19']=self.updater.task_contract_report()
            result['task_contracts_v19']['input_contract']=INPUT_CONTRACT
        return result

    def _update_permission(self, job_id):
        try:return self.updater.update(job_id)
        except PermissionRepairExhausted as exc:
            if not self.updater.simple:raise
            result=self.updater.skip_exhausted(job_id,max_attempts=2 if self.updater.incremental else 3)
            callback=getattr(self.retry,'permission_skipped',None)
            if callback:callback(job_id,exc,result.get('privacy_assessment'))
            if self.progress:
                final=result.get('privacy_assessment')
                hold=(f'independent privacy assessment {final["outcome"]} ({final["extra_model_calls"]} extra calls; '
                    f'{final["verified_controls"]} verified controls; {final["unresolved"]} unresolved portions released); '
                    'event uncertainty hold released' if final and final['uncertainty_released']
                    else 'protective access hold retained')
                self.progress(f'  {result["status"]} permission {job_id} after exhausted bounded repair; '
                    f'no rejected changes published; {hold}; continuing to the next turn')
            return result

    def query_context(self, visible):
        principal=visible.get('asker',{}).get('principal_id')
        # The evaluator authenticates the requester ID, including an actor who
        # has not spoken yet. Queries never register source identities or grants.
        if not isinstance(principal,str) or not principal:raise AccessDenied('Unknown fixture identity')
        return self.core.issue_session(principal,'amem-answerer',actions=['search','read','derive','link'],
            compartments=[self.compartment],expires_in=7*86400,processing_routes=self.processors)

    def trusted_time(self, turn):
        value=turn.get('timestamp')
        if value:
            parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
            if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=ZoneInfo(self.source_timezone))
            self.core.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',('fixture_time:'+self.eid,parsed.isoformat()))
        row=self.core.db.execute('SELECT value FROM metadata WHERE key=?',('fixture_time:'+self.eid,)).fetchone()
        self.core.clock=lambda:row[0] if row else None

    def speaker_context(self, principal):
        person=self.directory.get(principal)
        return {'speaker_name':person['name'],'speaker_role':person['role']} if person else {}

    def ingest_batch(self, event, turns, evolution_processor=None):
        turns=[source_turn(turn) for turn in turns]
        event_id='boundary:'+str(event['boundary']);content_hash=digest(turns)
        prior=self.core.event(self.service,event_id)
        if prior and prior['digest']!=content_hash:raise ValueError('Changed boundary input')
        if prior and prior['state']=='complete':
            self.trusted_time(turns[-1]);return
        notes=[]
        # A resumed partial boundary may begin with earlier completed turns.
        # Recover pending jobs at their OWN observed time before replay can move
        # the fixture clock backward. Do not expose queries or later sources.
        for jid in self.updater.pending_jobs():
            row=self.core.db.execute('SELECT source_id FROM permission_jobs WHERE id=?',(jid,)).fetchone()
            source=json.loads(self.core.db.execute('SELECT payload FROM resources WHERE id=?',(row[0],)).fetchone()[0])
            if source['source']['observed_at'] is None:
                self.core.db.execute('DELETE FROM metadata WHERE key=?',('fixture_time:'+self.eid,))
            self.trusted_time({'timestamp':source['source']['observed_at']})
            self.retry(lambda:self._update_permission(jid),'permission '+jid)
        for turn in turns:
            started=time.monotonic()
            self.trusted_time(turn)
            # An exhausted rejection becomes a skipped event with a protective
            # read hold. Transient failures still retry the same source first.
            for jid in self.updater.pending_jobs():
                self.retry(lambda:self._update_permission(jid),'permission '+jid)
            source=PermissionSourceEvent(id=turn['turn_id'],participant=turn['speaker']['principal_id'],
                source_identity=self.source_identity,text=turn['text'],
                occurred_at=turn.get('timestamp'),
                context={**({'turn_kind':turn['turn_kind']} if 'turn_kind' in turn else {}),
                    **({'reply_to':turn['reply_to']} if 'reply_to' in turn else {}),
                    **self.speaker_context(turn['speaker']['principal_id'])})
            result=self.updater.ingest(source)
            ingested_at=time.monotonic()
            published=self.retry(lambda:self._update_permission(result['job_id']),'permission '+result['job_id'])
            permission_at=time.monotonic()
            usage=dict(getattr(self.updater.processor,'usage',None) or {})
            # Trusted source-driven maintenance, independent of query text,
            # checkpoint placement and scoring. It cannot create permissions.
            # Its cost is included in source_total and reported separately.
            maintenance={'batches':0,'changes':[]}
            family=self.updater.version.split('-',1)[0]
            attempts=self.core.db.execute('SELECT attempts FROM permission_jobs WHERE id=?',
                (result['job_id'],)).fetchone()[0]
            # V19 shares a two-response source budget with maintenance. Paying
            # for a third review immediately after a two-call update caused
            # large late-turn stalls. Overflow remains protected and pending.
            maintenance_allowed=not (family.isdigit() and int(family)>=19 and attempts>=2)
            if family.isdigit() and int(family)>=17 and maintenance_allowed:
                maintenance=self.retry(lambda:self.updater.flush_bindings(max_batches=1),
                    'binding maintenance '+result['job_id'])
            elif not maintenance_allowed:
                maintenance['status']='deferred_source_budget'
                # Decisions needing no model call still apply on this turn.
                if self.updater.bootstrap.care_scopes:
                    from agentic_memory.privacy.care_v19 import apply_labels
                    maintenance['changes']=apply_labels(self.updater)
            maintained_at=time.monotonic()
            published['changes'].extend(maintenance['changes'])
            # Permission and fields commit each turn. Optional enrichment is
            # durably queued and never makes the next source turn wait.
            ids=sorted({rid for change in published['changes'] for rid in change.get('resource_ids',[]) if rid.startswith('note:')})
            notes.extend(ids)
            if evolution_processor and ids:
                self.evolution_queue.enqueue(result['job_id'],ids,evolution_processor)
            finished=time.monotonic()
            with self.core.transaction():
                for stage,seconds in [('source_ingest',ingested_at-started),
                    ('permission_total',permission_at-ingested_at),
                    ('binding_maintenance',maintained_at-permission_at),('source_total',finished-started)]:
                    self.core.db.execute('INSERT INTO pipeline_timings VALUES (?,?,?) ON CONFLICT(job_id,stage) DO UPDATE SET seconds=seconds+excluded.seconds',
                        (result['job_id'],stage,seconds))
            if self.progress:
                attempts=self.core.db.execute('SELECT attempts FROM permission_jobs WHERE id=?',
                    (result['job_id'],)).fetchone()[0]
                self.progress(f'  source {turn["turn_id"]}: {time.monotonic()-started:.1f}s; '
                    f'permission {permission_at-ingested_at:.1f}s ({attempts} attempts); '
                    f'maintenance {maintained_at-permission_at:.1f}s ({maintenance["batches"]} batches); '
                    f'enrichment queued {len(ids) if evolution_processor else 0} fields; '
                    f'{len(published["changes"])} permission changes; {len(ids)} memory fields; '
                    f'{usage.get("prompt_tokens",0)}/{usage.get("completion_tokens",0)} input/output tokens')
        self.core.record_event(self.service,event_id,content_hash,'complete',{
            'notes':notes,'turn_ids':[t['turn_id'] for t in turns],
            'source_keys':[self.eid+':'+t['turn_id'] for t in turns]})
