"""Retry known model failures without advancing a causal evaluation boundary."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time

from agentic_memory.privacy.permissions import PermissionProposalRejected, PermissionRevisionConflict
from agentic_memory.privacy.processors import ProcessorFailure, ModelContractError


class RecoveryController:
    """Wait on a failed stage; never skip a source, relax checks, or retry bugs.

    Transport outages can wait; exhausted permission repairs stop visibly.
    Contract errors outside the permission updater have a durable retry budget.
    """
    errors=(PermissionProposalRejected,PermissionRevisionConflict,ProcessorFailure,ModelContractError)

    def __init__(self, run_dir, *, sleep=time.sleep, progress=print, max_contract_failures=3):
        if type(max_contract_failures) is not int or not 1<=max_contract_failures<=5:
            raise ValueError('Use one to five contract failures')
        self.run_dir=Path(run_dir)
        self.sleep=sleep
        self.progress=progress
        self.max_contract_failures=max_contract_failures

    def _contract_failures(self, stage):
        path=self.run_dir/'recovery-events.jsonl'
        count=0
        if path.exists():
            for line in path.read_text().splitlines():
                event=json.loads(line)
                if event.get('stage')!=stage:continue
                if event['state']=='running':count=0
                elif event.get('contract_failure') or event.get('error_type')=='ModelContractError':count+=1
        return count

    def _record(self, event):
        event={'at':datetime.now(timezone.utc).isoformat(),**event}
        temporary=self.run_dir/'recovery.json.tmp'
        temporary.write_text(json.dumps(event,indent=2)+'\n')
        temporary.replace(self.run_dir/'recovery.json')
        with (self.run_dir/'recovery-events.jsonl').open('a') as handle:
            handle.write(json.dumps(event)+'\n')

    def __call__(self, operation, stage):
        failures=0
        contract_failures=self._contract_failures(stage)
        if contract_failures>=self.max_contract_failures:
            raise ModelContractError(f'{stage} remains paused after {contract_failures} invalid responses; inspect recovery.json')
        while True:
            try:
                result=operation()
            except self.errors as exc:
                failures+=1
                if isinstance(exc,ModelContractError):contract_failures+=1
                if (isinstance(exc,PermissionProposalRejected) or not getattr(exc,'retryable',True) or
                        contract_failures>=self.max_contract_failures):
                    self._record({'state':'blocked','stage':stage,'failures':failures,
                        'error_type':type(exc).__name__,'error':str(exc)[:1500],
                        'contract_failure':isinstance(exc,ModelContractError),
                        'details':getattr(exc,'details',{}),
                        'reason':'Non-retryable failure or exhausted semantic repairs; source/query remains blocked for review.'})
                    self.progress(f'  paused: {stage}: {exc}; inspect recovery.json',flush=True)
                    raise
                delay=min(5*2**min(failures-1,3),30)
                self._record({'state':'retrying','stage':stage,'failures':failures,
                    'error_type':type(exc).__name__,'error':str(exc)[:1500],
                    'contract_failure':isinstance(exc,ModelContractError),
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
