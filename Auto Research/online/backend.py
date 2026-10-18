"""Adapters to the existing evaluator, Judge and Developer tool loop."""
import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import config
from gatemem_adapter import assert_no_hidden_fields, make_prediction_row
from online.schedule import visible_checkpoint
from online.validation import ValidationFailure, sandbox_command


class InfrastructureFailure(RuntimeError):
    pass


class PromptContextFailure(InfrastructureFailure):
    """A deterministic oversized-prompt failure; retrying cannot repair it."""


class DeveloperApproachUnworkable(RuntimeError):
    """The Developer explicitly rejected the focused design, not its tooling."""


class Backend:
    def __init__(self, mock=True, *, renderer_mode=None):
        self.mock = mock
        self.renderer_mode = renderer_mode or config.ONLINE_RENDERER_MODE
        self.online_developer_timeout_s = config.ONLINE_DEVELOPER_EPISODE_TIMEOUT_S
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

    async def predict(self, workspace, blob, header, checkpoint, *, memory_decision=None):
        if memory_decision is None:
            _, memory_decision = await self.memory(
                workspace, blob, header, checkpoint=checkpoint)
        else:
            memory_decision = json.loads(json.dumps(memory_decision))
        trusted_action = str(memory_decision.get('action') or 'refuse')
        answer_plan = memory_decision.get('answer_plan') or {}
        render_attempt = None
        contract_result = None
        if not self.mock and trusted_action in {'answer', 'answer_redacted'}:
            from nodes.medical_evaluator import _render_answer
            asker = checkpoint.get('asker') or {}
            principal = asker.get('principal_id')
            relationships = [r for r in header.get('entities', {}).get('relationships', [])
                             if any(k.endswith('_id') and v == principal for k, v in r.items())]
            record = {**memory_decision, **visible_checkpoint(checkpoint),
                      'relationships': relationships}
            artifact = self._renderer_artifact(workspace)
            released, usage = await _render_answer(
                record, 'online', renderer_mode=self.renderer_mode,
                renderer_artifact=artifact)
            self.charge(usage)
            render_attempt = released.get('renderer_attempt')
            contract_result = released.get('renderer_contract')
            if released.get('degraded'):
                raise InfrastructureFailure('evaluator transport degraded')
            final_output = released
        else:
            from renderer_contract import TrustedRenderDecision, enforce_render_contract
            decision = TrustedRenderDecision.from_record(memory_decision)
            final_output, contract_result = enforce_render_contract(decision, None)
            render_attempt = None

        # The sole online final-answer assembly point. The trusted action is
        # copied explicitly; no renderer dictionary can overwrite ownership.
        final_prediction = {
            'action': trusted_action,
            'answer': str(final_output.get('answer') or ''),
            'answer_structured': final_output.get('answer_structured') or {},
            'used_record_ids': final_output.get('used_record_ids') or [],
            'answer_source': final_output.get('answer_source'),
        }
        row = make_prediction_row(
            checkpoint['checkpoint_id'], final_prediction['action'],
            final_prediction['answer'], final_prediction['answer_structured'],
            final_prediction['used_record_ids'], memory_decision.get('debug'))
        observable = row['output']
        observable['answer_source'] = final_prediction['answer_source']
        observable['answer_plan'] = answer_plan
        observable['selected_claim_ids'] = [
            str(c.get('claim_id')) for c in answer_plan.get('claims', [])
            if isinstance(c, dict)]
        observable['required_claim_ids'] = [
            str(c.get('claim_id')) for c in answer_plan.get('claims', [])
            if isinstance(c, dict) and c.get('required')]
        observable['fallback_answer'] = memory_decision.get('fallback_answer', '')
        observable['renderer_attempt'] = render_attempt
        observable['renderer_contract'] = contract_result
        observable['renderer_mode'] = self.renderer_mode
        observable['renderer_version'] = self._component_versions(workspace).get(
            'renderer_version', config.FIXED_RENDERER_VERSION)
        observable['contract_version'] = config.FIXED_CONTRACT_VERSION
        observable['memory_version'] = self._component_versions(workspace).get('memory_version')
        row['output']['retrieved_memory'] = memory_decision.get('evidence', [])
        row['render_degraded'] = False
        row['infrastructure_degraded'] = False
        return row

    @staticmethod
    def _renderer_artifact(workspace):
        path = Path(workspace) / 'renderer' / 'renderer.json'
        if not path.is_file():
            return {'schema_version': 1,
                    'renderer_version': config.FIXED_RENDERER_VERSION,
                    'instruction_suffix': ''}
        try:
            artifact = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ValidationFailure('malformed declarative renderer artifact') from exc
        if (not isinstance(artifact, dict) or artifact.get('schema_version') != 1 or
                not isinstance(artifact.get('instruction_suffix'), str)):
            raise ValidationFailure('invalid declarative renderer schema')
        return artifact

    @staticmethod
    def _component_versions(workspace):
        path = Path(workspace).parent / 'manifest.json'
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

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
            if context.get('latest_complete_structured_feedback'):
                return {'decision': 'ESCALATE', 'failure_classification': 'ESCALATE',
                        'diagnosis': 'The only mock repair (a lookup index) was already rejected.',
                        'escalation_reason': 'No second safe falsifiable hypothesis is available.'}
            if 'online_lookup' in (Path(workspace) / 'memory_system/schema.sql').read_text():
                return {'decision': 'NO_CHANGE', 'diagnosis': 'The focused repair is already present.',
                        'focused_goal': 'avoid a duplicate change', 'work_order': []}
            return {'decision': 'CODE_OR_SCHEMA_CHANGE',
                    'failure_classification': 'RETRIEVAL',
                    'repair_component': 'memory',
                    'diagnosis': 'Observed retrieval lacks the general episode/turn lookup index.',
                    'specific_hypothesis': (
                        'Adding the episode/turn lookup index changes the cited target trace.'),
                    'repair_scope': {'subsystem': 'storage',
                                     'file': 'memory_system/schema.sql',
                                     'function': 'schema initialization'},
                    'focused_goal': 'add one general lookup index',
                    'work_order': ['Add idempotent index online_lookup on observations(episode_id, turn_id).'],
                    'migration_sql': 'CREATE INDEX IF NOT EXISTS online_lookup ON observations(episode_id, turn_id);',
                    'trace_citations': [trace_id],
                    'addressed_decision_rule': trace_rule,
                    'predicted_replay_change': {
                        'observable': 'decision_trace',
                        'expected_change': 'the cited target trace changes at targeted replay',
                    },
                    'intended_target_ids': [
                        str(item['checkpoint_id']) for item in dossier.get('targets') or []
                        if isinstance(item, dict) and item.get('checkpoint_id')
                    ],
                    'safety_preservation_checks': [
                        'No privacy, deletion, or previously-passing checkpoint regresses.'
                    ]}
        from harness.profiles import ARCHITECT_PROFILE
        from nodes._transport import agent_call_json
        allowed_component = str(context.get('allowed_repair_component') or 'memory')
        source_root = Path(workspace) / (
            'renderer' if allowed_component == 'renderer' else 'memory_system')
        source = {str(p.relative_to(workspace)): p.read_text() for p in
                  sorted(source_root.rglob('*'))
                  if p.suffix in {'.py', '.sql', '.json'}}
        reingest = bool((context.get('incident_report') or {}).get('replay_reingest'))
        replay_note = (
            'Targeted replay deterministically rebuilds state from authoritative visible turns, so '
            'ingestion, parsing, authorization metadata, and state-transition repairs are measurable. '
            if reingest else
            'Targeted replay reuses the frozen database image. Limit this proposal to changes measurable '
            'without re-running ingestion. '
        )
        task = ('Analyze only this sanitized causal incident and implementation. First classify the '
                'failure as exactly one of INGESTION, AUTHORIZATION, RETRIEVAL, FACT_SELECTION, '
                'RENDERING, INFRASTRUCTURE, or ESCALATE in failure_classification. An ordinary '
                'benchmark mismatch MUST NOT be called NO_VIABLE_REPAIR. INFRASTRUCTURE and ESCALATE '
                'must not produce a product repair. For an actionable classification return '
                'repair_component exactly memory or renderer, decision DATA_MIGRATION or '
                'CODE_OR_SCHEMA_CHANGE and exactly one concrete, falsifiable repair: diagnosis and '
                'specific_hypothesis (one causal statement), repair_scope with subsystem, file, and '
                'function, focused_goal (one outcome), work_order (a JSON array containing exactly '
                'one actionable string, for example ["Change memory_system/store.py retrieve"]), '
                'intended_target_ids (the unresolved targets expected to improve), and migration_sql '
                '(idempotent SQLite SQL or an empty string), trace_citations (one or more exact trace_id '
                'values from the dossier), addressed_decision_rule (the exact decision_rule on a cited '
                'trace), failure_mechanism (a short stable causal mechanism enum/phrase), change_type '
                '(for example query_filter, ranking, parser, schema_change, or prompt_constraint), '
                'and predicted_replay_change with observable (action, rendered_answer, '
                'authorized_evidence, decision_trace, selected_claims, required_claims, '
                'contract_result, fallback_usage, answer_source, or pass_fail) and expected_change. '
                'Also return safety_preservation_checks naming privacy, deletion, and previously-passing '
                'behavior checks. '
                'Group failures only when the sanitized evidence supports one shared cause. Prefer small, '
                'testable changes and preserve all previously passing and partially promoted behavior. '
                'Preserve open_episode, ingest and query interfaces. Never specialize for checkpoint IDs, '
                'queries, expected answers, medical terms, or benchmark phrases. Existing data and '
                'authorization/tombstone state must be preserved. The only editable component for '
                f'this incident is {allowed_component}; adjacent components are read-only interfaces. '
                'FACT_SELECTION owns memory claim selection and RENDERING owns renderer artifacts. '
                'HOW YOUR REPAIR IS MEASURED: ' +
                replay_note +
                'Use failure_classification ESCALATE and decision ESCALATE only when the visible evidence '
                'is insufficient to identify a safe hypothesis; include escalation_reason and do not '
                'invent a work order. If latest_complete_structured_feedback is present, '
                'the next diagnosis must be materially different from its diagnosis, work order, migration, '
                'source diff, and behavior and must directly address the exact rejection reason. Relationship '
                'or semantic similarity alone must never grant permission.\n' +
                json.dumps({'context': context, 'implementation': source}))
        # No filesystem tools, external survey, or batch artifacts in this call.
        profile = dataclasses.replace(
            ARCHITECT_PROFILE,
            system_prompt='You design causal, grounded online memory and answer-rendering systems. Return a JSON design.')
        estimated_input_tokens = self._estimate_prompt_tokens(
            profile.system_prompt + "\n" + task)
        reserved_output_tokens = max(
            config.ARCHITECT_CONTEXT_OUTPUT_HEADROOM_TOKENS,
            int(profile.max_tokens or 0),
        )
        input_limit = config.ARCHITECT_CONTEXT_WINDOW_TOKENS - reserved_output_tokens
        if estimated_input_tokens > input_limit:
            raise PromptContextFailure(
                "Architect prompt exceeds configured context headroom: "
                f"estimated_input_tokens={estimated_input_tokens}, "
                f"input_limit={input_limit}, "
                f"context_window={config.ARCHITECT_CONTEXT_WINDOW_TOKENS}, "
                f"reserved_output_tokens={reserved_output_tokens}; compact the prompt or "
                "configure a larger Architect context window"
            )
        result, design = await agent_call_json(profile, task, Path(workspace))
        self.charge(result.usage)
        if not result.ok:
            if self._is_context_length_error(result.error):
                raise PromptContextFailure(
                    "Architect provider rejected the prompt as too large: " +
                    str(result.error or "context length exceeded")
                )
            raise InfrastructureFailure(result.error or 'Architect transport failure')
        if not design:
            raise ValidationFailure('Architect returned no design')
        return self._normalize_architect_design(design)

    @staticmethod
    def _normalize_architect_design(design):
        """Normalize the sole safe schema ambiguity while retaining strict gates."""
        if not isinstance(design, dict):
            return design
        if not design.get('repair_component'):
            classification = str(design.get('failure_classification') or '')
            if classification in {'INGESTION', 'AUTHORIZATION', 'RETRIEVAL', 'FACT_SELECTION'}:
                design = {**design, 'repair_component': 'memory'}
            elif classification == 'RENDERING':
                design = {**design, 'repair_component': 'renderer'}
        work_order = design.get('work_order')
        if isinstance(work_order, str) and work_order.strip():
            return {**design, 'work_order': [work_order.strip()]}
        return design

    @staticmethod
    def _estimate_prompt_tokens(text):
        # Qwen tokenizes code, punctuation-heavy IDs, and JSON more densely than
        # the usual four-characters-per-token prose heuristic.
        return (len(str(text).encode('utf-8')) + 2) // 3 + 16

    @staticmethod
    def _is_context_length_error(error):
        lowered = str(error or '').lower()
        return any(marker in lowered for marker in (
            'maximum context length', 'context length exceeded',
            'prompt is too long', 'too many input tokens',
            'input_tokens', 'max_model_len',
        ))

    async def develop(self, workspace, design):
        if self.mock:
            if design.get('repair_component') == 'renderer':
                path = Path(workspace) / 'renderer/renderer.json'
                artifact = json.loads(path.read_text(encoding='utf-8'))
                artifact['instruction_suffix'] = (
                    str(artifact.get('instruction_suffix') or '').rstrip() + '\n' +
                    str(design.get('focused_goal') or 'preserve grounded required claims'))
                path.write_text(json.dumps(artifact, indent=2) + '\n', encoding='utf-8')
            else:
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
                'online_unbounded': False, 'online_mode': True,
                'online_max_turns': config.ONLINE_DEVELOPER_TURN_LIMIT,
                'online_timeout_s': self.online_developer_timeout_s,
                'unworkable': False, 'unworkable_reason': ''})
            self._developer_sessions[key] = session
        previous = os.environ.get('ONLINE_CANDIDATE_SANDBOX')
        previous_component = os.environ.get('ONLINE_REPAIR_COMPONENT')
        os.environ['ONLINE_CANDIDATE_SANDBOX'] = '1'
        os.environ['ONLINE_REPAIR_COMPONENT'] = str(
            design.get('repair_component') or 'memory')
        try:
            final = await session.run()
        finally:
            if previous is None:
                os.environ.pop('ONLINE_CANDIDATE_SANDBOX', None)
            else:
                os.environ['ONLINE_CANDIDATE_SANDBOX'] = previous
            if previous_component is None:
                os.environ.pop('ONLINE_REPAIR_COMPONENT', None)
            else:
                os.environ['ONLINE_REPAIR_COMPONENT'] = previous_component
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
        if final.get('halt_reason') in {'timeout', 'turn_cap', 'exhausted'}:
            raise InfrastructureFailure(
                'bounded Developer episode ended before mandatory gates completed: ' +
                str(final.get('halt_reason')))
        if not all(final.get(k) for k in ('compile_ok', 'tests_ok', 'migration_ok')):
            raise ValidationFailure('Developer returned before mandatory build gates passed',
                                    stage='developer', origin='implementation')
        return {'status': 'completed', 'turns': session.turns,
                'tokens_used': final.get('tokens_used') or {},
                'gate_status': gate_status(final),
                'failures': final.get('failures') or [],
                'files_written': sorted(session.box.files_written),
                'summary': str(final.get('thought') or '')[-2000:]}
