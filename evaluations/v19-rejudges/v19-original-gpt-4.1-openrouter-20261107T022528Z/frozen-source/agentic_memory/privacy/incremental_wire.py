"""Compact model output; expansion changes representation, never decisions.

Facts select host-enumerated source segments. Attribution cites an exact source
event; the host retains that event's original bytes as evidence, not a model copy.
"""
from copy import deepcopy
import json


def self_activity_scope(activity, *, continuing=False):
    if continuing:
        return ('Accepted SELF service interaction established by its original authorization receipts. '
            'Permits this subject\'s own '
            'responsive facts, results, instructions, resulting recommendations and continuing plan '
            'for the SAME accepted service interaction. The activity label summarizes its initial '
            'question; it is NOT a closed list of information fields. An initial history/status '
            'question does not exclude the same worker\'s later recommendations or follow-up '
            'instructions for that subject within that service. Use the original acceptance and '
            'worker receipts to establish continuity. Explicit source field exclusions still '
            'constrain access. Excludes unrelated service interactions and other subjects\' information.')
    return ("The subject's own responsive facts, instructions, results, current plan and updates for: "+activity+
        '. Includes continuing requirements, exceptions, cancellations and necessary operational follow-through '
        'within this activity only. Excludes unrelated activities and other subjects information.')


def compact_operation_variants(variants):
    """Deduplicate wire shapes; canonical validation still checks linked IDs.

    Cross-field authority/receipt constraints belong to the canonical validator.
    Repeating an entire operation schema for each possible ID combination costs
    thousands of prompt tokens without adding an authorization guarantee.
    """
    references={'s','p','e','q','g','rid','hold','erasure_id','i','wi'}
    groups={}
    for variant in variants:
        shape=deepcopy(variant)
        for key,prop in shape.get('properties',{}).items():
            if key in references and 'enum' in prop:prop['enum']=[]
        fingerprint=json.dumps(shape,sort_keys=True,separators=(',',':'))
        if fingerprint not in groups:
            groups[fingerprint]=deepcopy(variant)
            continue
        for key,prop in variant.get('properties',{}).items():
            if key in references and 'enum' in prop:
                target=groups[fingerprint]['properties'][key]['enum']
                target.extend(value for value in prop['enum'] if value not in target)
    return list(groups.values())

KEYS={'op':'o','id':'i','participant':'p','subject':'s','actions':'a',
    'information_scope':'scope','authority_source':'e','evidence':'ev','reason':'r',
    'blocked_assignments':'blocked','activity_id':'task','requester':'rq',
    'recipient_access':'recipients','status':'st','corroboration':'cor','performance':'perf',
    'validity':'v','effective_until':'until','effective_from':'from','end_activity':'end',
    'supersedes':'sup','restriction_id':'rid','participants':'ps','effect':'effect',
    'unresolved':'pending','activity':'task_text','assignment_id':'g','decision':'d',
    'candidate_ids':'facts','hold_id':'hold'}
OPS={'propose_assignment':'grant','observe_participation':'observe',
    'establish_activity':'accept','record_restriction':'deny','replace_restriction':'replace_deny',
    'resolve_restriction':'resolve','revoke_assignment':'revoke','suspend_assignment':'suspend',
    'schedule_expiry':'expire','end_activity':'end','erase_information':'erase',
    'erase_candidates':'erase_facts','resolve_control_hold':'resolve_hold'}
BIND={'candidate_id':'f','assignment_id':'g','in_scope':'ok','subject_supported':'who',
    'blocked_by':'deny','reason':'r'}
GROUNDED_INSTRUCTION='''
WIRE:
c: one item for EVERY current control segment IN ORDER: 0 means reviewed, no
unexpressed unresolved access limit; {i:index,s:"unresolved_limit",r:reason} means
an actual possible adverse control cannot be interpreted. p contains ALL changes;
do not duplicate operation indexes or effect categories in c. p=[] is no change.
Use c=0 ALSO when p fully expresses a clear grant, denial, revocation, expiry or
erasure. It does NOT mean permission remains unchanged. Do not invent an
unresolved_limit merely because the host has not yet validated/published your
proposed change; that is the host's next step, not ambiguity in the source.
p: operations using these abbreviations: '''+str(KEYS)+'''.
Names: fields, activity, observe, deny, replace_deny, resolve, revoke, suspend, expire, end,
erase, erase_facts, resolve_hold. Only the supplied variants are available.
For an authenticated subject actually seeking/accepting help, use activity.
An unaddressed request for own particular help uses
{o:"activity",via:"request",i:new or existing OWN grant ID,s:subject,e:CURRENT
subject statement,work:actual activity,r:reason,v:{}}. It selects the configured
self-activity rule for responsive facts/instructions/results/plan and operational
follow-through. Mere ownership/assertion grants nothing. Reuse i only when its
actual scope covers this activity; keep its original lifetime unchanged.
For the subject accepting a worker's offer, answering their service question,
returning their requested callback or otherwise participating in that service,
use {o:"activity",via:"response",s:subject,e:CURRENT subject receipt,q:the actual
worker's earlier source,p:THAT worker,i:new or existing OWN grant ID,wi:NEW worker
grant ID,work:actual accepted service,r:reason,v:subject lifetime,wv:worker lifetime}.
This one MODEL-SELECTED accepted interaction explicitly selects access for BOTH
recipients: self activity for the subject and only necessary service information
for the actual worker. Do not activate self access on this acceptance while
withholding the worker grant because that SAME acceptance is supposedly missing.
A callback response needs no literal yes. Self and worker lifetimes are separate;
review each independently. Original exclusions constrain every worker field, which
still needs a separate focused scope review before publication. The worker's scope
includes necessary responsive information and the SUBJECT-PROVIDED contact and
communication instructions needed for that SAME service, not unrelated finances,
credentials, other activities or all of the subject's information. A helper's own
status update about a customer is not the helper seeking help for their own data.
For literal closed field permission, use fields below. If the worker is already
covered by that field grant and only the subject needs activity access, request
mode can select the subject's own activity separately; never broaden the field grant.
For literal permission to a recipient, use
{o:"fields",i:new grant ID,p:recipient,s:subject,e:CURRENT subject statement,
quote:EXACT supplied authorization statement,r:reason,v:{}}.
Select the complete allowed field set and its limits from the supplied quote
options. Do not add an inferred event subtype, location, title, date range or
activity limit that the subject did not state. The selected text is preserved
as the scope. Its actual exclusions and lifetime still apply. Each fact may
contain ANY SUBSET of the authorized fields: time alone need not include parking.
This form requires an actual permission statement; a bare factual reply is not
literal read consent. An accepted worker interaction uses activity via=response instead.
The response q must be authored by THAT worker, not someone with a similar job.
Returning a requested callback or supplying the requested information can accept
that actual service without the literal word yes. Assess self and worker
separately. A greeter confirming arrival does not gain unrelated repair findings.
An accepted service does not manufacture a task record. Reuse existing scopes
only when their actual permitted fields cover this new information.
observe saves a genuine worker offer with i,p,s,task_text,r; no read access.
deny uses i,s,ps:[affected recipients],scope,r,e; default read/search/derive.
deny is a FIELD-SPECIFIC prohibition, with blocked=[]; it never implicitly
suspends an entire grant. Review its scope against each fact in b. A prohibited
price does not suspend the same recipient's permitted time/location grant.
Use a separate explicit revoke/suspend ONLY when authority actually withdraws
that entire grant's information scope. A field denial is not such a withdrawal.
An exclusion about ONE recipient never denies the worker or subject. For example,
"Jo may know the time but not the price" grants Jo time, denies Jo price only.
Revocation names the actual existing grant g. A channel-specific communication
instruction is a factual instruction; it does not withdraw unrelated read grants.
Optional ev/perf/cor are arrays of source refs; omit unused fields, never null.
v optionally states supported expiry; do not invent expiry from ordinary dates.
For every grant explicitly supply v={} if no access expiry was stated, otherwise
v:{until:"ISO timestamp"}. This expires READING, not the dates of events readable
before expiry. An access window through Friday can include a later meeting date.

f: EVERY requested segment independently. {i:index,k:context|question} for pure
nonfacts. Facts/instructions: {i,k:asserted_fact|instruction,s:subject,r:attribution,
x:[]} with optional e:source (defaults to own segment), t:topics. Optional x holds
{k:replaces|cancels,f:old fact ID} only for explicit same-property corrections.
A matching-value confirmation or shorter restatement is NOT a correction: x=[].
Never retire a fuller compound fact's date/cancellation/exception just because a
later summary omits it. "Current" alone supplies no replacement evidence.
An operational preference can be a fact AND access evidence. Preserve instructions
and contact rules as facts. Do not copy fact text. Do not turn offers into facts.

b: recipient binding decisions {f:fact ID,g:grant ID,d:allow|outside|unattributed|
unattributed_in_scope,r:reason,optional deny:[applicable restriction IDs]}.
For this grounded wire emit q FIRST: the exact first 80 characters of that selected
fact's quote (the whole quote when shorter), then f,g,r,d. The host checks q against
f. Explain the scope match before choosing the decision. An exclusion reason
requires d=outside. Never infer a fact's text from an ID or another nearby field.
Review required_pairs, plus each new FACT segment under applicable existing/new
grants and supplied historical facts for newly proposed grants. Use segments[].
candidate_id for NEW facts, not an older fact ID. Match the subject. Do not review
unrelated old pairs, nonfacts, or nonexistent grants. With no assignments or new
grants b MUST be []. allow requires the WHOLE fact to fit the precise scope.
Prioritize new facts under the most relevant grants for the SAME subject, then
the supplied required_pairs and historical facts for new grants. The router only
prioritizes work; independently check exact scope/authority and use outside when
it does not fit. On repair use required_pairs; do not repeat accepted reviews.
The TOTAL b budget is 12 entries. When there are more pairs, use required_pairs
first, then new facts under relevant grants, then new grants' historical pairs;
defer overflow. Keep each reason under 60 characters. Do not repeatedly regrant
fields already covered by this recipient's active scope.
When binding_review_scope is own_subject_only, override the general b instruction:
assess ONLY grants with participant=subject in this MAIN response. For a new
activity only its own i can appear in b, not the worker wi. Do not output other
recipients' preliminary opinions, including outside decisions; their complete
field checks happen in the separate focused call. b=[] when no own grant applies.

Examples of the update convention (NOT evidence):
* A first message merely states "My meeting is Monday. My budget is 42000."
  Store both as protected facts; c=[0,0],p=[],b=[]. Mere assertion/ownership is not
  an activity request, acceptance or explicit authorization. Do not invent one.
* A helper asks "May I coordinate your meeting?" c=[0],p=[] (or save a genuine
  worker offer as observe), f=question, b=[] if no pending pairs. Missing consent
  is DEFINITE no grant, not an unresolved_limit and not a possible revocation.
* Worker asks an owner for delivery requirements. Owner replies "Bring the parcel
  before Friday; it is fragile." Select self for the owner's delivery activity,
  using activity via=response to select both the subject and the actual delivery worker,
  citing the owner reply and that worker's prior question.
  f retains both instructions. No task ceremony or literal self-authorization needed.
* Owner confirms seeking repair for a rattling machine: propose a narrow explicit
  self grant for that repair's symptoms, findings and responsive repair instructions.
  Do not extend to another person's equipment or unrelated financial details.
* Worker supplies new repair instructions: c=[0], p=[], f=instruction; bind each
  whole instruction under existing applicable grants. "Do not run the motor" is
  an instruction about the motor, never a restriction on reading the instruction.
* Owner says Jo may read time/place only: grant those fields to Jo; deny only
  explicitly excluded fields for Jo. This says nothing about other recipients.
* Owner clearly says the helper's access is revoked: c=[0], p contains revoke
  naming that helper's existing grant and e:CURRENT owner statement. Keep the
  owner's and other recipients' grants. Clear revocation is not unresolved_limit.
'''
INSTRUCTION='''\nOUTPUT WIRE (keys in input state remain descriptive):
c=control_review, p=permission_changes, f=segment classifications, b=binding_reviews.
c has one item per current control segment, in order: integer 0 means no change;
otherwise use {i:segment index,s:changed|unresolved_limit,k:[grant|deny|task|erase_or_expire],o:[operation indexes],r:short reason}.
erase_or_expire means actual source erasure or permission expiry, NOT factual revisions.
Each declared effect needs the corresponding operation; a grant cannot stand in
for a denial. Include all effects when a segment both allows and prohibits.
unresolved_limit means a possible restriction/revocation/erasure affecting existing
access cannot be interpreted. An unaccepted offer or missing consent is NOT such
a limit: record an observe operation with tentative status, without granting access.
f items {i:segment index,k:context|question} classify nonfacts without extra fields.
Factual f items use {i,k:asserted_fact|instruction,s:subject,e:source reference,r:short
attribution reason}; optional t=topics and x=[{k:replaces|cancels,f:target fact}].
x is normally []. A new topic, added detail, identity or symptom does NOT replace
an unrelated older fact. Only an explicit correction/cancellation of the SAME
property of the SAME subject can name a target. Cross-subject edges are invalid.
Select evidence e from supplied sources/fact_sources; the host preserves exact
source bytes. Do not echo quotes, quantities or original text in your response.
b items {f:fact,g:grant,d:allow|outside|unattributed|unattributed_in_scope,r:reason,optional deny:[restriction IDs]}.
For new facts use segments[].candidate_id and available_assignments. Old index
facts are not substitutes for a new segment's candidate ID. A factual replacement
changes f.x ONLY: use c=0,p=[] when access rules have not changed, and assess the
new fact under the existing grant. Do not use replace_deny, erase or end for a
changed factual value. deny lists actual prohibiting access rules, never factual
replacement/cancellation relations. An unrelated restriction does not prohibit a fact.
Operation names: grant,observe,accept,deny,replace_deny,resolve,revoke,suspend,
expire,end,erase,erase_facts,resolve_hold. resolve_hold uses hold,e,r to explicitly
resolve an earlier quarantined interpretation using a later authority statement.
Operation field abbreviations: '''+str(KEYS)+'''.
Omit redundant fields: grants are active and default to mediated read; denials
default to read/search/derive. Give a only for a different explicitly supported
action set. e defaults to the current event; status/effect are supplied by the
host. Omit ev unless additional source evidence beyond e/perf/cor is necessary.
For accept of a saved task, the host retains its
worker, requester and task text; supply i,s,r and e only if acceptance is older.
New tasks still need their actual worker/task text/performance receipts. A
missing recipient assessment is unresolved, never allowed. f.e defaults to that
segment's own source. b uses d=allow|outside|unattributed|unattributed_in_scope
instead of ok/who; optional deny lists actual prohibiting restrictions.
Use one narrow grant for one recipient's complete allowed field set, not one grant
per field. A denial NEVER implies a grant. Scope must state actual permitted fields,
not a broader topic tag. Prefer explicit grants when the owner explicitly grants
access. Task-derived grants need an existing or same-response accepted activity;
accept a saved tentative task by its EXISTING id. Emit the acceptance before its
task grant. A question/offer is context, not a factual instruction. Permission and
prohibition statements are context, not task facts. Keep each reason one clause.

Decision examples (illustrative identifiers only; use this input's actual IDs):
* An authenticated worker offers to collect a parcel for its owner. Output c
  changed/task referencing an observe operation, with that worker, owner and task
  text. f is context. NO grant. This is a successful tentative-task update, NEVER
  unresolved_limit. observe.authority is the accepting owner, not the worker.
* The owner replies yes to a SAVED task: accept that task's EXISTING ID. For a NEW
  accepted task include its worker, task_text and perf receipt(s); missing task
  fields cannot be invented by the host. An explicit owner read permission can
  instead be an explicit grant; it does not require inventing an accepted task.
* An owner permits a helper to read delivery date and address, excluding cost:
  grant the precise allowed fields, deny cost, f=context, then b reviews each
  supplied historical candidate against that NEW grant in this SAME response.
  Include outside decisions too. Protected facts do not need to be resent.
* A helper reports a changed delivery time: c=[0], p=[], f=asserted_fact with x
  replacing the earlier fact, b=allow under the EXISTING applicable grant.
* Someone claims a powerful role or asks for unavailable permission: c=[0],p=[],
  f=context. Their lack of authority is a definite denial of that REQUEST; it is
  NOT an unresolved restriction on anyone else's existing access.
* An owner says to stop the helper reading: c changed/deny and revoke the actual
  grant. If scope is truly ambiguous, use unresolved_limit. Do not resolve old
  holds unless this CURRENT authority statement actually clarifies their failed
  source. A past grant or another person's claim cannot resolve a hold.
'''


def map_operation(value, *, decode=False):
    keys={v:k for k,v in KEYS.items()} if decode else KEYS
    ops={v:k for k,v in OPS.items()} if decode else OPS
    if isinstance(value,list):return [map_operation(v,decode=decode) for v in value]
    if not isinstance(value,dict):return value
    result={keys.get(k,k):map_operation(v,decode=decode) for k,v in value.items()}
    field='op' if decode else 'o'
    if field in result:result[field]=ops.get(result[field],result[field])
    return result


def operation_schema(schema):
    if isinstance(schema,list):return [operation_schema(v) for v in schema]
    if not isinstance(schema,dict):return schema
    result={}
    for k,v in schema.items():
        if k=='properties':
            result[k]={KEYS.get(name,name):operation_schema(body) for name,body in v.items()}
            op=result[k].get('o')
            if op:op['enum']=[OPS.get(name,name) for name in op['enum']]
        elif k=='required':result[k]=[KEYS.get(name,name) for name in v]
        else:result[k]=operation_schema(v)
    if result.get('type') in ('string',['string','null']) and 'enum' not in result:
        result['maxLength']=min(result.get('maxLength',240),240)
    return result

ATTRIBUTION_FIELDS=('information_subjects','attribution_complete','action_actors','mentioned_people',
    'care_participants','information_types')


def contract(query, schema):
    """Expose a small output grammar while retaining the full semantic input."""
    from .incremental import obj,enum,fail
    query=deepcopy(query)
    grounded=int(query.get('permission_version','15').split('-',1)[0])>=16
    selective=int(query.get('permission_version','15').split('-',1)[0])>=17
    compact=int(query.get('permission_version','15').split('-',1)[0])>=18
    disclosed=str(query.get('permission_version','')).split('-',1)[0]=='19'
    maintenance=set(schema['properties'])=={'binding_reviews'}
    allocation=deepcopy(query.get('new_ids',{}))
    original_binding=schema['properties']['binding_reviews']
    bp=original_binding['items']['properties']
    bi=obj({'f':deepcopy(bp['candidate_id']),'g':deepcopy(bp['assignment_id']),
        'd':enum(['allow','outside','unattributed','unattributed_in_scope']),
        'deny':deepcopy(bp['blocked_by']),'r':{'type':'string','minLength':1,'maxLength':120}})
    bi['required']=['f','g','d','r']
    quotes={p['id']:p['quote'][:80] for p in query.get('candidates',[])}
    quotes.update({s['candidate_id']:s['quote'][:80] for s in query.get('segments',[])})
    if grounded:
        bi['properties']={'q':({'type':'string','enum':list(dict.fromkeys(quotes.values()))}
            if quotes else {'type':'string'}),
            **{k:bi['properties'][k] for k in ('f','g','r','d','deny')}}
        bi['required']=['q','f','g','r','d']
        bi['properties']['f']=enum(quotes) if quotes else {'type':'string'}
    binding={'type':'array','maxItems':original_binding.get('maxItems',12),'items':bi}
    indexed_pairs=None
    if maintenance:
        if grounded and 'pair_context' in query:
            indexed_pairs=query['pair_context']
            slots={}
            for i,pair in enumerate(indexed_pairs):
                item=obj({'r':{'type':'string','minLength':1,'maxLength':80},
                    'd':enum(['allow','outside','unattributed','unattributed_in_scope']),
                    'deny':deepcopy(bp['blocked_by'])})
                item['required']=['r','d'];slots[str(i)]=item
            compact_schema=obj({'b':obj(slots)})
        else:compact_schema=obj({'b':binding})
    else:
        segment=schema['properties']['segments']['items']['properties']
        relation=obj({'k':enum(['replaces','cancels']),
            'f':deepcopy(segment['relationships']['items']['properties']['target_id'])})
        fact_props={'i':deepcopy(segment['index']),'k':enum(['asserted_fact','instruction']),
            's':deepcopy(segment['subject']),'e':deepcopy(segment['subject_source']),
            'r':{'type':'string','minLength':1,'maxLength':120},
            't':deepcopy(segment['topics']),'x':{'type':'array','maxItems':8,'items':relation}}
        fact=obj(fact_props);fact['required']=['i','k','s','r','x']
        fact['properties']['x']['maxItems']=segment['relationships'].get('maxItems',8)
        context=obj({'i':deepcopy(segment['index']),'k':enum(['question','context'])})
        control=schema['properties']['control_review']['items']['properties']
        change=obj({'i':deepcopy(control['index']),'s':enum(['changed','unresolved_limit']),
            'k':{'type':'array','minItems':1,'maxItems':4,'items':enum(['grant','deny','task','erase_or_expire'])},
            'o':deepcopy(control['operations']),'r':{'type':'string','minLength':1,'maxLength':120}})
        compact_schema=obj({'c':{'type':'array','items':{'anyOf':[{'type':'integer','enum':[0]},change]}},
            'p':operation_schema(schema['properties']['permission_changes']),
            'f':{'type':'array','items':{'anyOf':[context,fact]}},'b':binding})
        if grounded:
            compact_schema['properties']={'review':{'type':'string','minLength':1,'maxLength':320},
                **compact_schema['properties']}
            compact_schema['required']=['review',*compact_schema['required']]
            # The operation array already expresses the complete proposed access
            # transition. Do not ask the model to restate its effects and indexes
            # in a second representation. Independent per-segment review retains
            # an explicit unresolved limit, which still blocks publication.
            unresolved=obj({'i':deepcopy(control['index']),
                's':enum(['unresolved_limit']),'r':{'type':'string','minLength':1,'maxLength':120}})
            compact_schema['properties']['c']['items']={'anyOf':[{'type':'integer','enum':[0]},unresolved]}
            if selective:
                intent_schema=obj({
                    'intents':{'type':'array','minItems':1,'maxItems':4,'uniqueItems':True,
                        'items':enum(['none','service','grant','deny','revoke','communication','erase','unresolved'])},
                    'recipient':enum(query['bootstrap']['participants'],True),
                    'existing':enum(query['permission_state']['assignment'],True),
                    'r':{'type':'string','minLength':1,'maxLength':120}})
                compact_schema['properties']['c']=obj({str(i):deepcopy(intent_schema)
                    for i in range(len(query['control_segments']))})
            variants=compact_schema['properties']['p']['items']['anyOf']
            compact_schema['properties']['p']['items']['anyOf']=[v for v in variants
                if v['properties']['o']['enum'][0]!='accept' and not
                (v['properties']['o']['enum'][0]=='grant' and v['properties'].get('basis',{}).get('enum')==['task'])]
        count=len(query['control_segments'])
        if not selective:
            compact_schema['properties']['c']['maxItems']=count
            if schema['properties']['permission_changes'].get('maxItems')!=0:
                compact_schema['properties']['c']['minItems']=count
        compact_schema['properties']['f']['maxItems']=len(query['segments'])
        if not query['segments']:compact_schema['properties']['f']['maxItems']=0
        else:
            indexes=[s['index'] for s in query['segments']]
            for variant in compact_schema['properties']['f']['items']['anyOf']:
                variant['properties']['i']['enum']=indexes
        variants=compact_schema['properties']['p']['items']['anyOf']
        for variant in variants:
            props=variant['properties'];op=props['o']['enum'][0]
            for field in ('st','effect'):
                props.pop(field,None)
                variant['required']=[k for k in variant['required'] if k!=field]
            props['r']['maxLength']=120
            if grounded and op in {'deny','replace_deny'} and 'blocked' in props:
                props['blocked']={'type':'array','maxItems':0,'items':{'type':'string'}}
            if grounded and op=='deny' and 'sup' in props:
                prior=list(query['permission_state']['restriction'])
                if selective:props['sup']=enum(prior,True)
                elif prior:props['sup']=enum(prior)
                else:
                    props.pop('sup')
                    variant['required']=[k for k in variant['required'] if k!='sup']
            for field in ('a','e','ev'):
                variant['required']=[k for k in variant['required'] if k!=field]
            if grounded and op=='grant':
                props['via']=enum(['explicit','self_request','response'])
                props['q']=enum([s['id'] for s in query['sources']])
                variant['required'].append('via')
                variant['required'].append('v')
            if op=='observe':
                props.pop('basis',None)
                variant['required']=[k for k in variant['required'] if k not in {'basis','rq'}]
            if op=='accept' and props.get('basis',{}).get('enum')==['corroborated']:
                props.pop('basis',None);props.pop('authority',None)
                variant['required']=[k for k in variant['required'] if k not in
                    {'basis','p','rq','task_text','cor','recipients'}]
                if int(query.get('permission_version','15').split('-',1)[0])>=16:
                    variant['required'].append('recipients')
        if grounded:
            # Reference grammar reflects authenticated capabilities, not source
            # meaning. The model still chooses WHETHER a receipt authorizes any
            # access and its exact scope; an unauthenticated/unauthorized speaker
            # can never be selected as somebody else's authority receipt.
            grounded_variants=[]
            for variant in compact_schema['properties']['p']['items']['anyOf']:
                props=variant['properties'];name=props['o']['enum'][0]
                capability=('permission' if name=='grant' else 'restrict' if name in
                    {'deny','replace_deny','resolve','revoke','erase','erase_facts'} else None)
                if capability and 's' in props:
                    for subject,caps in query['bootstrap']['authorities'].items():
                        receipts=[s['id'] for s in query['sources']
                            if s['source']['participant'] in caps.get(capability,[])
                            and (not compact and name!='grant' or s['id']==query['new_source_id'])]
                        if not receipts:continue
                        grounded_variant=deepcopy(variant)
                        grounded_variant['properties']['s']=enum([subject])
                        grounded_variant['properties']['e']=enum(receipts)
                        if 'e' not in grounded_variant['required']:grounded_variant['required'].append('e')
                        if name!='grant':
                            grounded_variants.append(grounded_variant)
                            continue
                        current=next(s for s in query['sources'] if s['id']==query['new_source_id'])
                        authorization_quotes=list(dict.fromkeys([current['text'],
                            *[s['quote'] for s in query['control_segments']]]))
                        field_props={'o':enum(['fields']),'i':deepcopy(props['i']),
                            'p':deepcopy(props['p']),'s':enum([subject]),'e':enum(receipts),
                            'quote':enum(authorization_quotes),'r':deepcopy(props['r']),'v':deepcopy(props['v'])}
                        if 'ev' in props:field_props['ev']=deepcopy(props['ev'])
                        literal=obj(field_props)
                        literal['required']=[k for k in literal['required'] if k!='ev']
                        grounded_variants.append(literal)
                        ordered=[s['id'] for s in query['sources']]
                        for question in query['sources']:
                            worker=question['source']['participant']
                            later=[r for r in receipts if ordered.index(r)>ordered.index(question['id'])]
                            if worker==subject or not later:continue
                            # The model explicitly selects a generic service rule;
                            # identity and source order retain response-grant checks.
                            service=obj({'o':enum(['service']),
                                'i':deepcopy(props['i']), 'p':enum([worker]),
                                's':enum([subject]), 'e':enum(later),
                                'q':enum([question['id']]), 'v':deepcopy(props['v']),
                                'work':{'type':'string','minLength':1,'maxLength':160},
                                'r':deepcopy(props['r'])})
                            if subject not in query['bootstrap']['participants']:
                                grounded_variants.append(service)
                elif name in {'revoke','suspend','expire'} and 'g' in props:
                    # These operations reference a grant rather than a subject.
                    # Ground the receipt in that grant's actual subject too.
                    for aid,assignment in query['permission_state']['assignment'].items():
                        if assignment['status']!='active':continue
                        authorities=query['bootstrap']['authorities'].get(assignment['subject'],{}).get('restrict',[])
                        receipts=[s['id'] for s in query['sources'] if s['source']['participant'] in authorities
                            and (not compact or s['id']==query['new_source_id'])]
                        if not receipts:continue
                        grounded_variant=deepcopy(variant)
                        if name=='revoke' and query.get('information_types'):
                            grounded_variant['properties']['withdraw_types']={'type':'array','maxItems':8,
                                'uniqueItems':True,'items':enum(query['information_types'])}
                            grounded_variant['required'].append('withdraw_types')
                        grounded_variant['properties']['g']=enum([aid])
                        grounded_variant['properties']['e']=enum(receipts)
                        if 'e' not in grounded_variant['required']:grounded_variant['required'].append('e')
                        grounded_variants.append(grounded_variant)
                else:grounded_variants.append(variant)
            grants=[v for v in compact_schema['properties']['p']['items']['anyOf']
                if v['properties']['o']['enum']==['grant']]
            if grants:
                grant_props=grants[0]['properties']
                for subject in query['bootstrap']['participants']:
                    receipts=[s['id'] for s in query['sources'] if s['source']['participant']==subject
                              and s['id']==query['new_source_id']]
                    if not receipts:continue
                    own=[aid for aid,a in query['permission_state']['assignment'].items()
                        if a['participant']==a['subject']==subject and a['status']=='active']
                    activity_props={'o':enum(['activity']),'via':enum(['request']),
                        's':enum([subject]),'e':enum(receipts),'i':enum([*own,*grant_props['i']['enum']]),
                        'work':{'type':'string','minLength':1,'maxLength':160},
                        'r':{'type':'string','minLength':1,'maxLength':120},'v':deepcopy(grant_props['v'])}
                    grounded_variants.append(obj(activity_props))
                    ordered=[s['id'] for s in query['sources']]
                    for question in query['sources']:
                        worker=question['source']['participant']
                        if worker==subject or ordered.index(question['id'])>=ordered.index(receipts[0]):continue
                        pair_props={**activity_props,'via':enum(['response']),
                            'q':enum([question['id']]),'p':enum([worker]),
                            'wi':deepcopy(grant_props['i']),'wv':deepcopy(grant_props['v'])}
                        if selective:
                            pair_props['wf']={'type':'array','minItems':1,'maxItems':8,
                                'items':{'type':'string','minLength':1,'maxLength':100}}
                        order=('o','via','s','e','q','p','i','wi','work','r','v','wv')
                        if selective:order=(*order,'wf')
                        grounded_variants.append(obj({k:pair_props[k] for k in order}))
            compact_schema['properties']['p']['items']['anyOf']=grounded_variants
        if selective:
            classifications={}
            fact_names={'k':'kind','s':'information_subject','e':'attribution_source',
                        'r':'attribution_reason','t':'topics','x':'relationships'}
            for segment in query['segments']:
                item=deepcopy(compact_schema['properties']['f']['items'])
                for variant in item['anyOf']:
                    variant['properties'].pop('i',None)
                    variant['required']=[k for k in variant['required'] if k!='i']
                    variant['properties']={fact_names.get(k,k):v for k,v in variant['properties'].items()}
                    variant['required']=[fact_names.get(k,k) for k in variant['required']]
                    if 'information_subject' in variant['properties']:
                        variant['properties']['subject_basis']=enum(['own_information','service_recipient_information',
                            'third_party_information','unattributed'])
                        variant['properties']['subject_basis']['description']=(
                            'own_information: about the current speaker. service_recipient_information: a worker describing '
                            'a DIFFERENT recipient in service_context. third_party_information: another supported subject. '
                            'unattributed: subject is unknown/null.')
                        variant['required'].append('subject_basis')
                        order=('kind','subject_basis','information_subject','attribution_source','attribution_reason','topics','relationships')
                        variant['properties']={key:variant['properties'][key] for key in order if key in variant['properties']}
                classifications[str(segment['index'])]=item
                # One complete row shape prevents the model choosing the short
                # nonfact branch and then changing only its kind to instruction.
                # Null attribution carries no authority; decode requires complete
                # attribution for facts and discards it for nonfacts.
                factual=item['anyOf'][1]['properties']
                row_props={'kind':enum(['context','question','asserted_fact','instruction']),
                    'subject_basis':enum(['own_information','service_recipient_information',
                        'third_party_information','unattributed'],True),
                    'information_subject':deepcopy(factual['information_subject']),
                    'attribution_source':enum(factual['attribution_source']['enum'],True),
                    'attribution_reason':{'type':['string','null'],'minLength':1,'maxLength':120},
                    'topics':deepcopy(factual['topics']),
                    'relationships':deepcopy(factual['relationships'])}
                classifications[str(segment['index'])]=obj(row_props)
                if compact:
                    classifications[str(segment['index'])]['properties'].pop('subject_basis')
                    classifications[str(segment['index'])]['required'].remove('subject_basis')
                if disclosed:
                    extra={'information_subjects':{'type':'array','maxItems':32,'uniqueItems':True,
                        'items':enum([s for s in factual['information_subject']['enum'] if s is not None])},
                        'attribution_complete':{'type':'boolean'},
                        'action_actors':{'type':'array','maxItems':32,'uniqueItems':True,
                            'items':enum(query['bootstrap']['participants'])},
                        'mentioned_people':{'type':'array','maxItems':16,'uniqueItems':True,
                            'items':{'type':'string','minLength':1,'maxLength':80}}}
                    if query.get('information_types'):
                        extra.update(care_participants={'type':'array','maxItems':8,'uniqueItems':True,
                            'items':enum(query['bootstrap']['participants'])},
                            information_types={'type':'array','maxItems':8,'uniqueItems':True,
                            'items':enum(query['information_types'])})
                    classifications[str(segment['index'])]['properties'].update(extra)
                    classifications[str(segment['index'])]['required'].extend(extra)
            compact_schema['properties']['f']=obj(classifications)
            for variant in compact_schema['properties']['p']['items']['anyOf']:
                props=variant['properties'];name=props['o']['enum'][0]
                if 'i' in props and name in {'fields','activity','service','observe','deny','grant'}:
                    old=props['i'].get('enum',[])
                    existing=set(query['permission_state']['assignment'] if name=='activity' else
                                 query['permission_state']['activity'] if name=='observe' else [])
                    props['i']=enum(['new',*[value for value in old if value in existing]])
                if name=='activity' and 'wi' in props:props['wi']=enum(['new'])
                if name=='service':
                    props['wf']={'type':'array','minItems':1,'maxItems':8,
                        'items':{'type':'string','minLength':1,'maxLength':100}}
                    variant['required'].append('wf')
                if name in {'deny','replace_deny'}:
                    props['kind']=enum(['disclosure','communication']);variant['required'].append('kind')
                if name in {'erase','erase_facts'}:
                    props.update(timing=enum(['now','at','conditional']),
                        effective_at={'type':['string','null']},condition={'type':['string','null']})
                    variant['required'].extend(['timing','effective_at','condition'])
            query['new_ids']={kind:['new']*len(values) for kind,values in allocation.items()}
        if compact:
            variants=compact_schema['properties']['p']['items']['anyOf']
            compact_schema['properties']['p']['items']['anyOf']=compact_operation_variants(variants)
        if disclosed:
            from .acceptance_v19 import READ_PERMISSION
            current=next(s for s in query['sources'] if s['id']==query['new_source_id'])
            if READ_PERMISSION.search(current['text']):
                # A literal read permission must not decode as broader service
                # access. Fields/denials retain their normal authority checks.
                variants=compact_schema['properties']['p']['items']['anyOf']
                compact_schema['properties']['p']['items']['anyOf']=[v for v in variants
                    if v['properties']['o']['enum'] not in (['service'],['activity'])]
    query['response_wire']='compact-v18' if compact else 'compact-v17' if selective else 'compact-v16' if grounded else 'compact-v15'
    if grounded:
        # Source context is already deduplicated in sources/fact_sources. Avoid
        # repeating full original events inside every candidate evidence map.
        query['candidates']=[{k:p[k] for k in
            ('id','subject','quote','source_id','evidence_kind','relationships') if k in p}
            for p in query.get('candidates',[])]
        if maintenance and 'pair_context' in query:
            # Keep each selected fact beside that recipient's precise scope and
            # original authority receipts. Comparing unrelated global grant
            # dictionaries caused broad field grants to be mistaken for narrower
            # neighboring tasks. The output IDs and validator are unchanged.
            query={k:query[k] for k in ('stage','permission_version','as_of',
                'pair_context','source_context','response_wire')}
    def decode(value):
        if not isinstance(value,dict) or set(value)!=set(compact_schema['properties']):
            fail('Compact response must match its decision arrays')
        if grounded and not maintenance and (not isinstance(value['review'],str) or not 1<=len(value['review'])<=320):
            fail('A grounded update needs a bounded current-event recipient review')
        decisions=[]
        bindings=value['b']
        if indexed_pairs is not None:
            if not isinstance(bindings,dict) or set(bindings)!={str(i) for i in range(len(indexed_pairs))}:
                fail('Every focused recipient pair needs its required verdict slot')
            for decision in bindings.values():
                if (not isinstance(decision,dict) or not {'r','d'}<=set(decision)
                        or set(decision)-{'r','d','deny'}):
                    fail('Focused verdict slots cannot change their fact or recipient identities')
            bindings=[{'f':p['candidate']['id'],'g':p['assignment']['id'],
                'q':p['candidate']['quote'][:80],**bindings[str(i)]}
                for i,p in enumerate(indexed_pairs)]
        for b in bindings:
            required={'f','g','d','r'}|({'q'} if grounded else set())
            if not isinstance(b,dict) or set(b)-required-{'deny'} or not required<=set(b):
                fail('Invalid compact binding decision')
            quote_invalid=grounded and (b['f'] not in quotes or b['q']!=quotes[b['f']])
            flags={'allow':(True,True),'outside':(False,True),'unattributed':(False,False),
                'unattributed_in_scope':(True,False)}.get(b['d'])
            if flags is None:fail('Unknown compact scope/subject decision')
            decisions.append({'candidate_id':b['f'],'assignment_id':b['g'],'in_scope':flags[0],
                'subject_supported':flags[1],'blocked_by':b.get('deny',[]),'reason':b['r']})
            if quote_invalid:decisions[-1]['_invalid_quote']=True
        if maintenance:return {'binding_reviews':decisions}
        controls=[]
        if selective:
            slots=value['c'];expected={str(i) for i in range(len(query['control_segments']))}
            if not isinstance(slots,dict) or set(slots)!=expected:fail('Every source intent slot is required exactly once')
            control_values=[slots[str(i)] for i in range(len(expected))]
        else:control_values=value['c']
        for i,c in enumerate(control_values):
            if selective:
                if (not isinstance(c,dict) or set(c)!={'intents','recipient','existing','r'}):
                    fail('Each current segment needs its indexed intent and recipient assessment')
                if set(c['intents']) & {'none','unresolved'} and len(c['intents'])!=1:
                    fail('None/unresolved intents cannot hide another selected control effect')
                controls.append({'index':i,'status':'unresolved' if 'unresolved' in c['intents'] else 'no_change',
                    'operations':[],'reason':c['r'],'intents':c['intents'],
                    'recipient':c['recipient'],'existing':c['existing']})
            elif type(c) is int and c==0:
                controls.append({'index':i,'status':'no_change','operations':[],
                    'reason':'The model assessed no control change for this source segment.'})
            elif grounded and isinstance(c,dict) and set(c)=={'i','s','r'} and c['s']=='unresolved_limit':
                controls.append({'index':c['i'],'status':'unresolved','operations':[], 'reason':c['r']})
            elif isinstance(c,dict) and set(c)=={'i','s','k','o','r'}:
                controls.append({'index':c['i'],'status':'unresolved' if c['s']=='unresolved_limit' else c['s'],
                    'operations':c['o'],'reason':c['r'],
                    'expected_effects':['lifecycle' if k=='erase_or_expire' else k for k in c['k']]})
            else:fail('Invalid compact control decision')
        sources={s['id']:s for s in [*query.get('sources',[]),*query.get('fact_sources',[])]}
        requested={s['index']:s for s in query['segments']}
        segments=[]
        if selective:
            if not isinstance(value['f'],dict) or set(value['f'])!={str(i) for i in requested}:
                fail('Each requested fact-classification slot is required exactly once')
            fact_names={'kind':'k','information_subject':'s','attribution_source':'e',
                        'attribution_reason':'r','topics':'t','relationships':'x'}
            fact_values=[]
            for i in requested:
                row=dict(value['f'][str(i)]);basis=row.pop('subject_basis',None)
                if row.get('kind') in {'context','question'}:
                    fact_values.append({'i':i,'k':row['kind']})
                    continue
                if (not compact and basis is None) or not row.get('attribution_reason'):
                    fail('Facts require an explicit attribution basis and reason')
                if basis!='unattributed' and not row.get('attribution_source') and not (
                        disclosed and not row.get('information_subjects') and not row.get('attribution_complete')):
                    fail('Attributed facts require their actual source receipt')
                current=sources.get(query['new_source_id'],{}).get('source',{}).get('participant')
                if not compact and basis=='own_information' and row.get('information_subject')!=current:
                    fail('Own-information attribution must name the actual current speaker')
                if not compact and basis=='service_recipient_information':
                    subjects={a['information_subject'] for a in query.get('service_context',{}).values()}
                    if row.get('information_subject') not in subjects:
                        fail('Service-result attribution must match the actual accepted service recipient')
                if basis=='unattributed' and row.get('information_subject') is not None:
                    fail('Unattributed facts cannot select an unsupported subject')
                fact_values.append({'i':i,**{fact_names.get(k,k):v for k,v in row.items()}})
        else:fact_values=value['f']
        for f in fact_values:
            if not isinstance(f,dict) or f.get('i') not in requested:fail('Unknown compact segment')
            i=f['i'];kind=f.get('k')
            if kind in {'context','question'}:
                if set(f)!={'i','k'}:fail('Nonfacts cannot supply factual fields')
                segments.append({'index':i,'kind':kind,'subject':None,'subject_source':None,
                    'subject_quote':None,'topics':[],'relationships':[],'reason':'Model classified source as '+kind+'.'})
            else:
                if set(f)-{'i','k','s','e','r','t','x',*ATTRIBUTION_FIELDS} or not {'i','k','s','r','x'}<=set(f):
                    fail('Invalid compact factual segment')
                evidence=f.get('e',requested[i]['source_id']) if f['s'] is not None or (disclosed and f.get('information_subjects')) else None
                receipt=sources.get(evidence)
                quote=receipt['text'] if receipt else None
                segments.append({'index':i,'kind':kind,'subject':f['s'],'subject_source':evidence,
                    'subject_quote':quote,'topics':f.get('t',[]),
                    'relationships':[{'kind':r['k'],'target_id':r['f'],
                        'evidence_quote':requested[i]['quote']} for r in f.get('x',[])],
                    'reason':f['r'],**({k:f.get(k) for k in ATTRIBUTION_FIELDS if k in f} if disclosed else {})})
        selected=map_operation(value['p'],decode=True);operations=[]
        if selective:
            for index,op in enumerate(selected):
                name=op.get('op')
                if op.get('id')=='new':
                    kind='restriction' if name=='record_restriction' else 'activity' if name=='observe_participation' else 'grant'
                    slot=2*index if kind=='grant' else index
                    op['id']=allocation[kind][slot]
                if name=='activity' and op.get('wi')=='new':op['wi']=allocation['grant'][2*index+1]
        for op in selected:
            if selective and op.get('op')=='record_restriction' and op.get('supersedes') is None:
                op.pop('supersedes',None)
            if selective and op.get('op') in {'record_restriction','replace_restriction'}:
                kind=op.pop('kind',None)
                if kind not in {'disclosure','communication'}:fail('Restrictions require their explicit control kind')
                if kind=='communication':
                    if not any(s['kind']=='instruction' for s in segments):
                        fail('A communication preference must be retained as a protected instruction')
                    continue  # Source facts retain the preference; no reading rights change.
            if not grounded or op.get('op')!='activity':
                operations.append(op);continue
            base={'op','via','subject','authority_source','id','work','reason','validity'}
            paired=op.get('via')=='response'
            if (op.get('via') not in {'request','response'} or set(op)!=(base|
                    ({'q','participant','wi','wv'}|({'wf'} if selective else set()) if paired else set()))):
                fail('Activity access requires its selected recipients and authenticated receipts')
            receipt=op['authority_source'];statement=sources.get(receipt)
            if (receipt!=query['new_source_id'] or not statement or
                    statement['source']['participant']!=op['subject']):
                fail('Only the subject own current participation can activate their service')
            work=op['work']
            if not isinstance(work,str) or not 1<=len(work)<=160:
                fail('Activity needs its bounded source-grounded service meaning')
            prior=None
            if paired:
                prior=sources.get(op['q']);ordered=[s['id'] for s in query['sources']]
                if (not prior or op['q'] not in ordered or
                        ordered.index(op['q'])>=ordered.index(receipt) or
                        prior['source']['participant']!=op['participant'] or op['participant']==op['subject']):
                    fail('Activity response needs the actual recipient worker own earlier receipt')
                if op['wi']==op['id']:fail('Activity recipients need separate grant identities')
            existing=query['permission_state']['assignment'].get(op['id'])
            if existing:
                if (existing['participant']!=op['subject'] or existing['subject']!=op['subject'] or
                        existing['status']!='active' or existing.get('validity',{})!=op['validity']):
                    fail('Activity reuse cannot change another recipient or the existing lifetime')
            else:
                own={'op':'self','id':op['id'],'subject':op['subject'],
                    'authority_source':receipt,'validity':op['validity'],'work':work,'reason':op['reason']}
                if compact and paired:own['evidence']=[receipt,op['q']]
                if compact:
                    own['_accepted_service']={'worker':op['participant'] if paired else None,
                        'worker_source_id':op['q'] if paired else None,
                        'subject_source_id':receipt,'task_summary':work}
                operations.append(own)
            if paired:
                linked=[v for v in selected if v.get('op')=='fields' and v.get('id')==op['wi']]
                if linked:
                    if (len(linked)!=1 or linked[0]['participant']!=op['participant'] or
                            linked[0]['subject']!=op['subject'] or linked[0]['authority_source']!=receipt or
                            linked[0]['validity']!=op['wv']):
                        fail('Activity must reference the same recipient and lifetime as its literal field grant')
                    # The selected literal fields constrain the accepted service;
                    # do not issue a second broader grant with the same ID.
                    linked[0]['evidence']=list(dict.fromkeys([*(linked[0].get('evidence') or []),op['q']]))
                else:
                    operations.append({'op':'service','id':op['wi'],'participant':op['participant'],
                        'subject':op['subject'],'authority_source':receipt,'q':op['q'],
                        'validity':op['wv'],'work':work,'reason':op['reason'],
                        **({'wf':op['wf']} if selective else {})})
        for operation_index,op in enumerate(operations):
            if (grounded and op.get('op') in {'self','service','fields','propose_assignment'} and
                    op.get('authority_source')!=query['new_source_id']):
                fail('New grants require the current authenticated authority event',operation_index=operation_index)
            if grounded and op.get('op')=='fields':
                if set(op)-{'op','id','participant','subject','authority_source','quote','reason','validity','evidence'}:
                    fail('Literal field access requires its exact source authorization',operation_index=operation_index)
                statement=sources.get(op.get('authority_source'));quote=op.pop('quote',None)
                options=[statement['text'],*[s['quote'] for s in query['control_segments']]] if statement else []
                if not isinstance(quote,str) or quote not in options:
                    fail('Literal scope must select an exact supplied authorization statement',operation_index=operation_index)
                op.update(op='propose_assignment',basis='explicit',via='explicit',
                    information_scope='Only the information fields explicitly authorized for THIS recipient by: '+quote+
                    ' Each fact may contain any subset of the permitted fields. Original exclusions and '
                    'conditions constrain every field. Do not add unstated event-subtype or activity limits.')
            if grounded and op.get('op')=='self':
                allowed={'op','id','subject','authority_source','validity','work','reason'}
                if compact and 'evidence' in op:allowed.add('evidence')
                if compact and '_accepted_service' in op:allowed.add('_accepted_service')
                if set(op)!=allowed:
                    fail('Self activity selects only its authenticated subject and supported activity',operation_index=operation_index)
                statement=sources.get(op['authority_source'])
                if not statement or statement['source']['participant']!=op['subject']:
                    fail('Self activity requires that subject own authenticated participation',operation_index=operation_index)
                activity=op.pop('work')
                if not isinstance(activity,str) or not 1<=len(activity)<=160:
                    fail('Self activity needs its bounded source-grounded activity meaning',operation_index=operation_index)
                op.update(op='propose_assignment',participant=op['subject'],basis='explicit',via='self_request',
                    information_scope=self_activity_scope(activity,continuing=compact))
            if grounded and op.get('op')=='service':
                if set(op)!=({'op','id','participant','subject','authority_source','q','validity','work','reason'}|
                             ({'wf'} if selective else set())):
                    fail('Service access selects only a supported worker interaction and activity',operation_index=operation_index)
                work=op.pop('work')
                if not isinstance(work,str) or not 1<=len(work)<=160 or op['participant']==op['subject']:
                    fail('Service access needs a distinct worker and bounded activity meaning',operation_index=operation_index)
                worker_fields=op.pop('wf',None)
                if selective and (not isinstance(worker_fields,list) or not 1<=len(worker_fields)<=8 or
                        any(not isinstance(v,str) or not 1<=len(v)<=100 for v in worker_fields)):
                    fail('Worker service needs its closed source-supported information field set')
                if query.get('permission_version','').split('-',1)[0]=='19':
                    op['_accepted_service']={'worker':op['participant'],'worker_source_id':op['q'],
                        'subject_source_id':op['authority_source'],'task_summary':work,
                        'information_fields':deepcopy(worker_fields)}
                scope=('Accepted service: '+work+'. ONLY these necessary information fields: '+
                    '; '.join(worker_fields)+'. Includes their continuing instructions, updates, cancellations, '
                    'conditions and operational follow-through. Also includes subject-provided operational contact, '
                    'communication and recipient-access/routing instructions necessary to carry out THIS service. '
                    'An original explicit closed field list or exclusion still overrides those operational categories. '
                    'No clinical findings or private reasons are implied by an administrative service. '
                    'Original authorization limits apply; no other '
                    'information about the subject is granted.') if selective else (
                    'Necessary information for the accepted service: '+work+
                    '. Includes responsive facts, instructions and status, and the subject-provided operational '
                    'contact routes and communication conditions necessary to carry out this same service. '
                    'Original authorization exclusions constrain every field. Excludes unrelated activities, '
                    'other subjects information, unrelated finances and credentials. An explicit closed field '
                    'list never expands through this rule.')
                op.update(op='propose_assignment',basis='explicit',via='response',information_scope=scope)
            if grounded and op.get('op')=='propose_assignment':
                if not isinstance(op.get('validity'),dict):
                    fail('Every grant needs an explicit lifetime review: v={} or supported expiry',operation_index=operation_index)
                via=op.pop('via',None);question=op.pop('q',None)
                if via not in {'explicit','self_request','response'}:
                    fail('Grant needs its explicit or activity-response authorization basis',operation_index=operation_index)
                if via=='self_request' and op.get('participant')!=op.get('subject'):
                    fail('A self request authorizes only that subject',operation_index=operation_index)
                if via=='response':
                    prior=sources.get(question);receipt=op.get('authority_source',query['new_source_id'])
                    ordered=[s['id'] for s in query['sources']]
                    if (not prior or question not in ordered or receipt not in ordered
                            or ordered.index(question)>=ordered.index(receipt)):
                        fail('Responsive grant needs the prior worker interaction and later subject receipt',operation_index=operation_index)
                    worker=prior['source']['participant']
                    if ((op.get('participant')!=op.get('subject') and worker!=op['participant'])
                            or worker==op.get('subject')):
                        fail('Responsive grant must cite the actual recipient worker, independently from the subject',
                             operation_index=operation_index,recipient=op.get('participant'),
                             worker_source_ids=[s['id'] for s in query['sources'] if s['source']['participant']==op.get('participant')],
                             repair='A literal subject permission naming its recipient uses via=explicit without q. '
                                    'For an inferred response grant cite a prior source authored by the actual worker.')
                    op['evidence']=list(dict.fromkeys([*(op.get('evidence') or []),question]))
            # Optional omitted and null collections both mean no additional
            # evidence/metadata, never a positive access decision. Validate other
            # types explicitly instead of crashing during iterable expansion.
            for field in ('evidence','performance','corroboration','recipient_access',
                          'unresolved','information_needs'):
                if op.get(field) is None:op.pop(field,None)
                elif not isinstance(op[field],list):fail('Operation '+field+' must be an array')
            if op.get('validity') is None:op.pop('validity',None)
            name=op['op'];e=op.get('authority_source',query['new_source_id'])
            if name=='observe_participation':
                op.setdefault('basis','performance');op['status']='tentative';op.setdefault('requester',None)
                # Selecting an observation is the model's semantic decision.
                # Its own authenticated event is a mechanical worker receipt,
                # not missing consent and never a requester read grant.
                receipt=sources.get(e)
                if (grounded and not op.get('performance') and receipt
                        and receipt['source']['participant']==op.get('participant')):
                    op['performance']=[e]
            if name=='establish_activity':
                op.setdefault('basis','corroborated');op['status']='established'
                prior=query['permission_state']['activity'].get(op.get('id'),{})
                for field in ('participant','requester','activity'):
                    if field not in op and field in prior:op[field]=deepcopy(prior[field])
                if 'participant' not in op or 'activity' not in op:fail('A new accepted task needs its worker and task meaning')
                op.setdefault('requester',None);op.setdefault('recipient_access',[])
                if op['basis']=='corroborated':op.setdefault('corroboration',[e])
            if name=='propose_assignment':op['status']='active';op.setdefault('actions',['read'])
            if name in {'record_restriction','replace_restriction'}:
                if grounded and op.get('blocked_assignments'):
                    fail('A field-specific denial cannot suspend entire grants; use an explicit supported withdrawal',
                        operation_index=operation_index)
                op['effect']='deny';op.setdefault('actions',['read',*query['bootstrap'].get('read_pipeline_actions',[])])
            authority=(name in {'record_restriction','replace_restriction','resolve_restriction','revoke_assignment','erase_information','erase_candidates'}
                or name in {'resolve_control_hold','execute_erasure'} or name=='propose_assignment' and op.get('basis')=='explicit'
                or name=='establish_activity' and op.get('basis')=='handoff')
            if authority:op.setdefault('authority_source',e)
            elif 'authority_source' in op:op.pop('authority_source')
            if name!='resolve_control_hold':
                op['evidence']=list(dict.fromkeys([e,*op.get('evidence',[]),*op.get('performance',[]),*op.get('corroboration',[])]))
        return {'control_review':controls,'permission_changes':operations,
            'segments':segments,'binding_reviews':decisions}
    return query,compact_schema,decode


def encode_fixture(value, query=None):
    """Lossless decision encoding for scripted contract tests, not inference."""
    bindings=[{'f':b['candidate_id'],'g':b['assignment_id'],
        'd':('allow' if b['subject_supported'] else 'unattributed_in_scope') if b['in_scope'] else
            ('outside' if b['subject_supported'] else 'unattributed'),
        'deny':b['blocked_by'],'r':b['reason']} for b in value['binding_reviews']]
    if int((query or {}).get('permission_version','15').split('-',1)[0])>=16:
        quotes={p['id']:p['quote'][:80] for p in query.get('candidates',[])}
        quotes.update({s['candidate_id']:s['quote'][:80] for s in query.get('segments',[])})
        for b in bindings:b['q']=quotes.get(b['f'],'')
        if 'pair_context' in query:
            slots={(p['candidate']['id'],p['assignment']['id']):str(i)
                for i,p in enumerate(query['pair_context'])}
            bindings={slots[(b['f'],b['g'])]:{k:v for k,v in b.items() if k not in {'f','g','q'}}
                for b in bindings if (b['f'],b['g']) in slots}
    if set(value)=={'binding_reviews'}:return {'b':bindings}
    operations=value['permission_changes']
    correction=(query or {}).get('correction',{})
    if type(correction.get('operation_index')) is int:
        operations=deepcopy(correction['preserved_permission_changes'])
        if correction.get('mode')=='append_effect':operations.extend(value['permission_changes'])
        else:
            i=correction['operation_index'];operations[i:i+1]=value['permission_changes']
    families={'propose_assignment':'grant','record_restriction':'deny','replace_restriction':'deny',
        'revoke_assignment':'deny','suspend_assignment':'deny','observe_participation':'task',
        'establish_activity':'task','end_activity':'erase_or_expire','erase_candidates':'erase_or_expire',
        'erase_information':'erase_or_expire','schedule_expiry':'erase_or_expire','resolve_control_hold':'erase_or_expire',
        'execute_erasure':'erase_or_expire'}
    controls=[0 if c['status']=='no_change' else {'i':c['index'],
        's':'unresolved_limit' if c['status']=='unresolved' else c['status'],
        'k':list(dict.fromkeys(families[operations[i]['op']] for i in c['operations'] if i<len(operations))) or ['deny'],
        'o':c['operations'],'r':c['reason']} for c in value['control_review']]
    if int((query or {}).get('permission_version','15').split('-',1)[0])>=16:
        controls=[{'i':c['index'],'s':'unresolved_limit','r':c['reason']}
                  if c['status']=='unresolved' else 0 for c in value['control_review']]
    if int((query or {}).get('permission_version','15').split('-',1)[0])>=17:
        controls={}
        for c in value['control_review']:
            selected=[operations[i] for i in c['operations'] if i<len(operations)]
            op=next(iter(selected),{})
            def intent(item):
                return 'communication' if item.get('kind')=='communication' else {'propose_assignment':'grant','revoke_assignment':'revoke',
                 'record_restriction':'deny','replace_restriction':'deny','erase_candidates':'erase',
                 'erase_information':'erase','execute_erasure':'erase'}.get(item.get('op'),'none')
            intents=['unresolved'] if c['status']=='unresolved' else list(dict.fromkeys(intent(item) for item in selected)) or ['none']
            controls[str(c['index'])]={'intents':intents,'recipient':op.get('participant'),
                                     'existing':None,'r':c['reason']}
    segments=[]
    for s in value['segments']:
        f={'i':s['index'],'k':s['kind']}
        if s['kind'] not in {'context','question'}:
            f.update(s=s['subject'],e=s['subject_source'],r=s['reason'])
            if s['topics']:f['t']=s['topics']
            f['x']=[{'k':r['kind'],'f':r['target_id']} for r in s['relationships']]
        segments.append(f)
    if int((query or {}).get('permission_version','15').split('-',1)[0])>=17:
        fact_names={'k':'kind','s':'information_subject','e':'attribution_source',
                    'r':'attribution_reason','t':'topics','x':'relationships'}
        segments={str(f['i']):{fact_names.get(k,k):v for k,v in f.items() if k!='i'} for f in segments}
        for f in segments.values():
            f.setdefault('topics',[])
            if f['kind'] not in {'context','question'}:
                f['subject_basis']='unattributed' if f.get('information_subject') is None else 'third_party_information'
            else:
                f.update(subject_basis=None,information_subject=None,attribution_source=None,
                    attribution_reason=None,topics=[],relationships=[])
            if int((query or {}).get('permission_version','15').split('-',1)[0])>=18:
                f.pop('subject_basis',None)
            if str((query or {}).get('permission_version','')).split('-',1)[0]=='19':
                f.update(information_subjects=[] if f.get('information_subject') is None else [f['information_subject']],
                    attribution_complete=f['kind'] not in {'context','question'},action_actors=[],mentioned_people=[])
                if query.get('information_types'):f.update(care_participants=[],information_types=[])
    operations=map_operation(value['permission_changes'])
    for op in operations:
        if int((query or {}).get('permission_version','15').split('-',1)[0])>=16 and op['o']=='grant':
            # Test fixtures select the same literal source-scope variant as the
            # production grammar; no free-form scope labels are provider options.
            statement=next(s for s in query['sources'] if s['id']==query['new_source_id'])
            fields={'o':'fields','i':op['i'],'p':op['p'],'s':op['s'],
                'e':op.get('e',query['new_source_id']),'quote':statement['text'],
                'r':op['r'],'v':op.get('v',{})}
            if 'ev' in op:fields['ev']=op['ev']
            op.clear();op.update(fields)
        for field in ('st','effect'):op.pop(field,None)
        if op['o']=='observe':op.pop('basis',None)
        if op['o']=='accept' and op.get('basis')=='corroborated':op.pop('basis',None);op.pop('authority',None)
        if int((query or {}).get('permission_version','15').split('-',1)[0])>=17:
            if op['o'] in {'fields','grant','deny','observe'}:op['i']='new'
    result={'c':controls,'p':operations,'f':segments,'b':bindings}
    if int((query or {}).get('permission_version','15').split('-',1)[0])>=16:
        result={'review':'Review the authenticated current speaker and each affected recipient independently.',**result}
    return result
