"""V19 intent-preserving service repair and evidence-bounded task resolution.

This ledger is not a permission authority. Only an authenticated subject's
grounded acceptance AND a separately validated recipient-specific assignment
can publish accepted service access. Pending rows never authorize a read.
"""
from copy import deepcopy
import json
import re

from .system import digest, encoded, instant
from . import revocation_v19 as recipients

PROMPT_VERSION = 'permission-inference-v19-intent-preserving-service-v1'
INSTRUCTION = '''
V19 service repair: service means SERVICE_ACCEPTANCE, not a disposable grant ID.
task_acceptance_context lists source-grounded outstanding offers, original intent,
subject, service, explicit limits and recipient resolution. An incorrect existing
grant is a REFERENCE_REPAIR: preserve the acceptance, subject, service and original
offer/acceptance receipts. Supply that worker's own matching grant or a narrow
activity via=response with the original offer q. Never reuse somebody else's grant.
An explicit source recipient overrides conversational defaults. No first/last
worker or session-initiator fallback exists. Ambiguity stays pending, with no grant.
On an acceptance repair emit intent_repair: outcome REFERENCE_REPAIR,
PENDING_RECIPIENT_RESOLUTION or SEMANTIC_REASSESSMENT, reason, and contrary_evidence
[{source_id,quote}]. The latter is REQUIRED to retract a mistaken acceptance and
must cite actual contradictory source evidence, not a validation error or a new
paraphrase of the same confirmation. Reassessment retains the original decision.
Dropping service into none without that outcome is never a successful repair.
Adverse controls remain independent and cannot be withdrawn by service repair.
'''


def enabled(updater):
    return recipients.enabled(updater)


def fail(reason):
    from .permissions import PermissionProposalRejected
    raise PermissionProposalRejected(reason, {'stage': 'acceptance_reference_repair',
        'repair_kind': 'REFERENCE_REPAIR', 'preserve_intent': 'SERVICE_ACCEPTANCE'})


def install(updater):
    updater.core.db.execute('''CREATE TABLE IF NOT EXISTS privacy.permission_intents_v19(
        compartment TEXT NOT NULL, job_id TEXT NOT NULL, segment INTEGER NOT NULL,
        body TEXT NOT NULL, PRIMARY KEY(compartment,job_id,segment))''')


def rows(updater, job_id=None):
    sql = 'SELECT body FROM permission_intents_v19 WHERE compartment=?'
    args = [updater.compartment]
    if job_id is not None:
        sql += ' AND job_id=?'; args.append(job_id)
    return [json.loads(r[0]) for r in updater.core.db.execute(sql+' ORDER BY job_id,segment', args)]


def save(updater, row):
    updater.core.db.execute('INSERT OR REPLACE INTO permission_intents_v19 VALUES (?,?,?,?)',
        (updater.compartment, row['job_id'], row['segment'], encoded(row)))


def authorization_snapshot(updater):
    return [r for r in rows(updater) if r['state'] in {'ACCEPTED', 'CANCELLED', 'REVOKED', 'EXPIRED',
        'HANDED_OFF','PENDING_CANCELLATION_RESOLUTION'}]


SERVICES = {'scheduling': r'\b(?:schedul\w*|appointments?|book\w*|arrival|parking|calendar)\b',
    'pickup': r'\b(?:pickup|parcel|collect\w*|delivery)\b',
    'billing': r'\b(?:billing|invoice\w*|payments?|insurance)\b',
    'repair': r'\b(?:repair\w*|fix\w*|maintenance)\b'}
OFFER = re.compile(r"\b(?:I can|may I|can I|would you like me to|shall I|I (?:have )?booked|"
    r"schedule is set|schedule is ready|I (?:can )?offer)\b", re.I)
ACCEPT = re.compile(r"\b(?:I can make (?:that|it)|that works|yes(?:,|\b)|please (?:do|proceed|schedule|collect|arrange)|"
    r"I accept|I agree|go ahead)\b", re.I)
SERVICE_REQUEST = re.compile(r'^(?:(?:can|could) you |please (?:help me )?)'
    r'(?:arrange|schedule|book|collect|repair|fix|coordinate)\b',re.I)
CONTRARY = re.compile(r"\b(?:cannot make|can't make|do not accept|don't accept|not accepting|"
    r"I decline|not (?:an? )?acceptance|only reporting|not agreeing|never agreed|"
    r"never accepted|never said yes|did not agree|didn't agree)\b", re.I)
CANCEL = re.compile(r"\b(?:cancel(?:led)?|withdraw(?:n)?|retract(?:ed)?|revoke(?:d)?)\b", re.I)


def unconditional(text):
    return '?' not in text and not re.search(
        r"\b(?:if|unless|after|might|maybe|do not|don't|not)\b", text, re.I)


def days_in(text):
    return set(re.findall(r'\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b',text.casefold()))


def service_fields(text):
    return {key for key, pattern in SERVICES.items() if re.search(pattern, text, re.I)}


def scope_matches(assignment, service):
    try:
        if service in recipients.FIELDS:
            return recipients.overlaps(assignment,{'kind':'field','value':service})
        fields=service_fields(assignment['information_scope'])
        return service in fields if fields else None
    except ValueError:
        return None


def withdrawn_for(assignment, service, read_actions):
    relevant=scope_matches(assignment,service) is not False
    if assignment.get('status') in {'revoked','suspended','expired'}:
        return relevant
    return any(('*' in r['actions'] or set(r['actions']) & set(read_actions)) and (
        r['scope']['kind']=='all' and relevant or r['scope'].get('value')==service)
        for r in assignment.get('recipient_revocations',[]))


def explicit_people(text, bootstrap, sources):
    """Only literal source references; never infer people from a grant's role."""
    names = recipients.identities(bootstrap, sources)
    found = set(); ambiguous = False
    for name, identities in names.items():
        if re.search(r'(?<!\w)'+re.escape(name)+r'(?!\w)', recipients.norm(text)):
            if len(identities) != 1:
                ambiguous = True
            else:
                found.update(identities)
    return found, ambiguous


def offers(updater, sources, acceptance_id):
    """Closed offer grammar; a worker's assertion creates a candidate, no ACL.

    All candidates remain outstanding until supported cancellation/expiry, an
    explicit replacement/handoff or an already recorded acceptance consumes one.
    Recency is never a tie breaker. Unknown temporal limits keep an offer unusable.
    """
    boot = updater._source_bootstrap(sources)
    known = rows(updater)
    consumed = {(r.get('task') or {}).get('offer', {}).get('source_id') for r in known
        if r['state'] in {'ACCEPTED', 'CANCELLED', 'REVOKED', 'EXPIRED','HANDED_OFF','PENDING_CANCELLATION_RESOLUTION'}
        and r['source_id'] != acceptance_id}
    result = []
    for sid, body in sources.items():
        if sid == acceptance_id:
            break
        text = body['text']; actor = body['source']['participant']
        fields = service_fields(text)
        people, ambiguous = explicit_people(text, boot, sources)
        # A worker can retract its own offer; only the subject's authority can
        # cancel another worker's accepted task. Neither operation grants access.
        if CANCEL.search(text) and unconditional(text):
            eligible=[o for o in result if o['state'] in {'OPEN','CONSUMED','UNRESOLVED_LIMIT'}
                and (not fields or o['service'] in fields) and (not people or o['worker'] in people)
                and (not days_in(text) or bool(days_in(text)&days_in(o['offer']['quote'])))
                and (actor==o['worker'] or actor in boot.authorities.get(o.get('subject'),{}).get('restrict',[])
                    or o.get('subject') is None and o['worker'] in people and re.search(r'\bmy\b',text,re.I))]
            ambiguous_cancel=(len(eligible)>1 and not re.search(r'\b(?:all|both)\b',text,re.I)
                and (not days_in(text) or any(sum(d in days_in(o['offer']['quote']) for o in eligible)>1
                    for d in days_in(text))))
            for prior in result:
                if prior['id'] not in {o['id'] for o in eligible}:continue
                authority = actor in boot.authorities.get(prior.get('subject'), {}).get('restrict', [])
                matches=((not fields or prior['service'] in fields) and (not people or prior['worker'] in people)
                    and (not days_in(text) or bool(days_in(text) & days_in(prior['offer']['quote']))))
                if matches and (actor==prior['worker'] or authority):
                    prior['state']='UNRESOLVED_LIMIT' if ambiguous_cancel else 'CANCELLED'
                elif (matches and prior.get('subject') is None and prior['worker'] in people
                        and re.search(r'\bmy\b',text,re.I)):
                    cancelling_subject=body['source'].get('subject') or actor
                    if actor in boot.authorities.get(cancelling_subject,{}).get('restrict',[]):
                        key='unresolved_for_subjects' if ambiguous_cancel else 'cancelled_for_subjects'
                        prior.setdefault(key,[]).append(cancelling_subject)
        handoff = re.search(r'\b(?:hand(?:ing)? over|handoff|take over)\b', text, re.I)
        if handoff and unconditional(text) and len(people - {actor}) == 1 and len(fields) == 1:
            target = next(iter(people - {actor}))
            predecessors = [o for o in result if o['worker'] == actor and o['service'] in fields
                and o['state'] in {'OPEN','CONSUMED'}]
            if len(predecessors) == 1:
                old = predecessors[0]; old['state'] = 'HANDED_OFF'
                result.append({**deepcopy(old), 'id': 'offer:'+digest([sid,target]), 'worker': target,
                    'offer': recipients.receipt(sid,text,0,len(text)), 'state': 'PENDING_WORKER_RECEIPT',
                    'handoff_from': old['id'], 'original_offer': deepcopy(old['offer'])})
            continue
        if not OFFER.search(text) or len(fields) != 1 or CONTRARY.search(text) or CANCEL.search(text):
            continue
        subject = body['source'].get('subject')
        # A named addressee is stronger than an unaddressed "your" offer.
        addressed = re.match(r'^\s*('+recipients.NAME+r'),', text)
        if addressed:
            try:
                subject, _ = recipients.named(addressed.group(1), recipients.identities(boot,sources))
            except ValueError:
                ambiguous = True
        until = re.search(r'\b(?:expires at|valid until) (\d{4}-\d\d-\d\dT[^\s.,]+)', text, re.I)
        expiry = until.group(1) if until else None
        unknown_condition = bool(re.search(r'\b(?:if|unless|after|once|provided)\b',text,re.I) or
            re.search(r'\b(?:expires|valid until)\b',text,re.I) and not expiry)
        if expiry:
            try:
                ended = instant(updater.core.clock()) is None or instant(expiry) <= instant(updater.core.clock())
            except (ValueError,TypeError):
                ended = True; unknown_condition = True
        else:
            ended = False
        parents = [o for o in result if o['worker']==actor and o['service'] in fields
            and o['state']=='PENDING_WORKER_RECEIPT']
        result.append({'id': 'offer:'+digest(sid), 'worker': actor, 'subject': subject,
            'service': next(iter(fields)), 'offer': recipients.receipt(sid,text,0,len(text)),
            'limits': [recipients.receipt(sid,text,0,len(text))], 'expires_at': expiry,
            'state': 'UNRESOLVED_LIMIT' if ambiguous or unknown_condition else 'EXPIRED' if ended else
                'CONSUMED' if sid in consumed else 'OPEN',
            **({'handoff_from':parents[0]['id'],'original_offer':parents[0]['original_offer']} if len(parents)==1 else {})})
    return result


def resolve(updater, row, sources):
    sid = row['source_id']; body = sources[sid]; actor = body['source']['participant']
    subject = row['original']['subject']; text = row['original']['acceptance']['quote']
    if row['original'].get('model_subject') not in (None,subject):
        return None,'PENDING_SUBJECT_RESOLUTION',[]
    boot = updater._source_bootstrap(sources)
    if actor not in boot.authorities.get(subject, {}).get('permission', []):
        return None, 'PENDING_AUTHORITY_RESOLUTION', []
    confirmation=text.lstrip()
    if not ACCEPT.match(confirmation) and ',' in confirmation:
        prefix,remaining=confirmation.split(',',1)
        addressed,ambiguous=explicit_people(prefix,boot,sources)
        if addressed and not ambiguous:confirmation=remaining.lstrip()
    requested=bool(SERVICE_REQUEST.match(confirmation))
    if not (ACCEPT.match(confirmation) or requested) or '?' in text and not requested or CONTRARY.search(body['text']):
        return None, 'PENDING_SEMANTIC_REASSESSMENT', []
    candidates = [o for o in offers(updater,sources,sid) if o['state'] in {'OPEN','UNRESOLVED_LIMIT'}
        and o['worker'] != actor and o.get('subject') in (None,subject)
        and subject not in o.get('cancelled_for_subjects',[])]
    people, ambiguous = explicit_people(text, boot, sources)
    people.discard(actor)
    if ambiguous:
        return None, 'PENDING_RECIPIENT_RESOLUTION', candidates
    if people:
        candidates = [o for o in candidates if o['worker'] in people]
    fields = service_fields(text)
    if not fields and re.search(r'\bI can make (?:that|it)\b', text, re.I):
        fields.add('scheduling')
    if fields:
        candidates = [o for o in candidates if o['service'] in fields]
    # A named weekday/date can distinguish two offers from the same worker.
    days = re.findall(r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b',text,re.I)
    if days:
        candidates = [o for o in candidates if all(d.casefold() in o['offer']['quote'].casefold() for d in days)]
    if len(candidates) != 1:
        return None, 'PENDING_RECIPIENT_RESOLUTION', candidates
    offer = candidates[0]
    if offer['state']!='OPEN' or subject in offer.get('unresolved_for_subjects',[]):
        return None,'PENDING_EFFECTIVE_CONDITIONS',candidates
    original = row['original']
    if original.get('offer_source_id') and original['offer_source_id'] != offer['offer']['source_id']:
        return None, 'PENDING_TASK_RESOLUTION', candidates
    if original.get('service') and original['service'] != offer['service']:
        return None, 'PENDING_TASK_RESOLUTION', candidates
    task = {'worker': offer['worker'], 'requester': actor, 'beneficiary': subject, 'subject': subject,
        'service': offer['service'], 'offer': deepcopy(offer['offer']),
        'acceptance': deepcopy(original['acceptance']),
        'limits': [*deepcopy(offer['limits']),recipients.receipt(sid,body['text'],0,len(body['text']))],
        'expires_at': offer.get('expires_at'), 'offer_id': offer['id'],
        **{k:offer[k] for k in ('handoff_from','original_offer') if k in offer}}
    # Never recover an earlier acceptance over a later withdrawal, and require a
    # new offer after a withdrawal rather than reviving an old assignment.
    order = list(sources); position = order.index(task['offer']['source_id'])
    for a in updater._state()['assignment'].values():
        if a['participant'] != task['worker'] or a['subject'] != subject:
            continue
        if withdrawn_for(a,task['service'],{'read',*boot.read_pipeline_actions}):
            boundaries = [v for values in a.get('evidence',{}).values() for v in values if v in order]
            boundaries += [r['reference']['source_id'] for r in a.get('recipient_revocations',[]) if r['reference']['source_id'] in order]
            if boundaries and max(order.index(v) for v in boundaries) >= position:
                return None, 'PENDING_NEW_AUTHORIZATION', candidates
    return task, 'REFERENCE_REPAIR_REQUIRED', candidates


def observe(updater, job_id, source_id, raw, sources, reverse, attempt):
    """Save recognition before expansion can reject a grant reference."""
    from .incremental import remap
    value = remap(raw,reverse)
    previous = {r['segment']:r for r in rows(updater,job_id)}
    controls = value.get('c',{})
    if not isinstance(controls,dict):
        return
    actor = sources[source_id]['source']['participant']
    subject = sources[source_id]['source'].get('subject') or actor
    responsive=any(op.get('o')=='service' or op.get('o')=='activity' and op.get('via')=='response'
        for op in value.get('p',[]) if isinstance(op,dict))
    self_request=(not responsive and any(op.get('o')=='activity' and op.get('via')=='request'
        for op in value.get('p',[]) if isinstance(op,dict)) and re.search(
            r'\b(?:I need|I want|I would like|help me|please help|can you|could you)\b',sources[source_id]['text'],re.I))
    for key, control in controls.items():
        if not isinstance(control,dict) or not str(key).isdigit():
            continue
        index = int(key)
        if index not in previous and self_request and control.get('recipient') in (None,actor):
            # Preserve the existing self-service REQUEST convention. It is not
            # a worker acceptance and needs no invented prior worker offer.
            continue
        if (not ('service' in control.get('intents',[]) or responsive and index==0)) or index in previous:
            continue
        from .fact_candidates import segments
        spans=segments(sources[source_id]['text'])
        if not 0<=index<len(spans):
            fail('Service interpretation requires its actual source segment')
        original = {'intent':'SERVICE_ACCEPTANCE','subject':subject,'requester':actor,
            'model_recipient':control.get('recipient'),'existing_grant':control.get('existing'),
            'reason':control.get('r'),'acceptance':recipients.receipt(source_id,sources[source_id]['text'],*spans[index])}
        proposed=next((op for op in value.get('p',[]) if isinstance(op,dict)
            and (op.get('o')=='service' or op.get('o')=='activity' and op.get('via')=='response')),None)
        if proposed:
            original.update(model_service=proposed.get('work'),model_subject=proposed.get('s'),
                model_offer_reference=proposed.get('q'))
            meanings=service_fields(proposed.get('work',''))
            if len(meanings)==1:original['service']=next(iter(meanings))
        row = {'contract_version':1,'id':'acceptance:'+digest([updater.compartment,job_id,index]), 'job_id':job_id,
            'segment':index,'source_id':source_id,'original':original,'state':'PENDING_RECIPIENT_RESOLUTION',
            'history':[],'grant_ids':[], 'adverse_intents':{k:sorted(set(c.get('intents',[])) & {'deny','revoke','erase'})
                for k,c in controls.items() if isinstance(c,dict)}}
        task,status,candidates = resolve(updater,row,sources)
        row.update(task=task,state=status,candidate_offers=candidates)
        if task:
            # Pin semantic context independently from the model's grant pointer.
            original.update(service=task['service'],offer_source_id=task['offer']['source_id'])
        row['history'].append({'attempt':attempt,'outcome':'INITIAL_INTERPRETATION','interpretation':deepcopy(original)})
        save(updater,row)


def context(updater, sources, source_id, job_id):
    current = rows(updater,job_id)
    return {'outstanding_offers':offers(updater,sources,source_id)[-24:],
        'interpretations':current,'repair_required':bool(current)}


def repair_schema(updater, schema, job_id):
    """Only the bounded reference-repair call gains this required outcome."""
    if not rows(updater,job_id):
        return schema
    from .incremental import obj,enum
    schema = deepcopy(schema)
    schema['properties']['intent_repair'] = obj({'outcome':enum([
        'REFERENCE_REPAIR','PENDING_RECIPIENT_RESOLUTION','SEMANTIC_REASSESSMENT']),
        'reason':{'type':'string','minLength':1,'maxLength':320},
        'contrary_evidence':{'type':'array','maxItems':8,'items':obj({
            'source_id':{'type':'string'},'quote':{'type':'string','minLength':1,'maxLength':1000}})}})
    schema['required'].append('intent_repair')
    return schema


def review(updater, job_id, raw, sources, reverse, attempt, required, aliases=None):
    from .incremental import remap
    current = rows(updater,job_id)
    value = remap(raw,reverse)
    proof = value.get('intent_repair')
    if isinstance(proof,dict):
        for item in proof.get('contrary_evidence',[]):
            item['source_id']=(aliases or {}).get(item.get('source_id'),item.get('source_id'))
    for row in current:
        task,status,candidates = resolve(updater,row,sources)
        if row['state'] == 'SEMANTICALLY_REASSESSED':
            continue
        row.update(task=task,state=status,candidate_offers=candidates)
        control = value.get('c',{}).get(str(row['segment']),{})
        for key, adverse in row['adverse_intents'].items():
            if not set(adverse) <= set(value.get('c',{}).get(key,{}).get('intents',[])):
                save(updater,row);fail('Service repair cannot drop an independent adverse intent')
        if required:
            if not isinstance(proof,dict) or proof.get('outcome') not in {
                    'REFERENCE_REPAIR','PENDING_RECIPIENT_RESOLUTION','SEMANTIC_REASSESSMENT'}:
                save(updater,row);fail('Repair must explicitly preserve or reassess the original service acceptance')
            if proof['outcome'] == 'SEMANTIC_REASSESSMENT':
                evidence = proof.get('contrary_evidence',[])
                valid = bool(evidence)
                for item in evidence:
                    body = sources.get(item.get('source_id'),{})
                    quote = item.get('quote','')
                    valid = valid and bool(quote and quote in body.get('text','') and
                        item.get('source_id')==row['source_id'] and quote in row['original']['acceptance']['quote'] and
                        body.get('source',{}).get('participant') == row['original']['requester'] and CONTRARY.search(quote))
                if not valid or not proof.get('reason') or 'service' in control.get('intents',[]):
                    save(updater,row);fail('Semantic reassessment needs contrary authenticated source evidence and a replacement interpretation')
                row.update(state='SEMANTICALLY_REASSESSED',reassessment={**deepcopy(proof),
                    'original':deepcopy(row['original']),'replacement_intent':'INFORMATIONAL_STATEMENT'})
                row['history'].append({'attempt':attempt,**deepcopy(row['reassessment'])})
                save(updater,row);continue
            row['history'].append({'attempt':attempt,**deepcopy(proof)})
            if proof['outcome']=='PENDING_RECIPIENT_RESOLUTION':
                row['state']=status if task is None else 'PENDING_REFERENCE_REPAIR'
                save(updater,row);fail('Service repair explicitly retains pending resolution; no grant may publish')
        if 'service' not in control.get('intents',[]):
            save(updater,row);fail('Reference repair cannot silently turn SERVICE_ACCEPTANCE into INFORMATIONAL_STATEMENT')
        if task is None:
            save(updater,row);fail('Service acceptance is durable but its task/recipient/authority remains unresolved')
        recipient = control.get('recipient')
        if recipient is not None and recipient != task['worker']:
            save(updater,row);fail('Service recipient contradicts the source-grounded outstanding offer')
        save(updater,row)


def reassessed_grant(row, op):
    if op.get('op')!='propose_assignment' or op.get('subject')!=row['original']['subject']:
        return False
    related={row['original'].get('model_recipient'),row['original']['requester']}
    if row.get('task'):related.add(row['task']['worker'])
    return op.get('participant') in related or row['original'].get('model_recipient') is None


def canonical_guard(updater, operations, state, sources, source_id):
    """Repeat the task invariant at the final canonical publication boundary."""
    if not source_id:return
    proposed={op.get('id'):op for op in operations if isinstance(op,dict) and op.get('op')=='propose_assignment'}
    for row in rows(updater,updater._job_id(source_id)):
        if row['state']=='SEMANTICALLY_REASSESSED':
            if any(reassessed_grant(row,op) for op in proposed.values()):
                fail('A rejected service interpretation cannot be replaced by compensating access')
            continue
        if row['state'] not in {'REFERENCE_REPAIR_REQUIRED','ACCEPTED'} or not row.get('grant_ids'):
            fail('Canonical validation cannot drop or publish a pending service interpretation')
        task,status,_=resolve(updater,row,sources)
        if not task:fail('Original task evidence or authorization no longer supports acceptance')
        for aid in row['grant_ids']:
            assignment=proposed.get(aid,state['assignment'].get(aid,{}))
            if (assignment.get('participant')!=task['worker'] or assignment.get('subject')!=task['subject']
                    or assignment.get('status')!='active' or scope_matches(assignment,task['service']) is not True):
                fail('Every accepted-task grant must retain the resolved worker, subject and service')
            if aid in proposed:
                evidence={sid for values in assignment.get('evidence',{}).values()
                    if isinstance(values,list) for sid in values}
                if not {source_id,task['offer']['source_id']}<=evidence:
                    fail('New task access requires original offer and current acceptance evidence')


def validate_controls(updater, job_id, wire, state, sources, payload):
    """An unrelated grant can never discharge an accepted worker's effect."""
    source_id=payload['new_source_id']
    aliases={f's{i}':s['id'] for i,s in enumerate(payload['sources'],1)}
    for row in rows(updater,job_id):
        if row['state'] == 'SEMANTICALLY_REASSESSED':
            if any(op.get('_accepted_service') or reassessed_grant(row,op) for op in wire['permission_changes']):
                fail('A rejected interpretation cannot manufacture a service grant')
            continue
        task,status,_ = resolve(updater,row,sources)
        if not task:
            fail('Unresolved service acceptance cannot publish a grant')
        worker = task['worker']; subject = task['subject']; receipt = task['offer']['source_id']
        matches = [];created=[]
        for aid,a in state['assignment'].items():
            if a['participant'] != worker or a['subject'] != subject or a['status'] != 'active':
                continue
            if withdrawn_for(a,task['service'],{'read',*updater.bootstrap.read_pipeline_actions}):
                continue
            if not gate(updater.core,updater.compartment,a):
                continue
            from .permissions import _valid_now
            if _valid_now(updater.core,a.get('validity',{})) is not True:
                continue
            if scope_matches(a,task['service']) is not True:
                continue
            accepted=a.get('accepted_service',{})
            if accepted and accepted.get('worker_source_id')!=receipt:
                continue
            matches.append(aid)
        for op in wire['permission_changes']:
            if op.get('op') != 'propose_assignment' or op.get('participant') != worker:
                continue
            service = op.get('_accepted_service',{})
            if (op.get('subject') != subject or service.get('worker') != worker or
                    aliases.get(service.get('worker_source_id'),service.get('worker_source_id')) != receipt or
                    aliases.get(service.get('subject_source_id'),service.get('subject_source_id')) != source_id):
                fail('Accepted worker access must preserve the original subject, offer, acceptance and service receipts')
            if task['service'] not in service_fields(service.get('task_summary','')):
                fail('Reference repair cannot replace the intended service')
            matches.append(op['id']);created.append(op['id'])
        decision = next((d for d in wire['control_review'] if d.get('index') == row['segment']),{})
        existing = decision.get('existing')
        if existing is not None and existing not in matches:
            fail('The referenced grant belongs to a different recipient, subject, service or effective condition')
        selected=list(dict.fromkeys([*created,*([existing] if existing in matches else [])]))
        if not selected:
            fail('The preserved service acceptance requires that actual worker own matching grant')
        row.update(task=task,grant_ids=selected,state='REFERENCE_REPAIR_REQUIRED')
        save(updater,row)


def publish(updater, job_id, validated):
    prior_ids=set(updater._state()['assignment'])
    for row in rows(updater,job_id):
        if row['state'] == 'SEMANTICALLY_REASSESSED':
            continue
        if not row.get('task') or not row.get('grant_ids'):
            fail('Pending task cannot become accepted through publication')
        for aid in row['grant_ids']:
            assignment = validated[0]['assignment'].get(aid)
            if not assignment or assignment['status'] != 'active':
                fail('Task publication requires the validated current recipient grant')
            if aid not in prior_ids and ('assignment',aid) in validated[1] and assignment.get('accepted_service'):
                assignment['accepted_task_v19'] = row['id']
        row['state'] = 'ACCEPTED';save(updater,row)


def synchronize(updater, sources):
    """Apply only explicit, authenticated terminal evidence; never reactivate."""
    boot = updater._source_bootstrap(sources)
    order = list(sources)
    state = updater._state()
    current=rows(updater);cancellation_basis=deepcopy(current)
    def cancellation_matches(candidate, source):
        task=candidate['task'];text=source['text'];actor=source['source']['participant']
        people,_=explicit_people(text,boot,sources)
        return (candidate['state']=='ACCEPTED' and task['service'] in service_fields(text)
            and source['source'].get('subject') in (None,task['subject'])
            and (task['worker'] in people or actor==task['worker'])
            and (actor==task['worker'] or actor in boot.authorities.get(task['subject'],{}).get('restrict',[]))
            and (not days_in(text) or bool(days_in(text) & days_in(task['offer']['quote']))))
    for row in current:
        if row['state'] != 'ACCEPTED':
            continue
        task = row['task']
        grants = [state['assignment'].get(aid,{}) for aid in row['grant_ids']]
        if grants and all(a.get('status') != 'active' or withdrawn_for(a,task['service'],
                {'read',*boot.read_pipeline_actions}) for a in grants):
            row['state'] = 'REVOKED'
        elif task.get('expires_at') and (instant(updater.core.clock()) is None or
                instant(task['expires_at']) <= instant(updater.core.clock())):
            row['state'] = 'EXPIRED'
        if row['source_id'] in order:
            for sid in order[order.index(row['source_id'])+1:]:
                source = sources[sid]; actor = source['source']['participant']; text = source['text']
                people,_ = explicit_people(text,boot,sources)
                fields = service_fields(text)
                if CANCEL.search(text) and unconditional(text) and cancellation_matches(row,source):
                    candidates=[c for c in cancellation_basis if cancellation_matches(c,source)]
                    ambiguous=(len(candidates)>1 and not re.search(r'\b(?:all|both)\b',text,re.I)
                        and (not days_in(text) or any(sum(d in days_in(c['task']['offer']['quote'])
                            for c in candidates)>1 for d in days_in(text))))
                    terminal=('PENDING_CANCELLATION_RESOLUTION' if ambiguous else
                        'REVOKED' if re.search(r'\brevoke',text,re.I) else 'CANCELLED')
                    row.update(state=terminal,
                        terminal_evidence=recipients.receipt(sid,text,0,len(text)))
                if (unconditional(text) and actor==task['worker'] and task['service'] in fields and len(people-{actor})==1
                        and re.search(r'\b(?:hand(?:ing)? over|handoff|take over)\b',text,re.I)):
                    row.update(state='HANDED_OFF',terminal_evidence=recipients.receipt(sid,text,0,len(text)))
        save(updater,row)


def gate(core, compartment, assignment):
    task_id = assignment.get('accepted_task_v19')
    if not task_id:
        return True
    for record, in core.db.execute('SELECT body FROM permission_intents_v19 WHERE compartment=?',(compartment,)):
        row=json.loads(record)
        if row['id']!=task_id:
            continue
        task=row.get('task') or {}
        if (row['state']!='ACCEPTED' or task.get('worker')!=assignment.get('participant')
                or task.get('subject')!=assignment.get('subject')):
            return False
        if task.get('expires_at'):
            now=instant(core.clock())
            return now is not None and now<instant(task['expires_at'])
        return True
    return False
