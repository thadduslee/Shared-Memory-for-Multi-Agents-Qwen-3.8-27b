"""Durable optional enrichment, drained after serving the committed source prefix."""
from agentic_memory.privacy.processors import ProcessorFailure, ModelContractError
from agentic_memory.privacy.system import AccessDenied, digest

BATCH_SIZE=4


class EvolutionQueue:
    def __init__(self, core, context, compartment):
        self.core,self.context,self.compartment=core,context,compartment

    def enqueue(self, source_job, resource_ids, processor):
        with self.core.transaction():
            for rid in sorted(set(resource_ids)):
                qid='queued-evolution:'+digest([source_job,rid,processor.processor_id])
                self.core.db.execute('INSERT OR IGNORE INTO evolution_queue '
                    '(id,compartment,source_job,resource_id,processor) VALUES (?,?,?,?,?)',
                    (qid,self.compartment,source_job,rid,processor.processor_id))

    def _live(self, rid, processor):
        c=self.core
        if not c.allowed(self.context,rid,'derive',processor=processor.processor_id):return False
        # Discard fields whose inferred grant was revoked, expired or suspended.
        # The ingestion service itself can still audit those fields.
        session=c._session(self.context,'derive',self.compartment)
        return any(c._assignment_live(row[0],session,'derive') for row in c.db.execute(
            "SELECT e.assignment_id FROM access_edges e JOIN assignments a ON a.id=e.assignment_id "
            "WHERE e.memory_id=? AND a.state='active'",(rid,)))

    def drain(self, processor, *, max_batches=1, flush=False):
        """Run bounded batches; failures stay queued without retrying ingestion.

        A batch is durably claimed before its external call. Resume reuses the
        same evolution job and exact inputs. New batches filter stale fields;
        the core checks source liveness and authorization again at publication.
        """
        c=self.core;completed=0
        while max_batches is None or completed<max_batches:
            if c.db.execute("SELECT 1 FROM permission_jobs WHERE compartment=? AND status NOT IN "
                            "('complete','superseded')",(self.compartment,)).fetchone():break
            with c.transaction():
                rows=list(c.db.execute("SELECT * FROM evolution_queue WHERE compartment=? AND processor=? "
                    "AND status='pending' ORDER BY rowid",(self.compartment,processor.processor_id)))
                if not rows:break
                claimed=next((r['batch_id'] for r in rows if r['batch_id']),None)
                if claimed:
                    batch=[r for r in rows if r['batch_id']==claimed]
                    if any(not self._live(r['resource_id'],processor) for r in batch):
                        c.db.execute("UPDATE evolution_queue SET status='stale',error='Source or grant changed' "
                                     'WHERE batch_id=?',(claimed,))
                        continue
                else:
                    live=[]
                    for row in rows:
                        if not self._live(row['resource_id'],processor):
                            c.db.execute("UPDATE evolution_queue SET status='stale' WHERE id=?",(row['id'],))
                        else:live.append(row)
                    if not live or (len(live)<BATCH_SIZE and not flush):break
                    batch=live[:BATCH_SIZE]
                    claimed='evolution-batch:'+digest([self.compartment,processor.processor_id,[r['id'] for r in batch]])
                    for row in batch:c.db.execute('UPDATE evolution_queue SET batch_id=? WHERE id=?',(claimed,row['id']))
            try:
                outcome=c.evolve(self.context,processor,[r['resource_id'] for r in batch],job_id=claimed)
            except (ProcessorFailure,ModelContractError) as exc:
                with c.transaction():
                    terminal=isinstance(exc,ModelContractError) or not getattr(exc,'retryable',True)
                    c.db.execute('UPDATE evolution_queue SET status=?,error=? WHERE batch_id=?',
                                 ('rejected' if terminal else 'pending',type(exc).__name__,claimed))
                break
            except AccessDenied:
                with c.transaction():
                    c.db.execute("UPDATE evolution_queue SET status='stale',error='Authorization changed' "
                                 'WHERE batch_id=?',(claimed,))
                continue
            with c.transaction():
                status='rejected' if outcome['status']=='rejected' else 'complete'
                c.db.execute('UPDATE evolution_queue SET status=?,error=NULL WHERE batch_id=?',(status,claimed))
            completed+=1
        return completed
