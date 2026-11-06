"""Retry known model failures without advancing a causal evaluation boundary."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time

from agentic_memory.privacy.permissions import PermissionProposalRejected, PermissionRevisionConflict
from agentic_memory.privacy.processors import ProcessorFailure, ModelContractError


class RecoveryController:
    """Wait on a failed stage; never skip a source, relax checks, or retry bugs.

    Ctrl+C still exits immediately. Repeated model failures remain visibly
    blocked in recovery.json and the console until a valid response is obtained.
    """
    errors=(PermissionProposalRejected,PermissionRevisionConflict,ProcessorFailure,ModelContractError)

    def __init__(self, run_dir, *, sleep=time.sleep, progress=print):
        self.run_dir=Path(run_dir)
        self.sleep=sleep
        self.progress=progress

    def _record(self, event):
        event={'at':datetime.now(timezone.utc).isoformat(),**event}
        temporary=self.run_dir/'recovery.json.tmp'
        temporary.write_text(json.dumps(event,indent=2)+'\n')
        temporary.replace(self.run_dir/'recovery.json')
        with (self.run_dir/'recovery-events.jsonl').open('a') as handle:
            handle.write(json.dumps(event)+'\n')

    def __call__(self, operation, stage):
        failures=0
        while True:
            try:
                result=operation()
            except self.errors as exc:
                failures+=1
                delay=min(5*2**min(failures-1,3),30)
                self._record({'state':'retrying','stage':stage,'failures':failures,
                    'error_type':type(exc).__name__,'error':str(exc)[:1500],
                    'retry_at':(datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat()})
                self.progress(f'  recovery: {stage}: {type(exc).__name__}: {str(exc)[:1000]}; '
                              f'waiting {delay}s before retry {failures}; source/query remains blocked',flush=True)
                self.sleep(delay)
            else:
                if failures:
                    self._record({'state':'running','stage':stage,'recovered_after_failures':failures})
                    self.progress(f'  recovery: {stage}: recovered after {failures} failures',flush=True)
                return result


def reconcile_manifest(journal, current, *, resume, previous_code_digest=None):
    """Keep the original manifest immutable; explicitly audit a code revision.

    The caller must supply the exact previously active digest. Dataset, model,
    permission version and every other manifest field must still match.
    """
    row=journal.execute("SELECT value FROM state WHERE key='manifest'").fetchone()
    if not row:
        if resume or previous_code_digest:
            raise ValueError('No existing run to resume')
        with journal:
            journal.execute('INSERT INTO state VALUES (?,?)',('manifest',json.dumps(current)))
        return current,[]
    if not resume:raise ValueError('Run exists; use --resume')
    original=json.loads(row[0])
    if {k:v for k,v in original.items() if k!='code_digest'}!={k:v for k,v in current.items() if k!='code_digest'}:
        raise ValueError('Dataset, models, or causal configuration changed; use a new run')
    row=journal.execute("SELECT value FROM state WHERE key='implementation_revisions'").fetchone()
    revisions=json.loads(row[0]) if row else []
    active=revisions[-1]['code_digest'] if revisions else original['code_digest']
    if previous_code_digest is not None and previous_code_digest!=active:
        raise ValueError('Code revision acknowledgement does not match the previously active implementation')
    if active!=current['code_digest']:
        if previous_code_digest!=active:
            raise ValueError('Implementation changed; use a new run or explicitly acknowledge the previous digest with --resume-code-revision')
        revisions.append({'previous_code_digest':active,'code_digest':current['code_digest'],
            'at':datetime.now(timezone.utc).isoformat(),
            'preserved_predictions':journal.execute('SELECT COUNT(*) FROM official').fetchone()[0],
            'completed_boundaries':journal.execute("SELECT COUNT(*) FROM boundaries WHERE state='complete'").fetchone()[0]})
        with journal:
            journal.execute('INSERT OR REPLACE INTO state VALUES (?,?)',
                            ('implementation_revisions',json.dumps(revisions)))
    return original,revisions
