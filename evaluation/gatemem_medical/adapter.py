"""Medical fixture authority lives here, never in agentic_memory.privacy.

Conservative bounded interpretation of authenticated fixture controls. Unknown
permission language does not create an allow. The parser is an evaluation
adapter, not a general-purpose natural-language authorization service.
"""
from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from agentic_memory.privacy.system import AccessDenied, digest

VISIBLE = {'checkpoint_id','episode_id','as_of_turn_id','asker','query_text'}
HIDDEN = {'query_type','attack_type','expected_action','judge_spec','leak_targets',
          'gold_answer_structured','gold_refusal_category','policy_snapshot'}
ACTIONS = ['search','read','derive','write','link','delete','export']


def sanitize(value):
    if isinstance(value,dict):
        return {k:sanitize(v) for k,v in value.items() if k not in HIDDEN}
    if isinstance(value,list):
        return [sanitize(v) for v in value]
    return value


def visible_query(checkpoint):
    result = sanitize({k:v for k,v in checkpoint.items() if k in VISIBLE})
    result['asker'] = {k:v for k,v in result.get('asker',{}).items() if k in {'principal_id','role'}}
    return result


def load_data(data_dir):
    data_dir = Path(data_dir)
    def read(name):
        # Strict loading: silently skipping malformed lines would invalidate coverage.
        return [json.loads(line) for line in (data_dir/name).read_text().splitlines() if line.strip()]
    episodes, checkpoints = read('episodes.jsonl'), read('checkpoints.jsonl')
    if any(e.get('domain') != 'medical' for e in episodes):
        raise ValueError('Expected the explicitly selected medical fixture')
    return episodes,checkpoints


def schedule(episodes, checkpoints):
    by_id = {e['episode_id']:e for e in episodes}
    if len(by_id) != len(episodes):
        raise ValueError('Duplicate episode ID')
    positions = {}
    for eid,ep in by_id.items():
        positions[eid] = {t['turn_id']:i for i,t in enumerate(ep['turns'])}
        if len(positions[eid]) != len(ep['turns']):
            raise ValueError('Duplicate turn ID')
    groups,seen = {},set()
    for cp in checkpoints:
        cid,eid,tid = cp['checkpoint_id'],cp['episode_id'],cp['as_of_turn_id']
        if cid in seen or eid not in positions or tid not in positions[eid]:
            raise ValueError('Invalid checkpoint boundary')
        seen.add(cid)
        groups.setdefault((eid,positions[eid][tid],tid),[]).append(cid)
    for eid,ep in by_id.items():
        if ep['turns']:
            last=ep['turns'][-1]['turn_id']
            groups.setdefault((eid,len(ep['turns'])-1,last),[])
    cursors,events={},[]
    for index,((eid,pos,tid),ids) in enumerate(sorted(groups.items()),1):
        start=cursors.get(eid,-1)+1
        if start>pos:
            raise ValueError('Empty boundary delta')
        events.append({'boundary':index,'episode_id':eid,'as_of_turn_id':tid,
                       'start':start,'end':pos+1,'checkpoint_ids':sorted(ids)})
        cursors[eid]=pos
    return events


class MedicalFixtureAdapter:
    def __init__(self, core, episode, processors):
        self.core,self.episode=core,episode
        self.eid=episode['episode_id']
        self.config=json.loads(Path(__file__).with_name('fixture_policy.json').read_text())
        if self.eid not in self.config['subject_bindings']:
            raise ValueError('No explicit fixture subject/authority binding')
        self.primary=self.config['subject_bindings'][self.eid]
        self.principals={p['principal_id']:p for p in episode['entities']['principals']}
        if self.primary not in self.principals:
            raise ValueError('Invalid configured fixture subject')
        self.processors=processors
        self.issuer=f'fixture-ingestor:{self.eid}'
        self.service=core.issue_session(self.issuer,'amem-ingestion',actions=ACTIONS+['ingest','manage_policy','audit'],
                                        compartments=[self.eid],expires_in=7*86400,processing_routes=processors)
        self.subjects={p['principal_id'] for p in self.principals.values() if p.get('role')=='patient'}
        self.families={r['family_id']:r['patient_id'] for r in episode['entities'].get('relationships',[])
                       if r.get('type') in self.config['family_relationships'] and 'family_id' in r and 'patient_id' in r}
        # Bootstrap once; reopening must never reset revocations or temporary grants.
        bootstrap='fixture-bootstrap:'+self.eid
        if not core.event(self.service,bootstrap):
            with core.transaction():
                for subject in sorted(self.subjects):
                    policy={'allows':[{'principals':[subject,self.issuer],'actions':ACTIONS},
                                       {'groups':[self.group(subject)],'actions':['search','read','derive','link']}],
                            'denies':[],'processors':processors}
                    core.install_policy(self.service,self.policy(subject),policy)
                    # Separate field-policy for trusted logistics projections.
                    core.install_policy(self.service,self.policy(subject)+':logistics',{
                        'allows':policy['allows']+[{'groups':[self.group(subject)+':logistics'],
                                                  'actions':['search','read','derive']}],
                        'denies':[],'processors':processors})
                    core.create(self.service,'guard:'+self.policy(subject),'policy_guard',{},
                                policy_id=self.policy(subject),compartment=self.eid)
                for relation in episode['entities'].get('relationships',[]):
                    if relation.get('type') in self.config['assigned_relationships'] and relation.get('patient_id') in self.subjects:
                        grantee=relation.get('clinician_id') or relation.get('principal_id')
                        if grantee in self.principals:
                            core.set_member(self.service,self.group(relation['patient_id']),grantee)
                    scope=self.config['initial_scopes'].get(relation.get('access_level'))
                    if scope=='logistics' and relation.get('family_id') in self.families:
                        core.set_member(self.service,self.group(relation['patient_id'])+':logistics',relation['family_id'])
                core.record_event(self.service,bootstrap,digest(episode['entities']),'complete',{})

    def policy(self,subject):
        return f'fixture:{self.eid}:{subject}'

    def group(self,subject):
        return f'care:{self.eid}:{subject}'

    def query_context(self,visible):
        principal=visible.get('asker',{}).get('principal_id')
        if principal not in self.principals:
            raise AccessDenied('Unknown fixture identity')
        # Source fixture identity is authoritative; a role claimed inside query text is not.
        return self.core.issue_session(principal,'amem-answerer',actions=['search','read','derive','link'],
                                       compartments=[self.eid],expires_in=7*86400,processing_routes=self.processors)

    def source_subjects(self,turn):
        speaker=(turn.get('speaker') or {}).get('principal_id')
        text=turn.get('text','').lower()
        named={pid for pid in self.subjects if any(re.search(r'\b'+re.escape(name.lower())+r'\b',text)
                for name in [self.principals[pid].get('display_name',''),self.principals[pid].get('display_name','').split(' ')[0]] if name)}
        if speaker in self.subjects:named.add(speaker)
        if not named:
            assigned={r.get('patient_id') for r in self.episode['entities'].get('relationships',[])
                      if r.get('clinician_id')==speaker and r.get('type') in self.config['assigned_relationships']}
            named=assigned & self.subjects
        return sorted(named or {self.primary}) # explicit fixture default, never frequency-inferred

    def trusted_time(self,turn):
        value=turn.get('timestamp')
        if value:
            parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
            if parsed.tzinfo is None:
                # GateMem fixture timestamps lack offsets; UTC is an explicit simulation convention.
                parsed=parsed.replace(tzinfo=timezone.utc)
            self.core.db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)',
                                 ('fixture_time:'+self.eid,parsed.isoformat()))
        row=self.core.db.execute('SELECT value FROM metadata WHERE key=?',('fixture_time:'+self.eid,)).fetchone()
        self.core.clock=lambda:row[0] if row else None

    def controls(self,turn,subject):
        speaker=(turn.get('speaker') or {}).get('principal_id')
        if speaker not in {subject,*self.config['control_principals']}:
            return
        if speaker in self.config['control_principals'] and turn.get('turn_kind') not in {'note_update',None}:
            return
        text=turn.get('text','')
        lower=text.lower()
        # Bounds the supported grant language; a suggestive phrase never authorizes full records.
        relevant=[p for p,s in self.families.items() if s==subject]
        if re.search(r'\b(revok\w*|withdraw\w*|stop sharing|do not share)\b',lower):
            for pid in relevant:
                self.core.set_member(self.service,self.group(subject)+':logistics',pid,False)
        elif re.search(r'\b(only logistics|logistics only|scheduling (?:logistics )?only|appointment times and parking)\b',lower):
            for pid in relevant:
                self.core.set_member(self.service,self.group(subject)+':logistics',pid,True)
        # Confirmed deletion controls are authoritative fixture statements. Requests
        # with future conditions are not treated as completed deletion statements.
        confirmed=(speaker in self.config['control_principals'] and
                   re.search(r'\b(cleared|deleted|removed|purged|forgotten|no longer stored)\b',lower))
        immediate=(speaker==subject and re.search(r'\b(delete|erase|forget|purge)\b',lower) and
                   not re.search(r'\b(after|once|when|later)\b',lower))
        if not (confirmed or immediate):
            return
        literals=re.findall(r'\b\d{3}[- .]\d{3}[- .]\d{4}\b',text)
        aliases=re.findall(r"['\"]([^'\"\n]{5,100})['\"]",text)
        selectors=[v.lower() for v in literals+aliases]
        # If a confirmed contact erasure omits the values, act on configured
        # contact-field shapes rather than asking a model to identify secrets.
        contact=bool(re.search(r'\b(contact|callback|phone|number|alias|voicemail)\b',lower))
        candidates=list(self.core.db.execute('SELECT id,payload FROM resources WHERE compartment=? AND kind=\'observation\' AND state=\'active\'',(self.eid,)))
        for row in candidates:
            body=json.loads(row['payload']).get('text','')
            if (any(s in body.lower() for s in selectors) or
                    (not selectors and contact and re.search(r'\b\d{3}[- .]\d{3}[- .]\d{4}\b',body))):
                self.core.delete(self.service,row['id'])

    def ingest_batch(self,event,turns,evolution_processor=None):
        event_id=f'boundary:{event["boundary"]}'
        content_hash=digest(turns)
        prior=self.core.event(self.service,event_id)
        if prior and prior['digest']!=content_hash:
            raise ValueError('Changed boundary input')
        if prior and prior['state']=='complete':
            self.trusted_time(turns[-1])
            return
        notes=[]
        if prior:
            notes=json.loads(prior['details'])['notes']
            self.trusted_time(turns[-1])
        else:
            with self.core.transaction():
                for turn in turns:
                    self.trusted_time(turn)
                    subjects=self.source_subjects(turn)
                    subject=subjects[0]
                    if len(subjects)==1:self.controls(turn,subject)
                    # Preserve deletion control provenance without reindexing its deleted values.
                    speaker=(turn.get('speaker') or {}).get('principal_id')
                    deletion_control=((speaker in self.config['control_principals'] and
                        re.search(r'\b(cleared|deleted|removed|purged|forgotten|no longer stored)\b',turn.get('text','').lower())) or
                        (speaker==subject and re.search(r'\b(delete|erase|forget|purge)\b',turn.get('text','').lower()) and
                         not re.search(r'\b(after|once|when|later)\b',turn.get('text','').lower())))
                    text=turn.get('text','')
                    if deletion_control:
                        text='An authenticated source confirmed a memory deletion; prior values are unavailable.'
                    oid=f'obs:{self.eid}:{turn["turn_id"]}'
                    note=self.core.ingest(self.service,oid,text,policy_id=self.policy(subject),
                        compartment=self.eid,attributes={'subject':subject,'subjects':subjects,'turn_id':turn['turn_id']},
                        dependencies=['guard:'+self.policy(s) for s in subjects],
                        source={'external_id':turn['turn_id'],'media_type':'text/plain',
                                'adapter':'gatemem-medical-v1','digest':digest(turn),
                                'timestamp':turn.get('timestamp'),'speaker_id':speaker})
                    notes.append(note)
                    # A source-addressable field projection approved by fixture
                    # policy, never an LLM summary of a restricted encounter.
                    if len(subjects)==1 and re.search(r'\b(appointment|arrival|arrive|scheduled|slot|visit)\b',text,re.I):
                        pattern=r'\b(?:[01]?\d|2[0-3]):[0-5]\d\s*(?:AM|PM)?\b|\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b|\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}\b'
                        spans=[(m.start(),m.end()) for m in re.finditer(pattern,text,re.I)][:32]
                        if spans:
                            notes.append(self.core.ingest_authoritative_view(self.service,oid,oid+':logistics',spans,
                                         policy_id=self.policy(subject)+':logistics'))
                self.core.record_event(self.service,event_id,content_hash,'ingested',
                                       {'notes':notes,'turn_ids':[t['turn_id'] for t in turns]})
        notes=[rid for rid in notes if self.core.allowed(self.service,rid)]
        if evolution_processor and notes:
            # Same-source-policy buckets preserve useful isolation; outputs still
            # carry the full actual input conjunction including retrieved neighbors.
            groups={}
            for rid in notes:
                policy=self.core.read(self.service,rid)['policy_id']
                groups.setdefault(policy,[]).append(rid)
            for index,ids in enumerate(groups.values()):
                job=f'{event_id}:evolve:{index}'
                if not self.core.db.execute('SELECT 1 FROM provenance WHERE id=?',(job,)).fetchone():
                    self.core.evolve(self.service,evolution_processor,ids,job_id=job)
        self.core.record_event(self.service,event_id,content_hash,'complete',
                               {'notes':notes,'turn_ids':[t['turn_id'] for t in turns]})
