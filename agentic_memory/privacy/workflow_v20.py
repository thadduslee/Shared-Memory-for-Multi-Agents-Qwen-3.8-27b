"""Versioned host scheduling and contracts. No application vocabulary or grants.

The host calls service_pending between foreground operations. Recovery budgets
are durable; query text is never authority for recovery or permission creation.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import time
from pathlib import Path
import threading

from .system import digest, encoded

PROMPT_VERSION = 'permission-inference-v20-bounded-host-recovery-v2'
SOURCE_SECONDS = 30.0
ANSWER_SECONDS = 20.0
PUBLICATION_RESERVE_SECONDS = .25
PENDING_SECONDS = 5.0
RECOVERY_ATTEMPTS = 2
INSTRUCTION = '''
Identity IDs, never display names, identify people. Identical names can identify
different people. The speaker is not automatically the information subject.
information_subjects lists EVERY person whose information the COMPLETE segment
discloses, including sensitive identity links. Use [] when attribution is unknown;
questions/context have []. Never classify another person's condition as a worker's
personal information merely because the worker recorded or repeated it.
An accepted service is defined by its original worker interaction and authenticated
subject acceptance, not just the first question's topic or a generated field label.
Include necessary responsive results, instructions, changes and follow-through of
THAT same service. Explicit source field lists, exclusions, recipient limits and
lifetimes prevail. Roles, source summaries and queries never confer permission.
Reuse existing ownership grants for self access; their IDs are not activity IDs.
Do not emit a new service operation just because a person asks an ordinary follow-up.
Use ONLY legal references supplied for each operation. Invalid references must be
reassessed, never replaced by a guessed or broader grant. Host allocates new IDs.
When OWNS already supplies self access, do not create another self activity.
For newly accepted worker access use service with i="new", actual worker p,
subject s, original worker receipt q, and current subject acceptance e. Reuse an
existing worker grant when its original service already covers this interaction.
An activity i is a SELF grant reference; never copy a worker grant into that field.
The main call has b=[]; only the separate indexed focused review publishes facts.
'''
BINDING_INSTRUCTION = '''
For each pair check the ORIGINAL accepted-service receipts, not a descriptive
initial-question label. The worker can read necessary results and follow-through
of that service, but no unrelated service, person or explicitly excluded field.
information_subjects is exhaustive for the complete fact. Review the information
about this assignment's subject independently. The host requires a positive,
current grant/review for EVERY disclosed subject before releasing the whole fact.
Independently verify that the candidate's subject list is COMPLETE and correct
against source_context. If another person's information is omitted or attributed
to its speaker by mistake, return unattributed/outside, never allow. A worker's
own documentation is not that worker's personal information about other people.
Unknown attribution never fits a grant. Queries and another reader's access grant
nothing. All restrictions, revocations, erasure and final-release checks apply.
Return ONLY indexed b slots, one per pair_context index, including negative
decisions. Each slot has r (short reason, under 60 characters), d (allow, outside,
unattributed, or unattributed_in_scope), and optional deny (actual restriction IDs).
Never copy quotes or IDs into slots. Check the complete source_context and exact
original authorization receipts for each independent pair. A closed field set
is a ceiling, not a requirement to include every allowed field in a fact.
An ownership grant applies only to its owner's exhaustively attributed personal
information; it does not turn another person's information into the speaker's.
Do not infer attribution from an ID or a role. A self-service label is descriptive;
responsive continuing instructions can belong to that same accepted service.
'''


def review_limit(updater):
    """Bound output and prompt growth to the remaining serial processing window."""
    deadline=getattr(updater.processor,'_deadline',None)
    if deadline is None:return 8
    remaining=max(0.,deadline-time.monotonic())
    return min(8,max(1,int((remaining-1.)/1.25)))


def enabled(value):
    version = str(getattr(value, 'version', value)).split('-', 1)[0]
    return version.isdigit() and int(version) >= 20


@contextmanager
def limits(processor, *, seconds=None, deadline=None):
    """Scoped serial processor deadline; restored even after provider failures."""
    previous = getattr(processor, '_deadline', None)
    current = deadline if deadline is not None else time.monotonic() + seconds
    if previous is not None:
        current = min(current, previous)
    processor._deadline = current
    try:
        yield current
    finally:
        if previous is None:
            del processor._deadline
        else:
            processor._deadline = previous


@contextmanager
def source_scope(processor,job_id):
    previous=getattr(processor,'_source_job_id',None)
    processor._source_job_id=job_id
    try:yield
    finally:
        if previous is None:del processor._source_job_id
        else:processor._source_job_id=previous


@contextmanager
def publication_lock(updater):
    """Serialize a compartment across local connections/processes, within budget.

    Advisory locks are released by the OS after a crash. Do not unlink lock files:
    another process may still own that inode. SQLite stores must be local.
    """
    from .processors import ProcessorDeadlineFailure
    import fcntl
    c=updater.core
    deadline=getattr(updater.processor,'_deadline',None) or time.monotonic()+SOURCE_SECONDS
    if not c._lock.acquire(timeout=max(0.,deadline-time.monotonic())):
        raise ProcessorDeadlineFailure('Compartment publication is busy')
    locks=getattr(c,'_v20_publication_locks',{})
    c._v20_publication_locks=locks
    key=(updater.compartment,threading.get_ident())
    if key in locks:
        try:yield
        finally:c._lock.release()
        return
    path=Path(str(c.privacy_path)+'.v20-'+digest(updater.compartment)[:16]+'.lock')
    try:
        with path.open('a+b') as handle:
            while True:
                try:
                    fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise ProcessorDeadlineFailure('Compartment publication is busy')
                    time.sleep(min(.01,remaining))
            locks[key]=handle
            try:yield
            finally:
                del locks[key]
                fcntl.flock(handle,fcntl.LOCK_UN)
    finally:c._lock.release()


def semantic_context(assignment, state):
    """Fingerprint meaning and original authority, excluding growing diagnostics."""
    fields = ('id', 'participant', 'subject', 'authority', 'basis', 'ownership',
              'actions', 'information_scope', 'status', 'policy_hash',
              'activity_id', 'self_use_activity', 'scope_evidence', 'accepted_service')
    a = {k: deepcopy(assignment[k]) for k in fields if k in assignment}
    activity = state['activity'].get(a.get('activity_id') or a.get('self_use_activity'))
    if activity:
        activity = {k: v for k, v in activity.items() if k not in
                    {'explanation', 'recipient_access', 'bindings', 'binding_count'}}
    restrictions = {rid: {k: v for k, v in r.items() if k not in {'explanation', 'reason'}}
                    for rid, r in state['restriction'].items()
                    if r['subject'] == a['subject'] and a['participant'] in r['participants']
                    and set(a['actions']) & set(r['actions'])}
    return ','.join(encoded(v) for v in (a, activity, restrictions))


def subjects(candidate):
    return candidate.get('subjects', [candidate['subject']] if candidate.get('subject') else [])


def whole_fact_allowed(core, compartment, candidate, reader, state):
    """Conjunctive semantic receipts for all disclosed people, before model use."""
    from .incremental import active, pair_key
    from .control_holds import blocks
    required = subjects(candidate)
    if not required:
        return False
    sources = {r[0]: None for r in core.db.execute(
        "SELECT id FROM resources WHERE compartment=? AND kind='observation'", (compartment,))
        if core._source_live(r[0]) and core._current(r[0])}
    class Host:
        pass
    host = Host()
    host.core = core
    host._evidence = lambda a: set(s for values in a.get('evidence', {}).values() for s in values)
    for subject in required:
        from .fact_candidates import erased
        if erased(core,compartment,subject,candidate['quote']):
            return False
        if blocks(core, compartment, subject):
            return False
        allowed = False
        for aid, a in state['assignment'].items():
            if a['subject'] != subject or a['participant'] != reader or 'read' not in a['actions']:
                continue
            if not active(host, a, state, sources):
                continue
            row = core.db.execute('SELECT snapshot,body FROM fact_processing WHERE job_id=? AND key=?',
                ('incremental-reviews:' + compartment, candidate['id'] + ':' + aid)).fetchone()
            if not row or row['snapshot'] != pair_key(candidate, a, state):
                continue
            d = json.loads(row['body'])
            if d.get('in_scope') is True and d.get('subject_supported') is True and d.get('blocked_by') == []:
                allowed = True
                break
        if not allowed:
            return False
    return True


def setup(updater):
    # Additive tables are created only when an explicitly selected v20 updater opens.
    c = updater.core
    c.db.execute('''CREATE TABLE IF NOT EXISTS privacy.host_recovery(
        id TEXT PRIMARY KEY, compartment TEXT NOT NULL, source_job TEXT NOT NULL,
        kind TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        queued REAL NOT NULL, body TEXT NOT NULL)''')
    c.db.execute('''CREATE TABLE IF NOT EXISTS privacy.workflow_timings(
        id INTEGER PRIMARY KEY, compartment TEXT NOT NULL, stage TEXT NOT NULL,
        seconds REAL NOT NULL, body TEXT NOT NULL)''')


def record(updater, stage, seconds, **details):
    with updater.core.transaction():
        updater.core.db.execute('INSERT INTO workflow_timings(compartment,stage,seconds,body) VALUES (?,?,?,?)',
            (updater.compartment, stage, seconds, encoded(details)))


def enqueue(updater, job_id, *, kind='control'):
    c = updater.core
    c._session(updater.ctx, 'manage_policy', updater.compartment)
    key = 'host-recovery:' + digest([job_id, kind])
    with c.transaction():
        c.db.execute('INSERT OR IGNORE INTO host_recovery VALUES (?,?,?,?,?,0,?,?)',
            (key, updater.compartment, job_id, kind, 'pending', time.time(), '{}'))
    return key


def defer_source(updater, job_id, error):
    """A deadline is not permission to discard a possibly adverse source event."""
    from .control_holds import quarantine
    c=updater.core
    with c.transaction():
        job=c.db.execute('SELECT * FROM permission_jobs WHERE id=?',(job_id,)).fetchone()
        validation={'accepted':False,'reason':str(error),'unresolved_access_hold':True,
                    'error':type(error).__name__,'foreground_deferred':True}
        validation['control_hold_ids']=quarantine(updater,job,validation)
        c.db.execute("UPDATE permission_jobs SET status='quarantined',validation=? WHERE id=?",
                     (encoded(validation),job_id))
        enqueue(updater,job_id)
    return {'status':'quarantined','changes':[]}


def note_binding_failure(updater,required,reason):
    with updater.core.transaction():
        for pair in required:
            key=pair['candidate_id']+':'+pair['assignment_id']
            row=updater.core.db.execute('SELECT snapshot,body FROM fact_processing WHERE job_id=? AND key=?',
                ('v20-binding-failures:'+updater.compartment,key)).fetchone()
            attempts=json.loads(row['body']).get('attempts',0) if row and row['snapshot']==pair['fingerprint'] else 0
            updater.core.db.execute('INSERT OR REPLACE INTO fact_processing VALUES (?,?,?,?)',
                ('v20-binding-failures:'+updater.compartment,key,pair['fingerprint'],
                 encoded({'attempts':min(2,attempts+1),'reason':reason,
                          'candidate_id':pair['candidate_id'],'assignment_id':pair['assignment_id']})))


def reviewable(updater,required):
    failed={r['key']:r for r in updater.core.db.execute('SELECT key,snapshot,body FROM fact_processing WHERE job_id=?',
        ('v20-binding-failures:'+updater.compartment,))}
    return [p for p in required if p['candidate_id']+':'+p['assignment_id'] not in failed or
        failed[p['candidate_id']+':'+p['assignment_id']]['snapshot']!=p['fingerprint'] or
        json.loads(failed[p['candidate_id']+':'+p['assignment_id']]['body']).get('attempts',0)<2]


def pending(updater):
    return [dict(r) for r in updater.core.db.execute(
        "SELECT * FROM host_recovery WHERE compartment=? ORDER BY CASE kind WHEN 'control' THEN 0 ELSE 1 END,queued,id",
        (updater.compartment,))]


def recovery_available(updater,item):
    """The two extra attempts belong to the source event, not each queue kind."""
    used=updater.core.db.execute('''SELECT coalesce(sum(h.attempts),0) FROM host_recovery h
        JOIN permission_jobs j ON j.id=h.source_job WHERE h.compartment=? AND j.source_id=
        (SELECT source_id FROM permission_jobs WHERE id=?)''',(updater.compartment,item['source_job'])).fetchone()[0]
    if used<RECOVERY_ATTEMPTS:return True
    with updater.core.transaction():
        updater.core.db.execute("UPDATE host_recovery SET status='needs_review',body=? WHERE id=?",
            (encoded({'reason':'source_recovery_budget_exhausted'}),item['id']))
        updater.core.db.execute("""UPDATE permission_jobs SET status='quarantined' WHERE id LIKE 'host-recovery:%'
            AND status!='complete' AND source_id=(SELECT source_id FROM permission_jobs WHERE id=?)""",(item['source_job'],))
    return False


def latest_source(updater,source_id):
    return not updater.core.db.execute("""SELECT 1 FROM resources WHERE compartment=? AND kind='observation'
        AND rowid>(SELECT rowid FROM resources WHERE id=?) LIMIT 1""",(updater.compartment,source_id)).fetchone()


def classify(updater,context,bootstrap,instruction,payload,schema):
    """Read-only recovery uses opaque identity references, just like ingestion."""
    from .incremental import remap,opaque_schema
    mapping={pid:'p'+str(i+1) for i,pid in enumerate(bootstrap.participants)}
    mapping.update({sid:'resource'+str(i+1) for i,sid in enumerate(bootstrap.resources) if sid not in mapping})
    mapping.update({sid:'s'+str(i+1) for i,sid in enumerate(context)})
    result=updater.core.process(updater.ctx,updater.processor,list(context),instruction,
        remap(payload,mapping),schema=opaque_schema(schema,mapping))
    updater.core._tickets.pop(id(result),None)
    reverse={v:k for k,v in mapping.items()}
    result=remap(result,reverse)
    # Compact control decoding keeps sN aliases for its own receipt resolver.
    # These read-only classifiers have no compact decoder; restore them here.
    def receipts(value,key=None):
        if isinstance(value,str) and key in {'source_id','subject_source'}:return reverse.get(value,value)
        if isinstance(value,dict):return {k:receipts(v,k) for k,v in value.items()}
        if isinstance(value,list):return [receipts(v,key) for v in value]
        return value
    return receipts(result)


def timing_summary(core):
    """Report wall times and all model work without hiding deferred costs."""
    import math
    from collections import defaultdict,Counter
    groups=defaultdict(list)
    phases=Counter()
    tokens=Counter()
    cost=0.
    retries=0
    foreground=Counter()
    unknown_usage=0
    wall_total=0.
    for r in core.db.execute('SELECT stage,seconds,body FROM workflow_timings ORDER BY id'):
        groups[r['stage']].append(r['seconds'])
        d=json.loads(r['body'])
        if r['stage']!='model_call' and not d.get('charged_to_answer'):wall_total+=r['seconds']
        if r['stage']=='model_call':
            phases[d.get('phase','foreground')]+=1
            usage=d.get('usage') or {}
            if d.get('usage') is None:unknown_usage+=1
            if d.get('phase')=='foreground' and d.get('source_job_id') and d.get('error')!='ProcessorBudgetFailure':
                foreground[d['source_job_id']]+=1
            cost+=usage.get('cost',0) or 0
            for key in ('prompt_tokens','completion_tokens'):tokens[key]+=usage.get(key,0) or 0
            retries+=max(0,d.get('transport_attempts',1)-1)
    for r in core.db.execute("SELECT seconds FROM pipeline_timings WHERE stage='source_with_host'"):
        groups['source_including_host'].append(r[0])
    for r in core.db.execute("SELECT seconds FROM pipeline_timings WHERE stage='source_total'"):
        groups['source_foreground'].append(r[0])
    def stats(values):
        values=sorted(values)
        return {'count':len(values),'total':sum(values),'mean':sum(values)/len(values),
            'p50':values[math.ceil(.5*len(values))-1],
            'p90':values[math.ceil(.9*len(values))-1],
            'p95':values[math.ceil(.95*len(values))-1],'max':values[-1]}
    return {'latency_seconds':{k:stats(v) for k,v in groups.items()},
            'model_calls_by_phase':dict(phases),'model_tokens':dict(tokens),
            'model_cost':cost,'unreported_usage_calls':unknown_usage,'transport_retries':retries,
            'max_foreground_model_calls_per_source':max(foreground.values(),default=0),
            'recovery_jobs':{r[0]:r[1] for r in core.db.execute('SELECT status,count(*) FROM host_recovery GROUP BY status')},
            'total_work_seconds':wall_total}


def service_pending(updater, *, seconds=5.0, requester_id=None, query=None, controls=True):
    """One host scheduling opportunity, never an unbounded queue drain.

    Query-time calls may service bindings only; historical controls require a
    separate trusted host tick. Calls are serialized with foreground publication.
    """
    if not 0 <= seconds <= SOURCE_SECONDS:
        raise ValueError('Host scheduling budget must be between zero and 30 seconds')
    c = updater.core
    c._session(updater.ctx, 'manage_policy', updater.compartment)
    if not seconds:
        return {'status': 'deferred', 'batches': 0, 'changes': []}
    started = time.monotonic()
    previous_phase=getattr(updater.processor,'_workflow_phase',None)
    updater.processor._workflow_phase='host'
    result={'status':'failed','calls':0}
    jobs=[]
    try:
      with limits(updater.processor, seconds=seconds), publication_lock(updater):
        jobs = [r for r in pending(updater) if r['status'] == 'pending' and
                (controls or r['kind'] != 'control')]
        if jobs:
            result = recover_one(updater, jobs[0])
        else:
            previous = getattr(updater, '_review_priority', None)
            updater._review_priority = (requester_id, query)
            try:
                result = updater.flush_bindings(max_batches=1)
            finally:
                updater._review_priority = previous
    except Exception as exc:
        from .processors import ProcessorBudgetFailure
        if jobs and isinstance(exc,ProcessorBudgetFailure):
            # A declined payment request is not a semantic recovery attempt.
            with c.transaction():
                c.db.execute("UPDATE host_recovery SET attempts=?,status='pending',body=? WHERE id=?",
                    (jobs[0]['attempts'],encoded({'paused_budget':True}),jobs[0]['id']))
        raise
    finally:
        record(updater, 'host_recovery' if jobs else 'host_maintenance', time.monotonic() - started,
               result=result['status'], calls=result.get('calls', result.get('batches', 0)),
               charged_to_answer=not controls,
               queue_delay_seconds=max(0.,time.time()-jobs[0]['queued']) if jobs else 0.)
        if previous_phase is None:del updater.processor._workflow_phase
        else:updater.processor._workflow_phase=previous_phase
    return result


def recover_one(updater, item):
    """Reassess original event with separate durable attempts and immutable history."""
    from .permissions import PermissionRepairExhausted, PermissionProposalRejected,PermissionRevisionConflict
    from .processors import ModelContractError, ProcessorFailure
    c = updater.core
    if not recovery_available(updater,item):
        return {'status':'needs_review','calls':0,'changes':[]}
    if item['kind'] == 'attribution':
        return recover_attribution(updater, item)
    if item['kind'] == 'extraction':
        return recover_extraction(updater,item)
    original = c.db.execute('SELECT rowid AS sequence,* FROM permission_jobs WHERE id=?',
                           (item['source_job'],)).fetchone()
    if not original or original['version'] != updater.version:
        raise ValueError('Recovery source must belong to the active version')
    attempt = item['attempts'] + 1
    with c.transaction():
        # Crash recovery never reuses an admitted attempt or leaves a stranded
        # running child blocking unrelated reads indefinitely.
        c.db.execute("UPDATE permission_jobs SET status='quarantined' WHERE id LIKE ? AND status!='complete'",
                     (item['id']+':attempt:%',))
    if attempt > RECOVERY_ATTEMPTS:
        with c.transaction():
            c.db.execute("UPDATE host_recovery SET status='needs_review' WHERE id=?",(item['id'],))
        return {'status': 'needs_review', 'calls': 0, 'changes': []}
    # A separate job preserves the exhausted original attempt ledger. Each host
    # tick allows one semantic attempt, and persists admission before any I/O.
    jid = item['id'] + ':attempt:' + str(attempt)
    with c.transaction():
        c.db.execute('UPDATE host_recovery SET attempts=?,body=? WHERE id=?',
            (attempt, encoded({'admitted_at': time.time()}), item['id']))
        c.db.execute('''INSERT OR IGNORE INTO permission_jobs
            (id,compartment,source_id,version,request_hash,status,inputs,model,prompt_version)
            VALUES (?,?,?,?,?,'pending',?,?,?)''',
            (jid, updater.compartment, original['source_id'], updater.version,
             digest([original['request_hash'], item['id'], attempt]), original['inputs'],
             original['model'], original['prompt_version']))
        previous=c.db.execute('SELECT validation FROM permission_attempts WHERE job_id=? ORDER BY attempt DESC LIMIT 1',(jid,)).fetchone()
        if previous and json.loads(previous[0] or '{}').get('exception_type')=='ProcessorBudgetFailure':
            c.db.execute("UPDATE permission_jobs SET status='pending' WHERE id=? AND status='quarantined'",(jid,))
    updater._recovering_source = original['source_id']
    updater._recovery_original_sequence = original['sequence']
    updater._recovery_original_job=original['id']
    updater._recovery_item_id=item['id']
    updater._control_reassessment_only=True
    updater._source_call_limit = 1
    try:
        result = updater.update(jid, max_attempts=1)
        if result['status']!='complete':
            raise PermissionRepairExhausted('Reassessment did not complete',{'job_id':jid})
        return {**result, 'calls': 1}
    except (PermissionProposalRejected, PermissionRevisionConflict, ModelContractError) as exc:
        with c.transaction():
            c.db.execute("UPDATE permission_jobs SET status='quarantined' WHERE id=?", (jid,))
            c.db.execute('UPDATE host_recovery SET status=?,body=? WHERE id=?',
                ('needs_review' if attempt >= RECOVERY_ATTEMPTS else 'pending',
                 encoded({'error': str(exc), 'reassessment_job': jid}), item['id']))
        return {'status': 'needs_review' if attempt >= RECOVERY_ATTEMPTS else 'pending',
                'calls': 1, 'changes': []}
    except ProcessorFailure:
        with c.transaction():
            c.db.execute("UPDATE permission_jobs SET status='quarantined' WHERE id=?", (jid,))
            c.db.execute('UPDATE host_recovery SET status=? WHERE id=?',
                         ('needs_review' if attempt >= RECOVERY_ATTEMPTS else 'pending', item['id']))
        raise
    finally:
        for key in ('_recovering_source', '_recovery_original_sequence', '_source_call_limit',
                    '_recovery_original_job','_recovery_item_id','_control_reassessment_only'):
            delattr(updater, key)


def finish_recovery(updater,jid):
    """Called INSIDE the reassessment publication transaction, never afterwards."""
    c=updater.core
    assert c.db.in_transaction
    for h in c.db.execute('SELECT id,body FROM permission_control_holds WHERE job_id=?',
                          (updater._recovery_original_job,)).fetchall():
        body=json.loads(h['body'])
        body['host_reassessment']={'job_id':jid,'source_id':updater._recovering_source}
        c.db.execute("UPDATE permission_control_holds SET state='host_resolved',body=? WHERE id=?",
                     (encoded(body),h['id']))
    c.db.execute("UPDATE host_recovery SET status='complete',body=? WHERE id=?",
                 (encoded({'reassessment_job':jid}),updater._recovery_item_id))


def validate_recovery_controls(updater, canonical):
    """Never publish an old control delta on top of later policy changes."""
    sequence = getattr(updater, '_recovery_original_sequence', None)
    if sequence is None or not canonical['operations']:
        return
    for row in updater.core.db.execute('''SELECT proposal FROM permission_jobs
            WHERE compartment=? AND rowid>? AND status='complete' AND id NOT LIKE 'host-recovery:%' ''',
            (updater.compartment, sequence)):
        if json.loads(row[0] or '{}').get('operations'):
            from .permissions import PermissionProposalRejected
            raise PermissionProposalRejected('Historical control delta requires review after later policy publication',
                                            {'stale_result': True})


def recover_attribution(updater, item):
    """One bounded original-source attribution review; no controls or new grants."""
    from .processors import ModelContractError
    from . import fact_candidates as facts
    c = updater.core
    job = c.db.execute('SELECT * FROM permission_jobs WHERE id=?', (item['source_job'],)).fetchone()
    if not latest_source(updater,job['source_id']):
        with c.transaction():
            c.db.execute("UPDATE host_recovery SET status='needs_review',body=? WHERE id=?",
                (encoded({'reason':'historical_attribution_requires_review'}),item['id']))
        return {'status':'needs_review','calls':0,'changes':[]}
    sources = {sid: json.loads(c.db.execute('SELECT payload FROM resources WHERE id=?', (sid,)).fetchone()[0])
               for sid in json.loads(job['inputs']) if c._source_live(sid) and c._current(sid)}
    candidates = [p for p in facts.rows(updater, prefix=sources)
                  if p['source_id']==job['source_id'] and not subjects(p)]
    candidates = candidates[:4]
    if not candidates:
        with c.transaction():
            c.db.execute("UPDATE host_recovery SET status='complete' WHERE id=?", (item['id'],))
        return {'status': 'complete', 'calls': 0, 'changes': []}
    attempt = item['attempts'] + 1
    if attempt>RECOVERY_ATTEMPTS:
        with c.transaction():
            c.db.execute("UPDATE host_recovery SET status='needs_review' WHERE id=?",(item['id'],))
        return {'status':'needs_review','calls':0,'changes':[]}
    boot = updater._source_bootstrap(sources)
    context={sid:sources[sid] for sid in list(sources)[-6:]}
    slots = {str(i): {'type': 'object', 'additionalProperties': False, 'properties': {
        'subjects': {'type': 'array', 'uniqueItems': True, 'maxItems': 8,
                     'items': {'type': 'string', 'enum': list(boot.resources)}},
        'source_id': {'type': 'string', 'enum': list(context)}},
        'required': ['subjects', 'source_id']} for i in range(len(candidates))}
    with c.transaction():
        c.db.execute('UPDATE host_recovery SET attempts=? WHERE id=?', (attempt, item['id']))
    try:
        context={sid:sources[sid] for sid in list(sources)[-6:]}
        result = classify(updater,context,boot,
            'Attribute each complete fact to EVERY disclosed person using authenticated source context. '
            'Names are not unique identities. Speaker is not necessarily subject. Unknown means []. '
            'This review cannot change permissions. Return only the supplied slots.',
            {'stage': 'attribution_recovery', 'permission_version': updater.version,
             'candidates': candidates, 'sources': context, 'resources': boot.resources},
            {'type': 'object', 'properties': slots, 'required': list(slots), 'additionalProperties': False})
        import jsonschema
        try:jsonschema.validate(result, {'type': 'object', 'properties': slots, 'required': list(slots), 'additionalProperties': False})
        except jsonschema.ValidationError as exc:raise ModelContractError('Invalid attribution review') from exc
        with c.transaction():
            for i, candidate in enumerate(candidates):
                d = result[str(i)]
                if not d['subjects']:
                    continue
                if facts.erased(c, updater.compartment, d['subjects'][0], candidate['quote']):
                    continue
                candidate.update(subject=d['subjects'][0], subjects=d['subjects'],
                                 subject_evidence={d['source_id']: sources[d['source_id']]['text']})
                c.db.execute('UPDATE fact_candidates SET body=? WHERE id=? AND state=\'active\'',
                             (encoded(candidate), candidate['id']))
            complete = not any(not subjects(p) and p['source_id']==job['source_id'] for p in facts.rows(updater,prefix=sources))
            status = 'complete' if complete else 'pending' if recovery_available(updater,item) else 'needs_review'
            c.db.execute('UPDATE host_recovery SET status=?,body=? WHERE id=?',
                         (status, encoded({'reviewed': len(candidates)}), item['id']))
        return {'status': status, 'calls': 1, 'changes': []}


    except (ValueError, ModelContractError) as exc:
        with c.transaction():
            status = 'pending' if recovery_available(updater,item) else 'needs_review'
            c.db.execute('UPDATE host_recovery SET status=?,body=? WHERE id=?',
                         (status, encoded({'error': str(exc)}), item['id']))
        return {'status': status, 'calls': 1, 'changes': []}


def recover_extraction(updater,item):
    """Read-only missing-field classification; cannot reinterpret controls."""
    from . import fact_candidates as facts,incremental
    from .processors import ModelContractError
    c=updater.core
    job=c.db.execute('SELECT * FROM permission_jobs WHERE id=?',(item['source_job'],)).fetchone()
    if not latest_source(updater,job['source_id']):
        with c.transaction():
            c.db.execute("UPDATE host_recovery SET status='needs_review',body=? WHERE id=?",
                (encoded({'reason':'historical_extraction_requires_review'}),item['id']))
        return {'status':'needs_review','calls':0,'changes':[]}
    sources={sid:json.loads(c.db.execute('SELECT payload FROM resources WHERE id=?',(sid,)).fetchone()[0])
        for sid in json.loads(job['inputs']) if c._source_live(sid) and c._current(sid)}
    if job['source_id'] not in sources:
        with c.transaction():c.db.execute("UPDATE host_recovery SET status='needs_review' WHERE id=?",(item['id'],))
        return {'status':'needs_review','calls':0,'changes':[]}
    receipt=c.db.execute('SELECT body FROM fact_extractions WHERE job_id=?',(job['id'],)).fetchone()
    accepted=json.loads(receipt[0]).get('accepted_segments',{}) if receipt else {}
    spans=facts.segments(sources[job['source_id']]['text'])
    missing=[i for i in range(len(spans)) if str(i) not in accepted][:4]
    context={sid:sources[sid] for sid in list(sources)[-6:]}
    boot=updater._source_bootstrap(sources)
    targets=facts.rows(updater,prefix=sources)[-32:]
    for candidate in targets:
        for sid in [candidate['source_id'],*candidate['subject_evidence']]:
            if sid in sources:context.setdefault(sid,sources[sid])
    slot=facts.obj({'kind':facts.enum(['asserted_fact','instruction','question','context']),
        'subject':facts.enum(boot.resources,True),
        'subjects':{'type':'array','maxItems':8,'uniqueItems':True,'items':facts.enum(boot.resources)},
        'subject_source':facts.enum(context,True),
        'reason':{'type':'string','minLength':1,'maxLength':120},
        'topics':{'type':'array','maxItems':8,'items':{'type':'string','minLength':1,'maxLength':80}},
        'relationships':{'type':'array','maxItems':8,'items':facts.obj({
            'kind':facts.enum(['replaces','cancels']),
            'target_id':facts.enum([p['id'] for p in targets]) if targets else {'type':'string'}})}})
    schema=facts.obj({str(i):slot for i in missing})
    attempt=item['attempts']+1
    with c.transaction():c.db.execute('UPDATE host_recovery SET attempts=? WHERE id=?',(attempt,item['id']))
    try:
        result=classify(updater,context,boot,
            'Classify only supplied missing source segments. Facts/instructions must list EVERY disclosed subject ID '
            'and exact available attribution receipt. Speaker need not be subject. Unknown subject uses null and []. '
            'Questions remain questions. Preserve complete source meaning. Only explicit same-property corrections '
            'or cancellations have relationships; summaries do not replace facts. This read-only classification '
            'cannot change controls, create grants, or clear holds.',
            {'stage':'extraction_recovery','permission_version':updater.version,'sources':context,
             'new_source_id':job['source_id'],
             'resources':boot.resources,'relationship_targets':targets,
             'segments':{str(i):sources[job['source_id']]['text'][spans[i][0]:spans[i][1]] for i in missing}},schema)
        if not isinstance(result,dict):raise ModelContractError('Invalid extraction review')
        import jsonschema
        decisions=[]
        for i in missing:
            try:jsonschema.validate(result.get(str(i)),slot)
            except jsonschema.ValidationError:continue
            d={**result[str(i)],'index':i}
            d['subject_quote']=sources[d['subject_source']]['text'] if d['subject_source'] else None
            quote=sources[job['source_id']]['text'][spans[i][0]:spans[i][1]]
            d['relationships']=[{**r,'evidence_quote':quote} for r in d['relationships']]
            decisions.append(d)
        with c.transaction():
            remaining,_=incremental.extract(updater,job,{'segments':decisions},sources,spans,list(context))
            status='complete' if not remaining else 'pending' if recovery_available(updater,item) else 'needs_review'
            c.db.execute('UPDATE host_recovery SET status=?,body=? WHERE id=?',
                (status,encoded({'missing':remaining}),item['id']))
            if any(not subjects(p) and p['source_id']==job['source_id'] for p in facts.rows(updater,prefix=sources)):
                enqueue(updater,job['id'],kind='attribution')
        return {'status':status,'calls':1,'changes':[]}
    except (ValueError,ModelContractError) as exc:
        with c.transaction():
            status='pending' if recovery_available(updater,item) else 'needs_review'
            c.db.execute('UPDATE host_recovery SET status=?,body=? WHERE id=?',
                (status,encoded({'error':str(exc)}),item['id']))
        return {'status':status,'calls':1,'changes':[]}
