"""Authority-bounded quarantine of unprocessed control events (v15).

Scoping uses authenticated capabilities and established task relationships, never
roles or keywords in source text. Clearing requires a later authenticated control
authority statement and an explicit model assessment of the failed source.
"""
import json

from .system import digest, encoded


def rows(core, compartment):
    result=[]
    for r in core.db.execute('SELECT * FROM permission_control_holds WHERE compartment=? ORDER BY id',(compartment,)):
        h=dict(r)
        body=json.loads(h['body'])
        reassessment=body.get('host_reassessment',{})
        if h['state']=='host_resolved' and reassessment:
            job=core.db.execute("SELECT status,source_id,version,validation FROM permission_jobs WHERE id=? AND compartment=?",
                (reassessment.get('job_id'),compartment)).fetchone()
            sid=reassessment.get('source_id')
            from .workflow_v20 import enabled
            if (job and job['status']=='complete' and job['source_id']==sid and enabled(job['version'])
                    and json.loads(job['validation'] or '{}').get('control_complete') is True
                    and sid and core._source_live(sid) and core._current(sid)):continue
        resolution=body.get('resolution',{})
        sid=resolution.get('source_id')
        if h['state']=='resolved' and sid and core._source_live(sid) and core._current(sid):continue
        result.append(h)
    return result


def quarantine(updater, job, validation):
    c=updater.core
    source=json.loads(c.db.execute('SELECT payload FROM resources WHERE id=?',(job['source_id'],)).fetchone()[0])
    actor=source['source']['participant']
    sources={r['id']:json.loads(r['payload']) for r in c.db.execute(
        "SELECT id,payload FROM resources WHERE compartment=? AND kind='observation'",(updater.compartment,))}
    boot=updater._source_bootstrap(sources)
    subjects={s for s,caps in boot.authorities.items() if actor in caps.get('restrict',[])}
    state=updater._state()
    # A worker can end their own accepted task, which can invalidate task grants.
    for a in state['activity'].values():
        if a.get('participant')==actor or a.get('requester')==actor:
            if a.get('subject'):subjects.add(a['subject'])
            subjects.update(g['subject'] for g in state['assignment'].values() if g.get('activity_id')==a['id'])
    # Trusted workflow identities may carry controls outside participant ownership.
    if source['source']['source_identity'] in boot.workflow_identities:subjects={None}
    holds=[]
    for subject in sorted(subjects,key=lambda x:x or ''):
        hid='control-hold:'+digest([job['id'],subject])
        body={'source_id':job['source_id'],'actor':actor,'subject':subject,
            'reason':validation.get('reason','Unresolved authenticated control event')}
        c.db.execute('INSERT OR IGNORE INTO permission_control_holds VALUES (?,?,?,?,?,?)',
            (hid,updater.compartment,job['id'],subject,encoded(body),'active'))
        holds.append(hid)
    return holds


def blocks(core, compartment, subject):
    return any(h['subject'] is None or subject is None or h['subject']==subject
        for h in rows(core,compartment))


def context(updater, sources, current_source_id=None):
    result=[]
    actor=sources[current_source_id]['source']['participant'] if current_source_id else None
    boot=updater._source_bootstrap(sources)
    for h in rows(updater.core,updater.compartment):
        if actor and actor not in boot.authorities.get(h['subject'],{}).get('restrict',[]):continue
        body=json.loads(h['body']);sid=body['source_id']
        # The failed source remains protected evidence, not fresh grant authority.
        result.append({'id':h['id'],'subject':h['subject'],'failed_source_id':sid,
            'failed_source':sources.get(sid),'reason':body['reason']})
    return result


def validate_resolution(updater, op, payload):
    from .permissions import PermissionProposalRejected
    def fail(reason):raise PermissionProposalRejected(reason)
    hold=next((h for h in rows(updater.core,updater.compartment) if h['id']==op.get('hold_id')),None)
    if not hold or hold['subject'] is None:fail('Unknown or unbounded control hold; explicit host reassessment required')
    aliases={f's{i}':s for i,s in enumerate(payload['sources'],1)}
    receipt=aliases.get(op.get('authority_source'))
    if not receipt or receipt['id']!=payload['new_source_id']:fail('Hold resolution requires the current authenticated authority statement')
    actor=receipt['source']['participant']
    if actor not in payload['bootstrap']['authorities'].get(hold['subject'],{}).get('restrict',[]):
        fail('Only this subject control authority may resolve the hold')
    from .lifecycle import assertive_spans
    if not assertive_spans(receipt.get('text','')):fail('A question cannot resolve a control hold')
    if not isinstance(op.get('reason'),str) or not op['reason'].strip():fail('Hold resolution needs an explicit source-grounded assessment')
    # Receipt is necessarily later: ingestion forbids historical replay.
    return hold


def publish_resolutions(updater, wire, payload, job_id):
    changes=[]
    for op in wire['permission_changes']:
        if op.get('op')!='resolve_control_hold':continue
        hold=validate_resolution(updater,op,payload)
        body=json.loads(hold['body'])
        body['resolution']={'job_id':job_id,'source_id':payload['new_source_id'],'reason':op['reason']}
        updater.core.db.execute("UPDATE permission_control_holds SET state='resolved',body=? WHERE id=?",
            (encoded(body),hold['id']))
        changes.append({'kind':'control_hold','id':hold['id'],'state':'resolved'})
    return changes
