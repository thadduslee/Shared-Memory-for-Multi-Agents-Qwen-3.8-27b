"""Bounded task completeness checks, never a role-based permission compiler.

Routing nominates speakers, not grants. A semantic resolution and the ordinary
authority/scope/binding validator are both required before access is published.
"""
from copy import deepcopy
import json
import re

from .system import digest, encoded

PROMPT_VERSION = 'permission-inference-v12-task-resolution'
MAX_AFFECTED = 4
MAX_PENDING = 32
IDLE_TURNS = 32
MAX_HISTORY_ROUNDS = 2
MAX_HISTORY_SOURCES = 6
MAX_REASSESSMENT_RESPONDERS = 4
REASONS = ('accepted_task', 'existing_grant', 'awaiting_acceptance', 'ambiguous',
           'no_task', 'explicit_refusal', 'insufficient_authority', 'completed')


def rows(core, compartment):
    return {r['id']: json.loads(r['body']) for r in core.db.execute(
        'SELECT id,body FROM permission_task_candidates WHERE compartment=?', (compartment,))}


def prepare(core, compartment, sources, prefix, new_source_id):
    """New speaker + adjacent interlocutor + at most two affected pending tasks.

    No keyword, role, evaluator annotation or model-generated scope is used for
    nomination. Terminal no-task judgments can be revisited on a later reply.
    """
    stored = rows(core, compartment)
    position = len(prefix)
    new = sources.get(new_source_id)
    if not new:return {}, stored, position
    actor = new['source']['participant']
    recent = [sid for sid in prefix[-6:] if sid in sources]
    selected = {}

    def nominate(sid):
        cid = 'task:' + digest([compartment, sid])[:24]
        candidate = deepcopy(stored.get(cid, {
            'id': cid, 'participant': sources[sid]['source']['participant'],
            'source_id': sid, 'subject': sources[sid]['source'].get('subject'),
            'task': '', 'activity_id': cid, 'status': 'new',
            'created': position, 'last_progress': position, 'last_review': 0}))
        selected[cid] = candidate

    nominate(new_source_id)
    previous = next((sid for sid in reversed(recent) if sources[sid]['source']['participant'] != actor), None)
    if previous:nominate(previous)
    pending = [c for c in stored.values() if c['status'] in {'unresolved', 'awaiting_facts', 'dormant', 'authorized'}
               and c['source_id'] in sources
               and (c['participant'] == actor or c.get('subject') == actor
                    or (c.get('subject') is None and c['status'] != 'dormant'))]
    pending.sort(key=lambda c: (c['status']=='authorized', c['last_review'], -c['last_progress'], c['id']))
    for candidate in pending:
        if len(selected) >= MAX_AFFECTED:break
        selected.setdefault(candidate['id'], deepcopy(candidate))
    return selected, stored, position


def projection(selected, source_ids):
    result = []
    for cid, row in selected.items():
        item = {k: deepcopy(row[k]) for k in ('id', 'participant', 'subject', 'task', 'activity_id', 'status')}
        if row['source_id'] in source_ids:item['source_id'] = row['source_id']
        result.append(item)
    return result


def add_history(sources, selected, requests, recent):
    """At most six targeted fact sources; no neighbors or authorization replay."""
    loaded = set(recent)
    extra = set()
    results = []
    for request in requests:
        if request['candidate_id'] not in selected:continue
        kind, reference = request['kind'], request['reference']
        matches = []
        for order, (sid, body) in enumerate(sources.items()):
            if kind == 'source':score = int(sid == reference)
            elif kind == 'quote':score = int(reference in body['text'])
            else:
                terms = set(re.findall(r'\w+', reference.casefold()))
                score = len(terms & set(re.findall(r'\w+', body['text'].casefold())))
            if score:matches.append((score, order, sid))
        matches.sort(reverse=True)
        found = []
        for _, _, sid in matches[request['offset']:]:
            if sid not in loaded and len(extra) >= MAX_HISTORY_SOURCES:continue
            loaded.add(sid)
            if sid not in recent:extra.add(sid)
            found.append(sid)
            if len(found) >= MAX_HISTORY_SOURCES:break
        results.append({**request, 'source_ids': found, 'total_matches': len(matches)})
    return {sid: body for sid, body in sources.items() if sid in loaded}, results


def check_requests(result, selected, sources, rounds, revision, fail):
    requests = result.get('context_requests', [])
    if not isinstance(requests, list):fail('Task context_requests must be a list')
    if not requests:return False
    if (result.get('revision') != revision or result.get('operations') != []
            or result.get('task_reviews', []) != []):
        fail('Task fact retrieval requires the current revision and no mutations or task resolutions')
    if rounds >= MAX_HISTORY_ROUNDS or len(requests) > 2:fail('Task fact retrieval budget exceeded')
    for r in requests:
        if (not isinstance(r, dict) or set(r) != {'candidate_id', 'kind', 'reference', 'offset'}
                or r['candidate_id'] not in selected or r['kind'] not in {'source', 'search', 'quote'}
                or not isinstance(r['reference'], str) or not 1 <= len(r['reference']) <= 300
                or type(r['offset']) is not int or not 0 <= r['offset'] <= 100000):
            fail('Invalid bounded task fact request')
        if r['kind'] == 'source' and (r['reference'] not in sources or r['offset'] != 0):
            fail('Task source must be live in this job prefix')
    return True


def contract(payload, schema):
    """An explicit task resolution is separate from the cheap coverage echo."""
    payload = deepcopy(payload);schema = deepcopy(schema)
    string = {'type': 'string', 'minLength': 1, 'maxLength': 240}
    source = {'type': 'string'}
    if payload['sources']:source['enum'] = [s['id'] for s in payload['sources']]
    refs = {'type': 'array', 'items': source}
    nullable = {'type': ['string', 'null']}
    properties = {
        'candidate_id': ({'type': 'string', 'enum': [c['id'] for c in payload['task_candidates']]}
                         if payload['task_candidates'] else {'type':'string'}),
        'decision': {'type': 'string', 'enum': ['authorized', 'unresolved', 'no_authorization']},
        'subject': {**nullable, 'enum': [None, *payload['bootstrap']['resources']]},
        'task': string, 'reason': {**string, 'minLength': 12},
        'reason_code': {'type': 'string', 'enum': list(REASONS)},
        'evidence': {**refs, 'minItems': 1}, 'activity_id': nullable, 'assignment_id': nullable,
        'binding_status': {'type': 'string', 'enum': ['bound', 'not_needed', 'awaiting_facts']},
        'binding_sources': refs,
    }
    def obj(props):return {'type':'object','additionalProperties':False,'properties':props,'required':list(props)}
    facts=obj({'source_id':source,'quote':{'type':'string','minLength':1,'maxLength':8000},'necessity':string,
        'scope_supported':string,'context_preserved':string,'metadata_safe':string,
        'evidence_kind':{'type':'string','enum':['asserted_fact','instruction']},
        'evidence':obj({'necessity':{**refs,'minItems':1},'subject':{**refs,'minItems':1}}),
        'restriction_decisions':{'type':'array','items':obj({'restriction_id':string,'applies':{'type':'boolean'},
            'explanation':string,'evidence':{**refs,'minItems':1}})}})
    authorization=obj({'information_scope':string,
        'actions':{'type':'array','minItems':1,'items':{'type':'string','enum':list(payload['bootstrap']['task_actions'])}},
        'corroboration':{**refs,'minItems':1},
        'checks':obj({k:string for k in ('involvement_supported','corroboration_supports','scope_supported')}),
        'bindings':{'type':'array','maxItems':8,'items':facts}})
    variants=[]
    for decision in ('unresolved','no_authorization'):
        p=deepcopy(properties);p['decision']={'type':'string','enum':[decision]}
        p['reason_code']={'type':'string','enum':(['awaiting_acceptance','ambiguous'] if decision=='unresolved'
            else ['no_task','explicit_refusal','insufficient_authority','completed'])}
        p['assignment_id']={'type':'null'};p['binding_status']={'type':'string','enum':['not_needed']}
        p['binding_sources']={**refs,'maxItems':0}
        variants.append(obj(p))
    linked=deepcopy(properties);linked['decision']={'type':'string','enum':['authorized']}
    linked['assignment_id']={'type':'string','minLength':1}
    linked['reason_code']={'type':'string','enum':['accepted_task','existing_grant']}
    linked['subject']={'type':'string','enum':list(payload['bootstrap']['resources']) or ['unavailable']}
    # A linked review can name only an already committed grant belonging to
    # THIS candidate. New task grants use the atomic branch, which cannot omit
    # the assignment or borrow another recipient's grant.
    assignments=payload['permission_state']['assignment']
    for candidate in payload['task_candidates']:
        for aid,a in assignments.items():
            if a['status']!='active' or a['participant']!=candidate['participant']:continue
            if candidate.get('subject') is not None and candidate['subject']!=a['subject']:continue
            p=deepcopy(linked)
            p['candidate_id']={'type':'string','enum':[candidate['id']]}
            p['subject']={'type':'string','enum':[a['subject']]}
            p['assignment_id']={'type':'string','enum':[aid]}
            p['activity_id']={'type':['string','null'],'enum':[a.get('activity_id',a.get('self_use_activity'))]}
            p['reason_code']={'type':'string','enum':['existing_grant']}
            variants.append(obj(p))
    atomic=deepcopy(linked);atomic['assignment_id']={'type':'null'};atomic['activity_id']={'type':'null'}
    atomic['authorization']=authorization;atomic['reason_code']={'type':'string','enum':['accepted_task']}
    atomic['binding_sources']={**refs,'maxItems':0}
    atomic['binding_status']={'type':'string','enum':['bound','awaiting_facts']}
    variants.append(obj(atomic))
    schema['properties']['task_reviews'] = {'type': 'array', 'maxItems': MAX_AFFECTED,'items':{'anyOf':variants}}
    slots=payload.get('new_assignment_ids',[])
    if slots:
        assignments=payload['permission_state']['assignment']
        live=[aid for aid,a in assignments.items() if a['status'] in {'active','proposed'}]
        proposed=[aid for aid,a in assignments.items() if a['status']=='proposed']
        for variant in schema['properties']['operations']['items']['anyOf']:
            p=variant['properties'];op=p['op']['enum'][0]
            if op=='propose_assignment':p['id']={'type':'string','enum':[*proposed,*slots]}
            if op in {'bind_information','schedule_expiry','suspend_assignment','revoke_assignment'}:
                # Source-time validation still checks existence and final state.
                p['assignment_id']={'type':'string','enum':[*assignments,*slots]}
    if 'task_reviews' not in schema['required']:schema['required'].append('task_reviews')
    request = {'candidate_id': properties['candidate_id'],
        'kind': {'type': 'string', 'enum': ['search', 'source', 'quote']},
        'reference': {'type': 'string', 'minLength': 1, 'maxLength': 300},
        'offset': {'type': 'integer', 'minimum': 0, 'maximum': 100000}}
    schema['properties']['context_requests'] = {'type': 'array', 'maxItems': 2, 'items': {
        'type': 'object', 'additionalProperties': False, 'properties': request, 'required': list(request)}}
    payload['contract_notes']['task_resolution'] = (
        'Return one task_review for EVERY task_candidates ID. reviewed/unchanged coverage is NOT a task '
        'resolution. Candidates nominate dialogue to assess, never authority. A question can be worker '
        'performance evidence despite having no assertable factual span. unresolved creates durable '
        'tentative involvement and must name the possible task/subject (null if uncertain). On subject '
        'acceptance, establish that task, propose a narrow assignment, and bind its necessary facts. '
        'authorized must link a validated active assignment for this candidate participant/subject. '
        'Use the candidate activity_id to resolve pending performance without replay. For bound, list '
        'every binding_sources ID actually needed and bound to THAT assignment; subject/other-recipient '
        'bindings do not count. If no necessary factual text is available, use awaiting_facts and explain; '
        'not_needed requires a substantive explanation. no_authorization needs no_task, explicit_refusal, '
        'insufficient_authority or completed, plus source evidence and a real reason, not a generic review '
        'acknowledgment. Lack of assertable text is not lack of task acceptance. Use accepted_task or '
        'existing_grant for authorized; awaiting_acceptance or ambiguous for unresolved. Older necessary '
        'facts may be requested with context_requests [{candidate_id,kind,reference,offset}], operations:[] '
        'and task_reviews:[], at most two rounds, six older sources total. Never use those old facts as '
        'fresh consent, supersession, expiry or revocation evidence. No history request is needed normally.')
    payload['contract_notes']['task_authorization']=(
        'For a NEW supported worker task, prefer the atomic authorized task_review with authorization '
        '{information_scope,actions,corroboration,checks,bindings}. Set activity_id and assignment_id null; '
        'the host links the candidate activity, creates a deterministic narrow assignment and compiles '
        'the exact bindings through normal validation. The candidate worker source supplies performance; '
        'corroboration MUST name authenticated subject/authority acceptance. A worker question and a '
        'subject answer have different authors; never swap them. Each binding needs its exact quote and '
        'necessity/scope/context/metadata checks, evidence and all final restriction decisions. Do not '
        'duplicate these atomic operations in operations. Use the linked non-null assignment_id variant '
        'only for a committed assignment of THIS recipient. New task grants use the atomic variant. An established '
        'activity alone is insufficient. Scope should describe the ongoing accepted task and actual '
        'information limits, not only one response sentence; never broaden beyond the accepted purpose.')
    payload['contract_notes']['assignment_ids']=(
        'Normal new propose_assignment operations must use an unused new_assignment_ids slot. A slot is '
        'only a proposed ID, never an existing permission: emit propose_assignment before binding to it. '
        'Never invent a self-use or recipient assignment ID. Atomic authorization links its own IDs. '
        'Existing assignments retain their current IDs. Empty operations retains state without creating grants.')
    if payload.get('task_followup'):
        payload['contract_notes']['focused_reassessment'] = (
            'This is the ONE bounded focused reassessment for the listed unresolved question/reply pairs. '
            'Read their actual dialogue and the configured task convention. The possible_subject is the '
            'authenticated responder, not an asserted permission or hidden relationship. A worker asking '
            'for information needed for a specific task followed by that person supplying it can identify '
            'the subject AND independently corroborate the worker task. Do not require a literal name, '
            'formal delegation or a second supervisor after that acceptance. Determine the actual narrow '
            'task from the exchange; no role/title supplies authority. If supported, establish the pending '
            'activity, create that worker assignment and bind its needed facts. If not supported, name '
            'the specific unresolved identity, refusal, scope or missing acceptance; never treat lack of '
            'a factual span in the QUESTION as lack of task performance. All ordinary validators apply.')
    return payload, schema


def followup_ids(reviews, selected, sources, new_source_id, already, bootstrap):
    """One extra call per job; cache each candidate/responder assessment."""
    if already or new_source_id not in sources:return []
    actor=sources[new_source_id]['source']['participant']
    return [r['candidate_id'] for r in reviews if (r['decision']=='unresolved'
            or (r['decision']=='no_authorization' and r['reason_code']=='no_task'
                and selected[r['candidate_id']]['status'] in {'unresolved','awaiting_facts','dormant'}))
            and selected[r['candidate_id']]['participant']!=actor
            and actor in bootstrap.authorities.get(selected[r['candidate_id']].get('subject') or actor,{}).get('permission',[])
            and actor not in selected[r['candidate_id']].get('reassessed_responders',[])
            and len(selected[r['candidate_id']].get('reassessed_responders',[]))<MAX_REASSESSMENT_RESPONDERS]


def focus_projection(ids, selected, sources, new_source_id, loaded):
    actor=sources[new_source_id]['source']['participant']
    return [{'candidate_id':cid,'participant':selected[cid]['participant'],
             'possible_subject':selected[cid].get('subject') or actor,
             'activity_id':selected[cid]['activity_id'],
             'evidence':[sid for sid in (selected[cid]['source_id'],new_source_id) if sid in loaded]}
            for cid in ids if cid in selected]


def validate_reviews(result, selected, sources, recent, state, fail, subjects):
    """Validate every resolution and stage non-authorizing pending activities."""
    reviews = result.pop('task_reviews', None)
    if (not isinstance(reviews, list) or len(reviews) != len(selected)
            or any(not isinstance(r, dict) or r.get('candidate_id') not in selected for r in reviews)
            or len({r['candidate_id'] for r in reviews}) != len(selected)):
        fail('Every affected task candidate needs an explicit resolution; reviewed/unchanged is insufficient',
             {'required_candidate_ids': list(selected)})
    fields = {'candidate_id','decision','subject','task','reason','reason_code','evidence',
              'activity_id','assignment_id','binding_status','binding_sources'}
    for review in reviews:
        atomic=review.get('authorization')
        if set(review) != fields | ({'authorization'} if atomic is not None else set()):fail('Malformed task resolution')
        if atomic is not None:
            if review.get('decision')!='authorized' or review.get('reason_code')!='accepted_task':
                fail('Atomic task authorization must explicitly assess accepted involvement')
            compile_authorization(review,selected[review['candidate_id']],result,sources,recent,state,fail)
        if (review['decision'] not in {'authorized','unresolved','no_authorization'}
                or review['binding_status'] not in {'bound','not_needed','awaiting_facts'}):fail('Invalid task resolution decision')
        for key in ('task','reason'):
            if not isinstance(review[key],str) or not review[key].strip() or len(review[key])>240:
                fail('Task resolution needs a concise substantive task and reason')
        if len(review['reason'].strip()) < 12:fail('Task resolution needs a substantive reason')
        evidence = review['evidence']
        if (not isinstance(evidence,list) or not evidence or any(s not in recent for s in evidence)):
            fail('Task resolution needs current-window evidence, not historical facts')
        if not isinstance(review['binding_sources'],list) or any(s not in sources for s in review['binding_sources']):
            fail('Task binding_sources must be live loaded sources')
        decision = review['decision']
        allowed = {'authorized': {'accepted_task','existing_grant'},
                   'unresolved': {'awaiting_acceptance','ambiguous'},
                   'no_authorization': {'no_task','explicit_refusal','insufficient_authority','completed'}}
        if review['reason_code'] not in allowed[decision]:fail('Task decision and substantive reason_code disagree')
        row = selected[review['candidate_id']]
        if review['subject'] is not None and review['subject'] not in subjects:
            fail('Task resolution names an unknown subject')
        if row.get('subject') is not None and review['subject'] != row['subject']:
            fail('Task resolution cannot silently change its committed subject')
        if decision != 'authorized' and (review['assignment_id'] is not None or review['binding_sources']
                                         or review['binding_status'] != 'not_needed'):
            fail('An unresolved/unauthorized task cannot claim a grant or information bindings')
        if decision == 'unresolved' and row['activity_id'] not in state['activity']:
            sid = row['source_id']
            if sid not in recent:fail('New tentative task needs its worker source in the current window')
            op = {'op':'observe_participation', 'id':row['activity_id'], 'participant':row['participant'],
                'subject':review['subject'], 'activity':review['task'], 'status':'tentative', 'basis':'performance',
                'performance':[sid], 'unresolved':[{'question':review['reason'],'evidence':evidence}],
                'evidence':{k:[sid] for k in ('participant','activity','status','basis')},
                'explanation':review['reason']}
            if review['subject'] is not None:op['evidence']['subject']=evidence
            # An explicit model activity for the same ID is validated normally.
            if not any(o.get('id')==row['activity_id'] for o in result['operations']):result['operations'].append(op)
    return reviews


def compile_authorization(review, candidate, result, sources, recent, state, fail):
    """Compile explicit semantic decisions, not roles or inferred host scopes."""
    grant=review['authorization']
    if (not isinstance(grant,dict) or set(grant)!={'information_scope','actions','corroboration','checks','bindings'}
            or not isinstance(grant['information_scope'],str) or not grant['information_scope'].strip()
            or not isinstance(grant['bindings'],list) or len(grant['bindings'])>8):
        fail('Atomic task authorization requires a precise scope, acceptance checks and exact bindings')
    corroboration=grant['corroboration'];checks=grant['checks']
    if (not isinstance(corroboration,list) or not corroboration or any(s not in recent for s in corroboration)
            or not isinstance(checks,dict) or set(checks)!={'involvement_supported','corroboration_supports','scope_supported'}
            or any(not isinstance(v,str) or not v.strip() or len(v)>240 for v in checks.values())):
        fail('Atomic task acceptance requires current corroboration and substantive checks')
    if review['assignment_id'] is not None or review['activity_id'] is not None:
        fail('Atomic task IDs are linked by the host; use null IDs in the new-grant variant')
    aid=candidate['activity_id'];worker=candidate['participant'];sid=candidate['source_id']
    gid='task-grant:'+digest([candidate['id'],review['subject'],grant['information_scope'],corroboration])[:24]
    evidence={k:list(corroboration) for k in ('participant','subject','activity','status','basis')}
    if sid in recent:evidence['participant']=[sid]
    activity={'op':'establish_activity','id':aid,'participant':worker,'subject':review['subject'],
        'activity':review['task'],'status':'established','basis':'corroborated','corroboration':corroboration,
        'evidence':evidence,'explanation':review['reason'],
        'self_check':{'involvement_supported':checks['involvement_supported'],
                      'corroboration_supports':checks['corroboration_supports'],'scope_resolved':checks['scope_supported']}}
    if sid in recent:activity['performance']=[sid]
    elif aid not in state['activity']:fail('Historical worker performance must already be a committed pending activity')
    assignment={'op':'propose_assignment','id':gid,'participant':worker,'subject':review['subject'],
        'activity_id':aid,'basis':'task','actions':grant['actions'],'information_scope':grant['information_scope'],
        'status':'active','evidence':{k:list(corroboration) for k in
            ('participant','subject','actions','information_scope','status','basis')},'explanation':review['reason'],
        'self_check':{'basis_supported':checks['corroboration_supports'],
                      'actions_supported':'Configured task actions only.','scope_supported':checks['scope_supported']}}
    if any(o.get('id') in {aid,gid} for o in result['operations']):
        fail('Do not duplicate atomic task activities or grants in operations')
    result['operations'].extend([activity,assignment])
    binding_sources=[]
    keys={'source_id','quote','necessity','scope_supported','context_preserved','metadata_safe',
          'evidence_kind','evidence','restriction_decisions'}
    for fact in grant['bindings']:
        if not isinstance(fact,dict) or set(fact)!=keys:fail('Malformed atomic task fact')
        source_id=fact['source_id']
        if source_id not in sources:fail('Atomic task fact must be loaded and live')
        if not isinstance(fact['evidence'],dict) or set(fact['evidence'])!={'necessity','subject'}:
            fail('Atomic task fact needs necessity and subject evidence')
        result['operations'].append({'op':'bind_information','assignment_id':gid,
            **{k:deepcopy(fact[k]) for k in ('source_id','quote','necessity','evidence_kind','restriction_decisions')},
            'evidence':{**deepcopy(fact['evidence']),'source_id':[source_id]},'explanation':fact['necessity'],
            'self_check':{'task_requires':fact['necessity'],
                **{k:fact[k] for k in ('scope_supported','context_preserved','metadata_safe')},
                'restrictions_checked':'Host checks explicit final restriction decisions.'}})
        binding_sources.append(source_id)
    if review['binding_status']=='bound' and not binding_sources:fail('Bound atomic task needs exact necessary facts')
    if review['binding_status']=='not_needed':fail('A new atomic task must bind facts or explicitly await unavailable facts')
    review['assignment_id']=gid;review['activity_id']=aid
    review['binding_sources']=list(dict.fromkeys(binding_sources))


def finish(reviews, selected, stored, position, validated, sources, fail, coverage=(), focused=()):
    """Cross-check claimed grants and exact facts against the validated final state."""
    state, touched, _ = validated
    updates = deepcopy(stored)
    for review in reviews:
        cid = review['candidate_id'];row = deepcopy(selected[cid])
        row.update(subject=review['subject'],task=review['task'],last_review=position,
                   reason=review['reason'],reason_code=review['reason_code'])
        if cid in focused:
            row['reassessment_done']=True
            actor=sources[next(reversed(sources))]['source']['participant']
            row['reassessed_responders']=list(dict.fromkeys([*row.get('reassessed_responders',[]),actor]))
        if review['decision']=='authorized':
            assignment = state['assignment'].get(review['assignment_id'])
            if (not assignment or assignment['status']!='active' or assignment['participant']!=row['participant']
                    or assignment['subject']!=row['subject']):
                fail('Authorized task resolution requires its own validated active narrow assignment. '
                     'Return the atomic authorization object with precise scope, corroboration and exact bindings; '
                     'an established activity alone is insufficient.',
                     {'candidate_id':cid,'participant':row['participant'],'received_assignment_id':review['assignment_id'],
                      'available_assignments':[aid for aid,a in state['assignment'].items()
                          if a['status']=='active' and a['participant']==row['participant'] and a['subject']==row['subject']]})
            if review['activity_id'] is not None and assignment.get('activity_id',assignment.get('self_use_activity'))!=review['activity_id']:
                fail('Task resolution activity does not match its assignment')
            if review['binding_status']=='bound':
                bound = {b['source_id'] for b in assignment['bindings']}
                if not review['binding_sources'] or not set(review['binding_sources'])<=bound:
                    fail('Supported task must bind the declared necessary sources to its own recipient',
                         {'candidate_id':cid,'assignment_id':review['assignment_id']})
            elif review['binding_sources']:fail('Only bound task resolutions may claim binding_sources')
            row['status']='awaiting_facts' if review['binding_status']=='awaiting_facts' else 'authorized'
            row['assignment_id']=review['assignment_id']
            row['last_progress']=position
            # One follow-up record per actual grant, not one per utterance.
            for old in updates.values():
                if old['id']!=cid and old.get('assignment_id')==row['assignment_id'] and old['status']=='authorized':
                    old['status']='superseded'
        else:
            row['status']='unresolved' if review['decision']=='unresolved' else 'no_authorization'
            if row['participant']==sources[next(reversed(sources))]['source']['participant']:
                row['last_progress']=position
        updates[cid]=row
        activity = state['activity'].get(row['activity_id'])
        if activity and activity['status'] in {'observed','tentative'}:
            activity['task_tracking']='pending' if row['status'] in {'unresolved','awaiting_facts'} else 'retired'
            touched.add(('activity',row['activity_id']))
    tracked={r['activity_id'] for r in updates.values()}
    for aid,activity in state['activity'].items():
        if activity['status'] not in {'observed','tentative'} or activity.get('task_tracking')=='retired' or aid in tracked:continue
        refs=activity.get('performance',[]) or activity.get('evidence',{}).get('participant',[])
        sid=next((s for s in refs if s in sources),None)
        if sid is None:continue
        cid='task:activity:'+digest(aid)[:24]
        updates[cid]={'id':cid,'participant':activity['participant'],'subject':activity.get('subject'),
            'source_id':sid,'activity_id':aid,'task':activity['activity'],'status':'unresolved',
            'created':position,'last_progress':position,'last_review':position}
    for decision in coverage:
        if (decision.get('decision')=='unresolved' and not any(
                r['participant']==decision['principal'] and r['status'] in {'unresolved','awaiting_facts'}
                for r in updates.values())):
            fail('Coverage-only unresolved is not durable: return an unresolved task_review or tentative activity',
                 {'principal':decision['principal']})
    pending = [r for r in updates.values() if r['status'] in {'unresolved','awaiting_facts'}]
    pending.sort(key=lambda r:(-r['last_progress'],r['id']))
    for index,row in enumerate(pending):
        if index>=MAX_PENDING or position-row['last_progress']>=IDLE_TURNS or row['source_id'] not in sources:
            row['status']='dormant'
            row['retired_reason']='Pending-task budget/inactivity or missing worker source; no authorization inferred.'
            activity=state['activity'].get(row['activity_id'])
            if activity and activity['status'] in {'observed','tentative'}:
                activity['task_tracking']='retired';touched.add(('activity',row['activity_id']))
    return updates


def publish(core, compartment, updates):
    core.db.executemany('INSERT INTO permission_task_candidates VALUES (?,?,?) '
        'ON CONFLICT(compartment,id) DO UPDATE SET body=excluded.body WHERE body!=excluded.body',
        [(compartment,cid,encoded(row)) for cid,row in updates.items()])


def empty_reviews(payload):
    """Offline smoke only: deliberately makes no semantic authorization claim."""
    return [{'candidate_id':c['id'],'decision':'no_authorization','subject':c['subject'],
        'task':'Uninterpreted offline dialogue','reason':'Offline fixture makes no task or permission inference.',
        'reason_code':'no_task','evidence':[payload['new_source_id']], 'activity_id':None,
        'assignment_id':None,'binding_status':'not_needed','binding_sources':[]}
        for c in payload.get('task_candidates',[])]
