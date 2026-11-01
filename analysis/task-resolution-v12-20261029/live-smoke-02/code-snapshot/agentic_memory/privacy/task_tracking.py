"""Bounded task completeness checks, never a role-based permission compiler.

Routing nominates speakers, not grants. A semantic resolution and the ordinary
authority/scope/binding validator are both required before access is published.
"""
from copy import deepcopy
import json
import re

from .system import digest, encoded
from .lifecycle import question_spans

PROMPT_VERSION = 'permission-inference-v12-task-resolution'
MAX_AFFECTED = 4
MAX_PENDING = 32
IDLE_TURNS = 32
MAX_HISTORY_ROUNDS = 2
MAX_HISTORY_SOURCES = 6
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
    schema['properties']['task_reviews'] = {'type': 'array', 'maxItems': MAX_AFFECTED,
        'items': {'type': 'object', 'additionalProperties': False, 'properties': properties, 'required': list(properties)}}
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
        'every binding_sources ID actually needed and bound to THAT assignment; patient/other-worker '
        'bindings do not count. If no necessary factual text is available, use awaiting_facts and explain; '
        'not_needed requires a substantive explanation. no_authorization needs no_task, explicit_refusal, '
        'insufficient_authority or completed, plus source evidence and a real reason, not a generic review '
        'acknowledgment. Lack of assertable text is not lack of task acceptance. Use accepted_task or '
        'existing_grant for authorized; awaiting_acceptance or ambiguous for unresolved. Older necessary '
        'facts may be requested with context_requests [{candidate_id,kind,reference,offset}], operations:[] '
        'and task_reviews:[], at most two rounds, six older sources total. Never use those old facts as '
        'fresh consent, supersession, expiry or revocation evidence. No history request is needed normally.')
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
    """One extra call per job, and only once per unresolved question candidate."""
    if already or new_source_id not in sources:return []
    actor=sources[new_source_id]['source']['participant']
    return [r['candidate_id'] for r in reviews if r['decision']=='unresolved'
            and selected[r['candidate_id']]['participant']!=actor
            and actor in bootstrap.authorities.get(selected[r['candidate_id']].get('subject') or actor,{}).get('permission',[])
            and not selected[r['candidate_id']].get('reassessment_done')
            and question_spans(sources[selected[r['candidate_id']]['source_id']]['text'])]


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
        if set(review) != fields:fail('Malformed task resolution')
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


def finish(reviews, selected, stored, position, validated, sources, fail, coverage=(), focused=()):
    """Cross-check claimed grants and exact facts against the validated final state."""
    state, touched, _ = validated
    updates = deepcopy(stored)
    for review in reviews:
        cid = review['candidate_id'];row = deepcopy(selected[cid])
        row.update(subject=review['subject'],task=review['task'],last_review=position,
                   reason=review['reason'],reason_code=review['reason_code'])
        if cid in focused:row['reassessment_done']=True
        if review['decision']=='authorized':
            assignment = state['assignment'].get(review['assignment_id'])
            if (not assignment or assignment['status']!='active' or assignment['participant']!=row['participant']
                    or assignment['subject']!=row['subject']):
                fail('Authorized task resolution requires its own validated active narrow assignment',
                     {'candidate_id':cid,'participant':row['participant']})
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
