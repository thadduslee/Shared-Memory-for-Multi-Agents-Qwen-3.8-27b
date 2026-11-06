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

POLICY_VERSION='v19-cited-subjects:3'
PROMPT_VERSION='permission-inference-v19-cited-subjects-v2'
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

Decide whose information each fact is by reasoning over the whole supplied
conversation; wording alone proves nothing. source.context gives each turn's
speaker_name and speaker_role, and bootstrap.participants gives everyone's name
and role. A speaker's statements about their own situation, plans, preferences and
requests concern that speaker, however they are phrased ("Yes, please", "I can come
Tuesday", "Keep the clinical details private"). A worker's statement about the
person they are serving concerns THAT person, not the worker, even when it names
nobody ("Doxylamine is a half tablet at bedtime", "I booked Friday at 11:20").
List every such person in information_subjects and cite as attribution_source the
turn that shows who it concerns: their own turn, or a turn that names them.
Do not assign every note in a conversation to one person: when two people could be
meant and the text does not settle it, set attribution_complete=false.
mentioned_people lists each person's name exactly as it appears in the fact, and
nothing else: never medications, places, organizations, roles or products.
care_participants lists workers this fact shows actually doing care or service work
for its subject: the speaker, or a worker the fact names as covering or taking over.
Asking for information, requesting access, claiming a role, past coverage that has
ended and merely being mentioned are not involvement. information_types lists EVERY
type in information_types that describes this fact, usually one to three. A dose
change is medication; a callback instruction is contact; a changed visit is
schedule. Describe the information itself, not who should read it.
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
    """Alias -> people. A configured directory replaces self-introduction claims."""
    if bootstrap.directory:
        result={}
        for pid,person in bootstrap.directory.items():
            for alias in person.get('aliases',[]):
                result.setdefault(alias,{}).setdefault(pid,[])
        return result
    result=identities.identities(bootstrap,sources)
    for sid,body in sources.items():
        text=body['text'].replace('\u2019',"'")
        for m in re.finditer(r"\b(?:I'm|I am|My name is|This is) ("+identities.NAME+r")(?= [a-z]|[,.!?]|$)",text):
            name=m.group(1);pid=body['source']['participant']
            proof=identities.receipt(sid,body['text'],*m.span(),participant=pid)
            for alias in {identities.norm(name),identities.norm(name.split()[0])}:
                result.setdefault(alias,{}).setdefault(pid,[]).append(proof)
    return result


def mentions(text,registry,exact=False):
    """Literal lookup of known names; it interprets no wording or sentence meaning.

    Directory aliases keep their case so that "Park" never matches "park".
    """
    found=set();proofs=[];ambiguous=[];covered=[]
    text=text.replace('\u2019',"'") if exact else identities.norm(text)
    for alias in sorted(registry,key=len,reverse=True):
        for m in re.finditer(r'(?<!\w)'+re.escape(alias)+r'(?!\w)',text):
            if any(a<=m.start() and m.end()<=b for a,b in covered):continue
            covered.append(m.span());people=registry[alias]
            if len(people)!=1:ambiguous.append('Ambiguous person '+alias);continue
            pid=next(iter(people));found.add(pid);proofs.extend(people[pid])
    return found,proofs,ambiguous


def named_in_source(updater,sid,text,bootstrap):
    """Causal directory discovery, cached because source text is immutable."""
    cache=updater.__dict__.setdefault('_directory_mentions',{})
    if sid not in cache:cache[sid]=mentions(text,names(bootstrap,{}),True)[0]
    return cache[sid]


def compile_attribution(updater,candidate,source,sources,decision,version=1):
    """The model decides whose information this is; code verifies its citation."""
    boot=updater._source_bootstrap(sources);author=source['source']['participant']
    quote=candidate['quote'];source_id=candidate['source_id'];exact=bool(boot.directory)
    role=lambda person:boot.directory.get(person,{}).get('role')
    workers={p for p in boot.directory if role(p) in boot.worker_roles}
    proposed=decision.get('information_subjects')
    complete=decision.get('attribution_complete') is True
    actors=decision.get('action_actors',[])
    if not isinstance(proposed,list) or any(s not in boot.resources for s in proposed):
        proposed=[];complete=False
    if not isinstance(actors,list) or any(a not in boot.participants for a in actors):
        actors=[];complete=False
    registry=names(boot,sources)
    named,proofs,unknown=mentions(quote,registry,exact)
    for person in decision.get('mentioned_people') or []:
        # A name the fact does not contain is not evidence of anything.
        if not isinstance(person,str) or person not in quote:continue
        found,_,ambiguous=mentions(person,registry,exact)
        if not found and not ambiguous:unknown.append('Unknown person '+person)
    attested=source['source'].get('subject')
    disclosed=set(proposed)
    if attested:disclosed.add(attested)
    # A worker doing a job is not the subject of the person they are serving.
    if disclosed-workers:disclosed-=workers-{attested}
    elif disclosed=={author} and author in workers and attested!=author:
        unknown.append('Authorship/action performance is not evidence of personal information')
    # A second person of the same kind named here needs an explicit decision, and
    # a fact about only workers cannot leave out a person they are serving.
    for person in sorted(named-disclosed):
        if disclosed and (role(person) is None or any(role(person)==role(s) for s in disclosed)
                or (person not in workers and disclosed<=workers)):
            unknown.append('Unlisted person named in fact '+person)
    evidence={source_id:quote}
    evidence.update({p['source_id']:p['quote'] for p in proofs})
    cited=decision.get('subject_source');cited_quote=decision.get('subject_quote')
    if cited is not None and not (cited in sources and isinstance(cited_quote,str) and cited_quote
            and cited_quote in sources[cited]['text']):
        complete=False
    order=list(sources);position=order.index(source_id)
    window=order[max(0,position-RECENT_TURNS+1):position+1]
    def supports(person,sid):
        body=sources[sid]
        return body['source']['participant']==person or person in mentions(body['text'],registry,exact)[0]
    for person in sorted(disclosed):
        if person==attested:continue
        # An invalid or unhelpful citation costs no extra model call: the window
        # is checked next, and an unverified subject simply stays protected.
        checked=([cited] if cited in sources else [])+list(reversed(window))
        support=next((sid for sid in checked if supports(person,sid)),None)
        if support is None:unknown.append('Unanchored subject '+person);continue
        evidence.setdefault(support,sources[support]['text'])
    # Two plausible subjects stay protected unless the fact itself settles it.
    present=set()
    for sid in window:
        present.add(sources[sid]['source']['participant'])
        present.update(mentions(sources[sid]['text'],registry,exact)[0])
    for person in sorted(disclosed):
        if person==author or person in named or person==attested or role(person) is None:continue
        rivals=sorted(p for p in present-disclosed if role(p)==role(person))
        if rivals:unknown.append('Ambiguous subject '+person+'; also present '+rivals[0])
    state='RESOLVED' if complete and disclosed and not unknown else 'UNRESOLVED'
    supported_actors=[a for a in actors if a==author or a in named]
    result=deepcopy(candidate)
    result.update(subject=next(iter(disclosed)) if len(disclosed)==1 else None,
        information_subjects=sorted(disclosed),author=author,action_actors=sorted(set(supported_actors)),
        action_actor_review={'proposed':actors,'unresolved':sorted(set(actors)-set(supported_actors))},
        subject_evidence=evidence,
        attribution={'contract_version':2,'policy_version':POLICY_VERSION,'version':version,
            'state':state,'unresolved':unknown or ([] if state=='RESOLVED' else ['Incomplete subject coverage']),
            'source_digest':digest(source),'model_subject':decision.get('subject'),
            'model_subjects':proposed,'reason':decision.get('reason','Source disclosure review')})
    if boot.care_scopes:
        from .care_v19 import labels
        result['care']=labels(boot,decision,author,named,disclosed,workers)
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
