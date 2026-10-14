"""Adapters to the existing evaluator, Judge and Developer tool loop."""
import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from gatemem_adapter import assert_no_hidden_fields, make_prediction_row
from online.schedule import visible_checkpoint
from online.validation import ValidationFailure, sandbox_command


class InfrastructureFailure(RuntimeError):
    pass


class DeveloperApproachUnworkable(RuntimeError):
    """The Developer explicitly rejected the focused design, not its tooling."""


class Backend:
    def __init__(self, mock=True):
        self.mock = mock
        self.charge = lambda usage: None
        self._developer_sessions = {}
        self._developer_charged_tokens = {}

    async def memory(self, workspace, blob, header, *, delta=None, checkpoint=None):
        payload = {'workspace': str(Path(workspace).resolve()), 'episode': header}
        if delta is not None:
            payload['delta'] = delta
        if checkpoint is not None:
            payload['checkpoint'] = visible_checkpoint(checkpoint)
        assert_no_hidden_fields(payload, where='online worker')
        with tempfile.TemporaryDirectory(prefix='online-agent-') as tmp:
            path = Path(tmp) / 'memory.sqlite'
            if blob:
                path.write_bytes(blob)
            payload['workspace'] = '/workspace'
            payload['database'] = '/scratch/memory.sqlite'
            try:
                command = sandbox_command(workspace, tmp, [sys.executable, '/worker.py'],
                                          worker=Path(__file__).with_name('worker.py'),
                                          runtime_only=True)
            except ValidationFailure as exc:
                # A missing sandbox is an operator problem, not something a
                # memory design can repair: block instead of opening an incident.
                raise InfrastructureFailure(str(exc)) from exc
            try:
                # The durable runner is single-writer and sequential. Use a
                # bounded child here without requiring an asyncio child watcher.
                proc = subprocess.run(  # noqa: ASYNC221
                    command, cwd=tmp, input=json.dumps(payload), capture_output=True, text=True, timeout=90, check=False,
                    env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': '/workspace'})
            except subprocess.TimeoutExpired as exc:
                raise ValidationFailure('memory implementation timed out') from exc
            if proc.returncode:
                raise ValidationFailure(proc.stderr[-4000:])
            lines = [line for line in proc.stdout.splitlines()
                     if line.startswith('ONLINE_RESULT=')]
            if not lines:
                raise ValidationFailure('memory implementation produced no result')
            # Consolidate WAL before treating a SQLite file as an immutable image.
            conn = sqlite3.connect(path)
            try:
                conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                conn.execute('PRAGMA journal_mode=DELETE')
            finally:
                conn.close()
            return path.read_bytes(), json.loads(lines[-1].split('=', 1)[1])

    async def predict(self, workspace, blob, header, checkpoint):
        _, output = await self.memory(workspace, blob, header, checkpoint=checkpoint)
        if not self.mock and output.get('action') in {'answer', 'answer_redacted'}:
            from nodes.medical_evaluator import _render_answer
            asker = checkpoint.get('asker') or {}
            principal = asker.get('principal_id')
            relationships = [r for r in header.get('entities', {}).get('relationships', [])
                             if any(k.endswith('_id') and v == principal for k, v in r.items())]
            record = {**output, **visible_checkpoint(checkpoint), 'relationships': relationships}
            rendered, usage = await _render_answer(record, 'online')
            self.charge(usage)
            if rendered.get('degraded'):
                raise InfrastructureFailure('evaluator transport degraded')
            output.update(rendered)
        row = make_prediction_row(checkpoint['checkpoint_id'], output.get('action', 'refuse'),
                                  output.get('answer', ''), output.get('answer_structured'),
                                  output.get('used_record_ids'), output.get('debug'))
        row['output']['retrieved_memory'] = output.get('evidence', [])
        row['render_degraded'] = False
        row['infrastructure_degraded'] = False
        return row

    async def judge(self, prediction, checkpoint):
        from nodes.judge_gatemem import merge_verdicts, rule_score
        rule = rule_score(prediction, checkpoint, score_prompt_context=True)
        if not self.mock:
            from nodes.judge import _llm_verdict
            llm, usage = await _llm_verdict(prediction, checkpoint, rule, visible_checkpoint(checkpoint))
            self.charge(usage)
            # GateMem's normalizer adds over_refusal even to an empty raw
            # response. That derived field is not evidence that Judge answered.
            if not set(llm) & {'action_ok', 'action_correct', 'utility_ok', 'utility_correct',
                               'privacy_leak', 'deletion_leak'}:
                raise InfrastructureFailure('Judge transport returned no verdict')
            rule = merge_verdicts(rule, llm)
        cat = checkpoint['query_type']
        passed = (rule['utility_correct'] if cat == 'utility' else
                  not rule['privacy_leak'] if cat == 'privacy' else not rule['deletion_leak'])
        return {'category': cat, 'passed': bool(passed),
                'utility_correct': bool(rule['utility_correct']) if cat == 'utility' else None,
                'privacy_leak': bool(rule['privacy_leak']) if cat == 'privacy' else None,
                'deletion_leak': bool(rule['deletion_leak']) if cat == 'safety' else None,
                'reason': rule.get('reason', '')}

    async def critique(self, rows):
        # Deliberately small and deterministic; no one-checkpoint MGS diagnosis.
        failed = [r['checkpoint_id'] for r in rows if not r['score']['passed']]
        return {'needs_repair': bool(failed), 'failed_checkpoints': failed,
                'text': ('Failed official checkpoints: ' + ', '.join(failed)) if failed else 'All first attempts passed.'}

    async def architect(self, context, workspace):
        assert_no_hidden_fields(context, where='online Architect')
        dossier = context.get('architect_incident_dossier') or {}
        trace_ids = dossier.get('trace_entry_ids') or []
        trace_id = trace_ids[0] if trace_ids else ''
        trace_rule = ''
        pending = list(dossier.get('targets') or [])
        while pending and not trace_rule:
            item = pending.pop(0)
            if isinstance(item, dict):
                if item.get('trace_id') == trace_id:
                    trace_rule = str(item.get('decision_rule') or '')
                pending.extend(value for value in item.values()
                               if isinstance(value, (dict, list)))
            elif isinstance(item, list):
                pending.extend(item)
        if self.mock:
            # Offline path exercises a real additive schema migration, not a
            # scripted benchmark answer or a fabricated score. It has one idea;
            # once that idea was rejected it says so instead of repeating it.
            if context.get('previous_rejected_candidate'):
                return {'decision': 'NO_VIABLE_REPAIR',
                        'diagnosis': 'The only mock repair (a lookup index) was already rejected.'}
            if 'online_lookup' in (Path(workspace) / 'memory_system/schema.sql').read_text():
                return {'decision': 'NO_CHANGE', 'diagnosis': 'The focused repair is already present.',
                        'focused_goal': 'avoid a duplicate change', 'work_order': []}
            return {'decision': 'CODE_OR_SCHEMA_CHANGE',
                    'diagnosis': 'Observed retrieval lacks the general episode/turn lookup index.',
                    'focused_goal': 'add one general lookup index',
                    'work_order': ['Add idempotent index online_lookup on observations(episode_id, turn_id).'],
                    'migration_sql': 'CREATE INDEX IF NOT EXISTS online_lookup ON observations(episode_id, turn_id);',
                    'trace_citations': [trace_id],
                    'addressed_decision_rule': trace_rule,
                    'predicted_replay_change': {
                        'observable': 'decision_trace',
                        'expected_change': 'the cited target trace changes at targeted replay',
                    }}
        from harness.profiles import ARCHITECT_PROFILE
        from nodes._transport import agent_call_json
        source = {str(p.relative_to(workspace)): p.read_text() for p in
                  sorted((Path(workspace) / 'memory_system').glob('*')) if p.suffix in {'.py', '.sql'}}
        task = ('Analyze only this visible causal incident and implementation. Return JSON with exactly one '
                'focused diagnosis and exactly one focused repair: decision '
                '(DATA_MIGRATION, CODE_OR_SCHEMA_CHANGE, or NO_VIABLE_REPAIR), diagnosis (one causal statement), '
                'focused_goal (one outcome), work_order (a one-item actionable list), and migration_sql '
                '(idempotent SQLite SQL or an empty string), trace_citations (one or more exact trace_id '
                'values from the dossier), addressed_decision_rule (the exact decision_rule on a cited '
                'trace), and predicted_replay_change with observable (action, rendered_answer, '
                'authorized_evidence, decision_trace, or pass_fail) and expected_change. '
                'Do not bundle unrelated improvements. '
                'Preserve open_episode, ingest and query interfaces. Never specialize for checkpoint IDs, '
                'queries, expected answers, medical terms, or benchmark phrases. Existing data and '
                'authorization/tombstone state must be preserved. HOW YOUR REPAIR IS MEASURED: targeted '
                'replay re-queries the memory database stored at the failing boundary, after your '
                'migration_sql, using your candidate code. It does NOT re-run ingestion, and no existing '
                'row may be updated or deleted. So a change to how turns are parsed, split, linked or '
                'superseded at ingest cannot change the replay and will be rejected as '
                'no_measurable_improvement. Only query-time logic (retrieval, ranking, authorization, '
                'selection, action and projection) or additive derived rows that cite an existing '
                'observation can turn a failing target into a passing one. If no focused repair within '
                'these limits can do that, return decision NO_VIABLE_REPAIR with a diagnosis saying why; '
                'the run then stops for an operator instead of retrying. If previous_rejected_candidate is present, '
                'the next diagnosis must be materially different from its diagnosis, work order, migration, '
                'source diff, and behavior and must directly address the exact rejection reason. Relationship '
                'or semantic similarity alone must never grant permission.\n' +
                json.dumps({'context': context, 'implementation': source}))
        # No filesystem tools, external survey, or batch artifacts in this call.
        profile = dataclasses.replace(ARCHITECT_PROFILE, system_prompt='You design causal online SQL memory systems. Return a JSON design.')
        result, design = await agent_call_json(profile, task, Path(workspace))
        self.charge(result.usage)
        if not result.ok:
            raise InfrastructureFailure(result.error or 'Architect transport failure')
        if not design:
            raise ValidationFailure('Architect returned no design')
        return design

    async def develop(self, workspace, design):
        if self.mock:
            path = Path(workspace) / 'memory_system/schema.sql'
            path.write_text(path.read_text() + '\n' + design.get('migration_sql', '') + '\n')
            return {'status': 'completed', 'turns': 0, 'tokens_used': {},
                    'gate_status': {'compile_ok': 'passed', 'tests_ok': 'passed',
                                    'migration_ok': 'passed'}}
        return await self._run_developer(workspace, design)

    async def correct(self, workspace, design, feedback):
        """Return private gate feedback to the same online Developer session."""
        if self.mock:
            # Test backends can override this to model a correction. The base
            # mock change is already deterministic and needs no second edit.
            return {'status': 'completed', 'turns': 0, 'tokens_used': {},
                    'feedback_received': feedback,
                    'gate_status': {'compile_ok': 'passed', 'tests_ok': 'passed',
                                    'migration_ok': 'passed'}}
        key = str(Path(workspace).resolve())
        session = self._developer_sessions.get(key)
        if session is None:
            raise InfrastructureFailure('online Developer session was lost before correction')
        session.provide_external_feedback(feedback)
        return await self._run_developer(workspace, design, session=session)

    async def _run_developer(self, workspace, design, *, session=None):
        from nodes.developer import _TOOLBOXES, DeveloperSession, gate_status
        key = str(Path(workspace).resolve())
        if session is None:
            _TOOLBOXES.pop(key, None)
            session = DeveloperSession({
                'workspace': key, 'workspace_from': 'committed_online_version', 'iteration': 1,
                'instructions': '\n'.join(design['work_order']) +
                    '\nOnline causal mode: inspect only files needed for this focused work order; '
                    'make the focused change; run compilation, tests, and migration checks early; '
                    'correct failures immediately in this same workspace; finish only when mandatory '
                    'build gates pass. Do not invoke run_sandbox_smoke_test; the online runner performs '
                    'private exact-delta and causal replay validation.',
                'sql_schema': (Path(workspace) / 'memory_system/schema.sql').read_text(),
                'migration_sql': design.get('migration_sql', ''), 'scratchpad': [], 'failures': [],
                'retry_count': 0, 'compile_ok': False, 'tests_ok': False, 'migration_ok': False,
                'lint_ok': False, 'smoke_ok': False, 'done': False, 'pass_rate': 0.,
                'gate_attempts': {}, 'transport_failures': 0, 'transport_errors': [],
                'online_unbounded': True, 'unworkable': False, 'unworkable_reason': ''})
            self._developer_sessions[key] = session
        previous = os.environ.get('ONLINE_CANDIDATE_SANDBOX')
        os.environ['ONLINE_CANDIDATE_SANDBOX'] = '1'
        try:
            final = await session.run()
        finally:
            if previous is None:
                os.environ.pop('ONLINE_CANDIDATE_SANDBOX', None)
            else:
                os.environ['ONLINE_CANDIDATE_SANDBOX'] = previous
        total = int((final.get('tokens_used') or {}).get('total_tokens') or 0)
        prior = int(self._developer_charged_tokens.get(key, 0))
        self.charge({'total_tokens': max(0, total - prior)})
        self._developer_charged_tokens[key] = total
        if final.get('unworkable'):
            raise DeveloperApproachUnworkable(
                str(final.get('unworkable_reason') or 'focused approach is unworkable'))
        if final.get('halt_reason') == 'transport':
            raise InfrastructureFailure(
                str((final.get('transport_errors') or ['Developer transport failure'])[-1]))
        if not all(final.get(k) for k in ('compile_ok', 'tests_ok', 'migration_ok')):
            raise ValidationFailure('Developer returned before mandatory build gates passed',
                                    stage='developer', origin='implementation')
        return {'status': 'completed', 'turns': session.turns,
                'tokens_used': final.get('tokens_used') or {},
                'gate_status': gate_status(final),
                'failures': final.get('failures') or [],
                'files_written': sorted(session.box.files_written),
                'summary': str(final.get('thought') or '')[-2000:]}
