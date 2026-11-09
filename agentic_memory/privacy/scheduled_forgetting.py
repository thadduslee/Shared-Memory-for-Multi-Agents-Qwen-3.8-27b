"""Host-owned deferred erasure. A scheduled request never creates read access."""
import json
from dataclasses import asdict

from .system import digest, encoded, instant


def schedule(updater, erasure, job_id):
    erasure={**erasure,'policy_hash':digest(asdict(updater.bootstrap))}
    identifier='forget:'+digest([updater.compartment,job_id,erasure])
    updater.core.db.execute('INSERT OR IGNORE INTO scheduled_erasures VALUES (?,?,?,?,?,?)',
        (identifier,updater.compartment,erasure['subject'],'pending',erasure.get('effective_at'),encoded(erasure)))
    return identifier


def pending(updater):
    result=[]
    for row in updater.core.db.execute("SELECT * FROM scheduled_erasures WHERE compartment=? AND state='pending'",(updater.compartment,)):
        body=json.loads(row['body'])
        if live(updater.core,body):result.append({'id':row['id'],**body})
    return result


def live(core, body):
    sources={sid for refs in body['evidence'].values() for sid in refs}
    if body.get('policy_hash'):
        namespace=next((row[0] for sid in sources for row in core.db.execute('SELECT compartment FROM resources WHERE id=?',(sid,))),None)
        policy=core.db.execute('SELECT body FROM permission_bootstrap WHERE compartment=?',(namespace,)).fetchone()
        if not policy or digest(json.loads(policy[0]))!=body['policy_hash']:return False
    return bool(sources) and all(core._source_live(sid) and core._current(sid) for sid in sources)


def process_due(updater):
    """Call from trusted source ingestion or a host timer, never requester text."""
    core=updater.core;core._session(updater.ctx,'manage_policy')
    now=instant(core.clock())
    if now is None:return 0
    from .lifecycle import apply_erasure
    count=0
    with core.transaction():
        for row in list(core.db.execute("SELECT * FROM scheduled_erasures WHERE compartment=? AND state='pending'",(updater.compartment,))):
            body=json.loads(row['body'])
            if not live(core,body):
                core.db.execute("UPDATE scheduled_erasures SET state='cancelled',body='{}' WHERE id=?",(row['id'],))
            elif row['effective_at'] and instant(row['effective_at'])<=now:
                count+=apply_erasure(core,updater.ctx,updater.compartment,body,row['id'])
                core.db.execute("UPDATE scheduled_erasures SET state='complete',body='{}' WHERE id=?",(row['id'],))
    return count


def due_rules(core, compartment, subject):
    """Due values fail closed even before the host's physical deletion sweep."""
    now=instant(core.clock())
    if now is None:return []
    from .lifecycle import parts, fingerprint
    result=[]
    for row in core.db.execute("SELECT effective_at,body FROM scheduled_erasures WHERE compartment=? AND subject=? AND state='pending' AND effective_at IS NOT NULL",(compartment,subject)):
        body=json.loads(row['body'])
        if instant(row['effective_at'])<=now and live(core,body):
            result.extend((fingerprint(words),len(words)) for words in map(parts,body['values']))
    return result
