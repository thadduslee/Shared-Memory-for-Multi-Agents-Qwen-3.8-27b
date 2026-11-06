"""V19 disclosure attribution, exact projections and conjunctive authorization.

Identity evidence and source semantics describe data; neither creates a grant.
Author, action actor, information subjects and permission authority are distinct.
"""
from copy import deepcopy
import json
import re

from .system import AccessDenied, digest, encoded
from .permission_state import RECENT_TURNS
from . import revocation_v19 as identities

POLICY_VERSION='v19-disclosed-subjects:1'
PROMPT_VERSION='permission-inference-v19-disclosed-subjects-v2'
INSTRUCTION='''
V19 disclosure contract: information_subjects lists EVERY person/resource whose
information is disclosed by the complete segment, including identity/routing
associations and contextual references. attribution_complete=false if any subject
is unknown. action_actors describes who performed an action, separately from its
information subjects. Authorship, I booked/filed/entered, employment, title, or a
grant held by the author NEVER establishes personal ownership of the disclosed
information. Authenticated source.subject is an information-subject receipt, not
an author or a claim of exhaustive coverage. Include other disclosed subjects.
Do not merge ambiguous names or same-surname people. Cite actual source evidence.
Unknown attribution stays protected. Exact independent projections require host
validation; never paraphrase/sanitize a mixed record into another audience.
subject=null with nonempty RESOLVED information_subjects represents a mixed fact,
not unknown attribution. Review each matching subject/grant independently; whole
record release requires current authorization for all its information_subjects.

First-person personal statements concern the authenticated SPEAKER, including
ordinary descriptions of symptoms, experiences, preferences and needs. Do not
require the speaker to repeat their name. A worker's "I booked/entered/filed"
describes an action, not ownership of the customer's information. For an unnamed
follow-up note, cite the subject's recent authenticated personal statement as
attribution_source, or the trusted source.subject receipt. Do not assign every
note in a conversation to one patient: preserve explicit other/mixed subjects.
An existing personal OWNS grant already permits personal facts; do not invent a
service acceptance or a new grant merely to store a factual answer. Explicit
permission to another recipient is a field grant, not acceptance of their work.
For "Jo may read my appointment time and location only", c selects grant with
recipient=Jo, and p MUST include {o:"fields",i:"new",p:Jo,s:the speaking subject,
e:CURRENT source,quote:the exact supplied permission statement,r:short reason,v:{}}.
Use the actual opaque IDs and schema property names. fields is the explicit READ
GRANT operation; it is not an activity or a denial. If the same statement excludes
other information, ALSO emit the recipient-specific deny operation. Denials alone
do not grant the permitted information, and another self grant does not grant Jo
anything. Preserve the stated expiry in v rather than defaulting to {}.
'''


def enabled(updater):return identities.enabled(updater)


def installed(core, compartment):
    return bool(core.db.execute('SELECT 1 FROM privacy_metadata WHERE key=?',
        ('attribution-policy:'+compartment,)).fetchone())


def install(updater, migration=None):
    c=updater.core
    if migration not in (None,'quarantine_existing'):
        raise ValueError('Unsupported attribution migration')
    old=installed(c,updater.compartment)
    if old and c.db.execute('SELECT value FROM privacy_metadata WHERE key=?',('attribution-policy:'+updater.compartment,)).fetchone()[0]!=POLICY_VERSION:
        raise ValueError('Attribution policy version mismatch; use an explicitly reviewed migration, never a silent downgrade')
    if not old and c.db.execute('SELECT 1 FROM resources WHERE compartment=? LIMIT 1',(updater.compartment,)).fetchone() and migration is None:
        raise ValueError('V19 attribution requires a fresh store/compartment or explicit attribution_migration=quarantine_existing on a working copy')
    with c.transaction():
        c.db.execute('''CREATE TABLE IF NOT EXISTS privacy.attribution_history_v19(
            compartment TEXT NOT NULL,candidate_id TEXT NOT NULL,version INTEGER NOT NULL,
            body TEXT NOT NULL,PRIMARY KEY(compartment,candidate_id,version))''')
        c.db.execute('''CREATE TABLE IF NOT EXISTS privacy.attribution_views_v19(
            compartment TEXT NOT NULL,candidate_id TEXT NOT NULL,version INTEGER NOT NULL,
            subject TEXT NOT NULL,resource_id TEXT NOT NULL,
            PRIMARY KEY(compartment,candidate_id,version,subject,resource_id))''')
        c.db.execute('''CREATE TABLE IF NOT EXISTS privacy.attribution_pending_v19(
            compartment TEXT NOT NULL,candidate_id TEXT NOT NULL,reason TEXT NOT NULL,
            PRIMARY KEY(compartment,candidate_id))''')
        c.db.execute('INSERT OR REPLACE INTO privacy_metadata VALUES (?,?)',
            ('attribution-policy:'+updater.compartment,POLICY_VERSION))
        c.db.execute('INSERT OR IGNORE INTO privacy_metadata VALUES (?,?)',('attribution-revision:'+updater.compartment,'0'))
        if not old and migration:
            for r in c.db.execute('SELECT * FROM fact_candidates WHERE compartment=?',(updater.compartment,)).fetchall():
                c.db.execute('INSERT OR IGNORE INTO attribution_pending_v19 VALUES (?,?,?)',
                    (updater.compartment,r['id'],'Explicit migration requires original-source review'))
                c.db.execute('INSERT OR IGNORE INTO attribution_history_v19 VALUES (?,?,0,?)',
                    (updater.compartment,r['id'],encoded({'state':'PENDING_REVIEW','reason':'Explicit migration; prior attribution unverified',
                        'previous_candidate':audit_candidate(json.loads(r['body'])),'policy_version':POLICY_VERSION})))
        c._tickets.clear()


def source_prefix(updater):
    return {r['id']:json.loads(r['payload']) for r in updater.core.db.execute(
        "SELECT id,payload FROM resources WHERE compartment=? AND kind='observation' AND state='active' ORDER BY rowid",
        (updater.compartment,)) if updater.core._current(r['id'])}


def names(bootstrap,sources):
    result=identities.identities(bootstrap,sources)
    # A self introduction can be followed by a purpose clause. No roster, title,
    # surname similarity or other speaker's assertion supplies identity evidence.
    for sid,body in sources.items():
        text=body['text'].replace('\u2019',"'")
        for m in re.finditer(r"\b(?:I'm|I am|My name is|This is) ("+identities.NAME+r")(?= [a-z]|[,.!?]|$)",text):
            name=m.group(1);pid=body['source']['participant']
            proof=identities.receipt(sid,body['text'],*m.span(),participant=pid)
            for alias in {identities.norm(name),identities.norm(name.split()[0])}:
                result.setdefault(alias,{}).setdefault(pid,[]).append(proof)
    return result


def mentions(text,registry):
    found=set();proofs=[];unknown=[];covered=[];known_ranges=[]
    for alias,people in registry.items():
        if len(people)==1:
            pattern=re.escape(alias).replace(r'\ ',r'\s+')
            known_ranges.extend(m.span() for m in re.finditer(r'(?<!\w)'+pattern+r'(?!\w)',text,re.I))
    for alias in sorted(registry,key=len,reverse=True):
        for m in re.finditer(r'(?<!\w)'+re.escape(alias)+r'(?!\w)',identities.norm(text)):
            if any(a<=m.start() and m.end()<=b for a,b in covered):continue
            covered.append(m.span());people=registry[alias]
            if len(people)!=1:unknown.append(alias);continue
            pid=next(iter(people));found.add(pid);proofs.extend(people[pid])
    # Unknown multiword proper names remain unknown entities, not author data.
    # Calendar words are syntax, not an entity/role/domain roster.
    calendar={'Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday',
        'January','February','March','April','May','June','July','August','September','October','November','December',
        'Mon','Tue','Tues','Wed','Thu','Thur','Thurs','Fri','Sat','Sun','Jan','Feb','Mar','Apr','Jun','Jul','Aug','Sep','Sept','Oct','Nov','Dec'}
    for m in re.finditer(r'\b[A-Z][a-z]+(?: [A-Z][a-z]+){1,3}\b',text):
        name=m.group()
        if any(w in calendar for w in name.split()):continue
        if identities.norm(name) not in registry:unknown.append(name)
    for m in re.finditer(r'\b[A-Z][a-z]+\b',text):
        if any(a<=m.start() and m.end()<=b for a,b in known_ranges):continue
        if (m.group() in {'I','My','The','A','An','This','That','There','It','Please','Yes','No','And','But','For','Only','Same'}
                and (not text[:m.start()].strip() or text[:m.start()].rstrip().endswith(('.','!','?',':')))):
            continue
        if m.start() and m.group() not in calendar and identities.norm(m.group()) not in registry:
            unknown.append('Unresolved named entity '+m.group())
    return found,proofs,unknown


def positive_personal(text):
    text=text.strip().replace('\u2019', "'")
    text=re.sub(r'"[^"\n]*"|\u201c[^\u201d\n]*\u201d','',text)
    if re.search(r'\b(?:notes?|records?|reports?|lists?|files?|documents?|calendar)\b',text,re.I):return False
    # Personal states need no name or special "my own" wording. Keep action
    # performance/holding another person's records distinct from self disclosure.
    state=re.search(r"\bI(?:'ve| have) had\b|\bI (?:feel|felt|experience|experienced|"
        r"prefer|live)\b|\bI(?:'m| am| was|'ve been| have been) "
        r"(?:(?:feeling|experiencing)\b|(?!\w+ing\b)\S)",text,re.I)
    possession=(re.search(r'\bI (?:have|had)\b',text,re.I) and not re.search(
        r'\b(?:details|another|other|someone|somebody|client|customer|patient|checking|checked)\b',text,re.I))
    return bool(state or possession or re.match(r'^My (?:own|personal|private) \w',text,re.I)
        or re.match(r'My [a-z][a-z -]* (?:is|are|was|were|remains)\b',text,re.I)
        or re.fullmatch(r'I (?:(?:am|was) (?:here|ready|available|unavailable|tired)|live alone)[.!]?',text,re.I)
        or re.match(r'^I [^.!?"\u201c\u201d]*\b(?:my own|for myself|for me)\b',text,re.I))


def personal_evidence(text):
    # Subject inclusion is conservative: a real personal clause still discloses
    # its speaker when another clause discusses a different person.
    return any(positive_personal(part.strip().rstrip(';')) for part in re.split(r'(?<=[.!?;])\s*',text) if part.strip())


def self_personal(text):
    # Positive personal predicates. An action performed for an unstated
    # beneficiary is not a personal-information receipt.
    text=text.strip()
    if re.search(r"\b(?:their|her|his|them|they|you|your|client|customer|someone|somebody)\b",text,re.I):return False
    return positive_personal(text)


def context_receipt(person, author, sid, source_id, sources):
    """A cited personal turn can anchor its immediate conversational follow-up.

    This is evidence for the information subject, never a read permission. A
    different speaker's later personal disclosure ends this context.
    """
    body=sources.get(sid,{})
    if person==author or body.get('source',{}).get('participant')!=person:
        return False
    if not personal_evidence(body.get('text','')):
        return False
    order=list(sources)
    if (sid not in order or source_id not in order or
            not 0<order.index(source_id)-order.index(sid)<RECENT_TURNS):
        return False
    following=False
    for key,item in sources.items():
        if key==source_id:
            break
        if key==sid:
            following=True
            continue
        if following and item['source']['participant']!=person and personal_evidence(item['text']):
            return False
    return True


def compile_attribution(updater,candidate,source,sources,decision,version=1):
    boot=updater._source_bootstrap(sources);author=source['source']['participant']
    proposed=decision.get('information_subjects')
    complete=decision.get('attribution_complete') is True
    actors=decision.get('action_actors',[])
    if not isinstance(proposed,list) or any(s not in boot.resources for s in proposed):
        proposed=[];complete=False
    if not isinstance(actors,list) or any(a not in boot.participants for a in actors):
        actors=[];complete=False
    registry=names(boot,sources)
    disclosed,proofs,unknown=mentions(candidate['quote'],registry)
    personal=personal_evidence(candidate['quote'])
    if personal:disclosed.add(author)
    direct_personal=personal and self_personal(candidate['quote']) and not unknown and not disclosed-{author}
    if direct_personal and not proposed:
        # A clear authenticated self disclosure does not become an unclaimed
        # file just because the model omitted its subject tag.
        proposed=[author];complete=True
    supported_actors=[a for a in actors if a==author and re.search(r'\bI\b',candidate['quote'])]
    attested=source['source'].get('subject')
    if attested:disclosed.add(attested)
    # Named information contradicts author-as-subject even if a prior model was
    # confident. Authorship and an ownership grant are not contrary evidence.
    claimed=set(proposed)
    if disclosed-{author} and not personal:claimed.discard(author)
    disclosed.update(claimed)
    if re.search(r'\b(?:another|other) (?:person|patient|client|customer|subject)\b',candidate['quote'],re.I) and len(disclosed)<2:
        unknown.append('Unresolved contextual comparison subject')
    evidence={candidate['source_id']:candidate['quote']}
    evidence.update({p['source_id']:p['quote'] for p in proofs})
    sid=decision.get('subject_source');quote=decision.get('subject_quote')
    if direct_personal and sid is None:
        sid=candidate['source_id'];quote=source['text']
    if sid in sources and isinstance(quote,str) and quote and quote in sources[sid]['text']:
        evidence[sid]=quote
    else:complete=False
    if disclosed=={author} and attested!=author and not self_personal(candidate['quote']):
        unknown.append('Authorship/action performance is not evidence of personal information')
    # An unnamed third party needs an actual authenticated subject receipt;
    # a model's selection from grant holders cannot identify them.
    for person in disclosed-{author}:
        contextual=(sid!=candidate['source_id'] and sid in evidence and
            context_receipt(person,author,sid,candidate['source_id'],sources))
        if not contextual and complete:
            # The model may correctly tag a follow-up while citing only the
            # note itself. Attach the actual recent personal receipt rather
            # than discarding an otherwise grounded patient association.
            order=list(sources);position=order.index(candidate['source_id'])
            for context_id in reversed(order[max(0,position-RECENT_TURNS+1):position]):
                if context_receipt(person,author,context_id,candidate['source_id'],sources):
                    evidence[context_id]=sources[context_id]['text']
                    contextual=True
                    break
        anchored=(person in mentions(candidate['quote'],registry)[0] or attested==person or contextual)
        if not anchored:unknown.append('Unanchored subject '+person)
    state='RESOLVED' if complete and disclosed and not unknown else 'UNRESOLVED'
    result=deepcopy(candidate)
    result.update(subject=next(iter(disclosed)) if len(disclosed)==1 else None,
        information_subjects=sorted(disclosed),author=author,action_actors=sorted(set(supported_actors)),
        action_actor_review={'proposed':actors,'unresolved':sorted(set(actors)-set(supported_actors))},
        subject_evidence=evidence,
        attribution={'contract_version':1,'policy_version':POLICY_VERSION,'version':version,
            'state':state,'unresolved':unknown or ([] if state=='RESOLVED' else ['Incomplete subject coverage']),
            'source_digest':digest(source),'model_subject':decision.get('subject'),
            'model_subjects':proposed,'reason':decision.get('reason','Source disclosure review')})
    # Metadata must not add a private association absent from the exact fact.
    result['topics']=[t for t in result.get('topics',[]) if t.casefold() in result['quote'].casefold()]
    return result


def audit_candidate(candidate):
    if candidate is None:return None
    return {**{k:deepcopy(candidate[k]) for k in ('id','source_id','spans','author','action_actors','subject',
        'information_subjects','projection_parent','projection_parent_version') if k in candidate},
        'digest':digest(candidate),'evidence_source_ids':sorted(candidate.get('subject_evidence',{})),
        'attribution_version':candidate.get('attribution',{}).get('version')}


def remember(updater,candidate,reason='Initial source attribution',previous=None):
    attr=candidate['attribution']
    updater.core.db.execute('INSERT OR IGNORE INTO attribution_history_v19 VALUES (?,?,?,?)',
        (updater.compartment,candidate['id'],attr['version'],encoded({'state':attr['state'],'candidate':audit_candidate(candidate),
            'previous_candidate':audit_candidate(previous),'reason':reason,'policy_version':POLICY_VERSION})))


def subjects(candidate):return candidate.get('information_subjects',[candidate.get('subject')])


def matches(candidate,subject):
    return subject in subjects(candidate) if 'attribution' in candidate else candidate.get('subject') in (None,subject)


def current(core,candidate,seen=None):
    attr=candidate.get('attribution',{})
    if attr.get('state')!='RESOLVED' or attr.get('policy_version')!=POLICY_VERSION:return False
    if not candidate.get('information_subjects'):return False
    if core.db.execute('SELECT 1 FROM attribution_pending_v19 WHERE candidate_id=?',(candidate['id'],)).fetchone():return False
    if not all(core._source_live(s) and core._current(s) for s in candidate.get('subject_evidence',{})):return False
    parent=candidate.get('projection_parent')
    if parent:
        seen=set(seen or ())
        if parent in seen:return False
        seen.add(parent)
        r=core.db.execute("SELECT body FROM fact_candidates WHERE id=? AND state='active'",(parent,)).fetchone()
        if not r:return False
        original=json.loads(r[0])
        if original.get('attribution',{}).get('version')!=candidate['projection_parent_version']:return False
        if original.get('attribution',{}).get('policy_version')!=POLICY_VERSION:return False
        if core.db.execute('SELECT 1 FROM attribution_pending_v19 WHERE candidate_id=?',(parent,)).fetchone():return False
        if not core._source_live(original['source_id']) or not core._current(original['source_id']):return False
        if not all(core._source_live(s) and core._current(s) for s in original.get('subject_evidence',{})):return False
    return True


def gate(core,ctx,action,row,*,visited=None,processor=None):
    policy=core.db.execute('SELECT value FROM privacy_metadata WHERE key=?',('attribution-policy:'+row['compartment'],)).fetchone()
    if not policy or policy[0]!=POLICY_VERSION:return False
    attrs=json.loads(row['attributes']);cid=attrs.get('fact_candidate_id')
    if not cid:
        if attrs.get('permission_view') or row['kind'] in {'observation','observation_view'}:return False
        # Derived artifacts inherit all input checks through the kernel's
        # dependency traversal. Bare unreviewed text is not attributed data.
        return bool(json.loads(row['dependencies']))
    r=core.db.execute("SELECT body FROM fact_candidates WHERE id=? AND state='active'",(cid,)).fetchone()
    if not r:return False
    candidate=json.loads(r[0]);version=candidate.get('attribution',{}).get('version')
    if not current(core,candidate) or attrs.get('attribution_version')!=version:return False
    if attrs.get('subject') not in subjects(candidate):return False
    marker=('disclosure',cid,ctx.principal_id,action,processor)
    if marker in (visited or ()):return True
    for subject in subjects(candidate):
        if subject==attrs.get('subject'):continue
        peers=core.db.execute('SELECT resource_id FROM attribution_views_v19 WHERE compartment=? AND candidate_id=? AND version=? AND subject=?',
            (row['compartment'],cid,version,subject)).fetchall()
        if not any(core.allowed(ctx,p[0],action,processor=processor,visited=set(visited or ())|{marker}) for p in peers):return False
    return True


def review(updater,cid,decision,*,reason):
    c=updater.core;c._session(updater.ctx,'manage_policy',updater.compartment)
    if not isinstance(reason,str) or not reason.strip():raise ValueError('Correction requires an audit reason')
    with c.transaction():
        r=c.db.execute("SELECT * FROM fact_candidates WHERE id=? AND compartment=? AND state='active'",(cid,updater.compartment)).fetchone()
        if not r:raise ValueError('Only a live candidate can be reviewed')
        before=json.loads(r['body']);sources=source_prefix(updater)
        if before['source_id'] not in sources:raise ValueError('Original source unavailable')
        if before.get('projection_parent'):raise ValueError('Review the original attribution and recreate its projection')
        version=c.db.execute('SELECT COALESCE(MAX(version),0)+1 FROM attribution_history_v19 WHERE compartment=? AND candidate_id=?',
            (updater.compartment,cid)).fetchone()[0]
        after=compile_attribution(updater,before,sources[before['source_id']],sources,decision,version)
        from .fact_candidates import segments
        text=sources[before['source_id']]['text']
        permitted=segments(text,version=updater.version)
        if len(before['spans'])!=1 or before['spans'][0] not in permitted or before['quote']!=text[before['spans'][0][0]:before['spans'][0][1]]:
            after['attribution']['state']='UNRESOLVED'
            after['attribution']['unresolved'].append('Original selector omits source dependency context; requires fresh complete extraction')
        remember(updater,after,reason,before)
        c.db.execute('UPDATE fact_candidates SET body=? WHERE id=?',(encoded(after),cid))
        c.db.execute('DELETE FROM attribution_pending_v19 WHERE compartment=? AND candidate_id=?',(updater.compartment,cid))
        c.db.execute('UPDATE privacy_metadata SET value=CAST(value AS INTEGER)+1 WHERE key=?',('attribution-revision:'+updater.compartment,))
        # Current decisions fingerprint the full candidate. Also discard durable
        # repair work/tickets so a previous model response cannot republish it.
        for cached in c.db.execute('SELECT job_id,key,body FROM fact_processing WHERE job_id IN (SELECT id FROM permission_jobs WHERE compartment=?)',
                (updater.compartment,)).fetchall():
            if any(encoded(ref) in cached['body'] for ref in (cid,before['source_id'])):
                c.db.execute('DELETE FROM fact_processing WHERE job_id=? AND key=?',(cached['job_id'],cached['key']))
        c.db.execute('DELETE FROM fact_processing WHERE job_id=? AND key LIKE ?',
            ('incremental-reviews:'+updater.compartment,cid+':%'))
        c._tickets.clear()
        return after


def request_review(updater,cid,*,reason):
    """Immediately protect a confidently wrong current record before repair."""
    c=updater.core;c._session(updater.ctx,'manage_policy',updater.compartment)
    if not isinstance(reason,str) or not reason.strip():raise ValueError('Review request needs an audit reason')
    with c.transaction():
        r=c.db.execute("SELECT body FROM fact_candidates WHERE compartment=? AND id=? AND state='active'",(updater.compartment,cid)).fetchone()
        if not r:raise ValueError('Unknown live candidate')
        before=json.loads(r[0])
        version=c.db.execute('SELECT COALESCE(MAX(version),0)+1 FROM attribution_history_v19 WHERE compartment=? AND candidate_id=?',
            (updater.compartment,cid)).fetchone()[0]
        c.db.execute('INSERT OR REPLACE INTO attribution_pending_v19 VALUES (?,?,?)',(updater.compartment,cid,reason))
        c.db.execute('INSERT INTO attribution_history_v19 VALUES (?,?,?,?)',
            (updater.compartment,cid,version,encoded({'state':'REVIEW_REQUIRED','reason':reason,
                'previous_candidate':audit_candidate(before),'policy_version':POLICY_VERSION})))
        c.db.execute('UPDATE privacy_metadata SET value=CAST(value AS INTEGER)+1 WHERE key=?',('attribution-revision:'+updater.compartment,))
        c._tickets.clear()
        return {'candidate_id':cid,'state':'REVIEW_REQUIRED','version':version}


def project(updater,cid,span,decision,*,reason):
    from . import fact_candidates as facts
    c=updater.core;c._session(updater.ctx,'manage_policy',updater.compartment)
    with c.transaction():
        r=c.db.execute("SELECT * FROM fact_candidates WHERE id=? AND compartment=? AND state='active'",(cid,updater.compartment)).fetchone()
        if not r:raise ValueError('Unknown projection parent')
        parent=json.loads(r['body']);sources=source_prefix(updater);source=sources.get(parent['source_id'])
        if (not source or parent.get('projection_parent') or parent.get('attribution',{}).get('policy_version')!=POLICY_VERSION
                or c.db.execute('SELECT 1 FROM attribution_pending_v19 WHERE candidate_id=?',(cid,)).fetchone()):
            raise ValueError('Projection requires a reviewed current original source')
        if not isinstance(span,(list,tuple)) or len(span)!=2 or any(type(v) is not int for v in span):raise ValueError('Exact span required')
        a,b=span;text=source['text']
        if not any(left<=a<b<=right for left,right in parent['spans']):raise ValueError('Projection crosses parent boundary')
        # Closed independent positive clauses. Reject global conditions,
        # negation, anaphora and identity-comparison/routing associations.
        if re.search(r'\b(?:if|unless|except|not|only|same|other|that|this|they|them|his|her|their|it)\b',source['text'],re.I):
            raise ValueError('Cannot detach relevant qualifiers or associations')
        clauses=[]
        for m in re.finditer(r'[^;]+',parent['quote']):
            raw=m.group();start=m.start()+len(raw)-len(raw.lstrip());end=m.end()-len(raw)+len(raw.rstrip())
            value=parent['quote'][start:end]
            if not re.fullmatch(identities.NAME+r"'s [a-z][a-z -]* (?:is|was|are|were|remains) [^;]+[.]?",value):
                raise ValueError('Projection needs independently named complete assertions')
            clauses.append([parent['spans'][0][0]+start,parent['spans'][0][0]+end])
        if [a,b] not in clauses:raise ValueError('Projection must select one exact complete clause')
        child=deepcopy(parent);child.update(id='fact-projection:'+digest([cid,parent['attribution']['version'],a,b]),
            spans=[[a,b]],quote=text[a:b],topics=[],relationships=[],projection_parent=cid,
            projection_parent_version=parent['attribution']['version'])
        child['source_ref']={**child['source_ref'],'character_spans':[[a,b]]}
        child['context_ref']={'observation_id':parent['source_id'],'character_spans':[[a,b]]}
        # Parent subject is a lower bound for the whole record, not an extra
        # subject silently attached to every independent projected clause.
        projected_source=deepcopy(source);projected_source['source']['subject']=None
        child=compile_attribution(updater,child,projected_source,sources,decision)
        child['attribution']['source_digest']=digest(source)
        if child['attribution']['state']!='RESOLVED':raise ValueError('Projection attribution remains unresolved')
        c.db.execute('INSERT OR IGNORE INTO fact_candidates VALUES (?,?,?,?,?,?)',
            (child['id'],updater.compartment,parent['source_id'],encoded(child),'active',r['job_id']))
        remember(updater,child,reason)
        c.db.execute('UPDATE privacy_metadata SET value=CAST(value AS INTEGER)+1 WHERE key=?',('attribution-revision:'+updater.compartment,))
        c._tickets.clear()
        return child
