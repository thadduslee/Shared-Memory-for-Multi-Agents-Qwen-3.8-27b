"""Standalone prequential runner for the NEW policy-governed A-MEM runtime.

No Auto Research modules, templates, journals, workers or CLI are imported.
GateMem supplies source fixtures and evaluation scoring only.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime,timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import urlsplit

from agentic_memory.privacy import PrivacyMemorySystem,AccessDenied
from agentic_memory.privacy.system import digest,encoded
from agentic_memory.privacy.answering import answer_request,validate_answer_output
from agentic_memory.privacy.processors import LocalEmbedder,HashEmbedder,CompletionProcessor,ScriptedProcessor,ModelContractError,openrouter_routing_preferences,ProcessorBudgetFailure
from .adapter import MedicalFixtureAdapter,load_data,schedule,visible_query
from .inference_adapter import InferenceFixtureAdapter,EmptyPermissionProcessor
from .input_boundary import episode_header,source_turn
from .scoring import GateMemScorer
from .recovery import RecoveryController,reconcile_manifest

ROOT=Path(__file__).resolve().parents[2]


def load_environment(path):
    path=Path(path)
    if path.is_file():
        for line in path.read_text().splitlines():
            line=line.strip()
            if line and not line.startswith('#') and '=' in line:
                key,_,value=line.partition('=')
                if key.strip().startswith('AMEM_'):
                    os.environ.setdefault(key.strip(),value.strip().strip('\"\''))


def processors(offline=False):
    if offline:
        return HashEmbedder(),ScriptedProcessor(),None
    embedder=LocalEmbedder(os.getenv('AMEM_EMBEDDING_PATH') or None)
    llm=CompletionProcessor('vllm:qwen',os.environ['AMEM_LLM_BASE_URL'],
        os.getenv('AMEM_LLM_MODEL','Qwen/Qwen3.8-27B'),os.getenv('AMEM_LLM_API_KEY',''),
        timeout=float(os.getenv('AMEM_TIMEOUT','300')),max_tokens=8192)
    judge=CompletionProcessor('judge:independent',os.environ['AMEM_JUDGE_BASE_URL'],
        os.getenv('AMEM_JUDGE_MODEL','Qwen/Qwen3-14B'),os.getenv('AMEM_JUDGE_API_KEY',''),
        timeout=float(os.getenv('AMEM_TIMEOUT','300')),max_tokens=int(os.getenv('AMEM_JUDGE_MAX_TOKENS','2048')))
    return embedder,llm,judge


def answer(core,adapter,visible,processor,top_k=None):
    result=answer_request(core,adapter,visible,processor,top_k=top_k)
    return {'checkpoint_id':visible['checkpoint_id'],'episode_id':visible['episode_id'],
            'as_of_turn_id':visible['as_of_turn_id'],**result}


def write_json(path,value):
    path=Path(path)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')
    temporary.replace(path)


def export_run(run_dir,journal,events,episodes,checkpoints,scorer,core,offline):
    predictions=[json.loads(row[0]) for row in journal.execute('SELECT prediction FROM official ORDER BY boundary,id')]
    scores=[json.loads(row[0]) for row in journal.execute('SELECT score FROM official WHERE score IS NOT NULL ORDER BY boundary,id')]
    for name,rows in [('official_predictions.jsonl',predictions),('scores.jsonl',scores)]:
        tmp=run_dir/(name+'.tmp')
        tmp.write_text(''.join(encoded(r)+'\n' for r in rows));tmp.replace(run_dir/name)
    done=[dict(row) for row in journal.execute('SELECT * FROM boundaries ORDER BY id')]
    expected_ids={c['checkpoint_id'] for c in checkpoints}
    actual_ids={p['checkpoint_id'] for p in predictions}
    ingested=set()
    for row in core.db.execute('SELECT details FROM events WHERE id LIKE \'boundary:%\' AND state=\'complete\''):
        detail=json.loads(row[0])
        ingested.update(detail.get('source_keys',[]))
    # Older event details include the episode-qualified observation IDs instead.
    if not ingested:
        for row in core.db.execute('SELECT id FROM resources WHERE kind=\'observation\''):
            ingested.add(row[0].removeprefix('obs:'))
    expected_turns={f'{e["episode_id"]}:{t["turn_id"]}' for e in episodes for t in e['turns']}
    coverage={'expected_checkpoints':len(checkpoints),'official_predictions':len(predictions),
              'scored_checkpoints':len(scores),'expected_boundaries':len(events),
              'completed_boundaries':sum(b['state']=='complete' for b in done),
              'expected_source_turns':len(expected_turns),'ingested_source_turns':len(ingested),
              'exact_checkpoint_coverage':actual_ids==expected_ids and len(predictions)==len(expected_ids),
              'exact_source_coverage':ingested==expected_turns,
              'all_boundaries_complete':len(done)==len(events) and all(b['state']=='complete' for b in done)}
    metrics=scorer.aggregate(scores)
    # Native aggregate uses zero for absent categories. Mark undefined rates explicitly.
    for count,keys in [('n_utility_scored',['utility_accuracy']),
                       ('n_privacy_scored',['privacy_leakage_rate','privacy_e2e_leakage_rate']),
                       ('n_safety_scored',['deletion_leakage_rate','deletion_e2e_leakage_rate'])]:
        if not metrics.get(count):
            for key in keys:metrics[key]=None
    if not all(metrics.get(k) for k in ('n_utility_scored','n_privacy_scored','n_safety_scored')):
        metrics['compliance_utility_score']=metrics['compliance_utility_e2e_score']=None
    report={'system':'privacy-aware A-MEM two-store v2','mode':'offline-functional-test' if offline else 'real-model-evaluation',
            'coverage':coverage,'metrics':metrics,'security_claim':'Prototype guarantees are bounded by the tested core and trusted fixture adapters.',
            'complete':all(coverage[k] for k in ('exact_checkpoint_coverage','exact_source_coverage','all_boundaries_complete')) and len(scores)==len(checkpoints)}
    manifest_row=journal.execute("SELECT value FROM state WHERE key='manifest'").fetchone()
    if manifest_row:
        report['agent_input_contract']=json.loads(manifest_row[0])['agent_input_contract']
    revisions=journal.execute("SELECT value FROM state WHERE key='implementation_revisions'").fetchone()
    report['implementation_revisions']=json.loads(revisions[0]) if revisions else []
    report['mixed_implementation']=bool(report['implementation_revisions'])
    recovery_file=run_dir/'recovery.json'
    if recovery_file.is_file():report['recovery']=json.loads(recovery_file.read_text())
    report['evolution']={
        'jobs':{r[0]:r[1] for r in core.db.execute('SELECT status,COUNT(*) FROM evolution_jobs GROUP BY status')},
        'attempts':core.db.execute('SELECT COUNT(*) FROM evolution_attempts').fetchone()[0],
        'repaired_jobs':core.db.execute("SELECT COUNT(*) FROM evolution_jobs WHERE status='applied' AND attempts>1").fetchone()[0],
        'model_rejections':core.db.execute("SELECT COUNT(*) FROM evolution_attempts WHERE status='rejected'").fetchone()[0]}
    report['evolution']['queue']={r[0]:r[1] for r in core.db.execute(
        'SELECT status,COUNT(*) FROM evolution_queue GROUP BY status')}
    report['evolution']['enrichment_complete']=not report['evolution']['queue'].get('pending',0)
    report['pipeline_seconds']={r[0]:round(r[1],3) for r in core.db.execute('SELECT stage,SUM(seconds) FROM pipeline_timings GROUP BY stage')}
    report['governance']={'candidate_versions':core.db.execute('SELECT COUNT(*) FROM capability_candidates').fetchone()[0],
        'decisions':core.db.execute('SELECT COUNT(*) FROM capability_decisions').fetchone()[0],
        'recipient_decisions':core.db.execute('SELECT COUNT(*) FROM permission_coverage').fetchone()[0],
        'erasure_selectors':core.db.execute('SELECT COUNT(*) FROM source_erasures').fetchone()[0]}
    report['permission_inference']={
        'jobs':{r[0]:r[1] for r in core.db.execute('SELECT status,COUNT(*) FROM permission_jobs GROUP BY status')},
        'attempts':core.db.execute('SELECT COUNT(*) FROM permission_attempts').fetchone()[0],
        'record_versions':core.db.execute('SELECT COUNT(*) FROM permission_records').fetchone()[0]}
    permission_stats=report['permission_inference']
    permission_stats['skipped_turns']=[{**dict(row),'validation':json.loads(row['validation'] or '{}')} for row in core.db.execute(
        "SELECT id,compartment,source_id,validation FROM permission_jobs WHERE status='skipped' ORDER BY rowid")]
    permission_stats['quarantined_turns']=[{**dict(row),'validation':json.loads(row['validation'] or '{}')} for row in core.db.execute(
        "SELECT id,compartment,source_id,validation FROM permission_jobs WHERE status='quarantined' ORDER BY rowid")]
    from agentic_memory.privacy.control_holds import rows as control_holds
    permission_stats['active_scoped_control_holds']=sum(len(control_holds(core,r[0])) for r in core.db.execute(
        'SELECT DISTINCT compartment FROM permission_control_holds'))
    permission_stats['complete']=not (permission_stats['skipped_turns'] or permission_stats['quarantined_turns'])
    report['degraded']=not permission_stats['complete']
    permission_stats['runtime_assignments']=core.db.execute("SELECT COUNT(*) FROM assignments WHERE state='active'").fetchone()[0]
    permission_stats['indexed_resources']=core.db.execute('SELECT COUNT(*) FROM representations').fetchone()[0]
    timings=[{'job_id':r[0],**json.loads(r[1])} for r in core.db.execute("SELECT job_id,validation FROM permission_attempts WHERE validation IS NOT NULL")]
    measured=[r for r in timings if 'elapsed_s' in r]
    permission_stats['coverage_adjustments']=sum(len(r.get('coverage_adjustments',[])) for r in timings)
    extraction_status={r[0]:r[1] for r in core.db.execute('SELECT status,COUNT(*) FROM fact_extractions GROUP BY status')}
    stage_calls={}
    for row in core.db.execute('SELECT stage,body FROM fact_attempts'):
        body=json.loads(row['body'])
        stage=stage_calls.setdefault(row['stage'],{'calls':0,'timed_calls':0,'seconds':0.0,'prompt_tokens':0,'completion_tokens':0})
        stage['calls']+=1
        if 'elapsed_s' in body:
            stage['timed_calls']+=1;stage['seconds']+=body['elapsed_s']
        for key in ('prompt_tokens','completion_tokens'):stage[key]+=(body.get('usage') or {}).get(key,0)
    binding_decisions=[d for t in timings for d in t.get('candidate_binding_decisions',[])]
    gap_decisions=[d for t in timings for d in t.get('authorization_gap_reviews',[])]
    report['memory_processing']={'durable_extraction_enabled':bool(extraction_status),
        'extraction_jobs':extraction_status,
        'candidate_states':{r[0]:r[1] for r in core.db.execute('SELECT state,COUNT(*) FROM fact_candidates GROUP BY state')},
        'semantic_stages':stage_calls,
        'binding_review_outcomes':{name:sum(d['outcome']==name for d in binding_decisions)
            for name in sorted({d['outcome'] for d in binding_decisions})},
        'authorization_gap_diagnoses':{name:sum(d['category']==name for d in gap_decisions)
            for name in sorted({d['category'] for d in gap_decisions})},
        'failure_hold_jobs':sum(count for status,count in permission_stats['jobs'].items() if status not in {'complete','superseded'}),
        'checkpoints_with_evidence':sum(bool(p.get('input_manifest',{}).get('memory_ids')) for p in predictions),
        'checkpoints_without_evidence':sum(not p.get('input_manifest',{}).get('memory_ids') for p in predictions),
        'semantic_accuracy_measured':False}
    report['memory_processing']['historical_binding_failures']=sum(len(json.loads(r[0] or '{}').get('pending_bindings',[]))
        for r in core.db.execute('SELECT validation FROM permission_jobs'))
    # Historical lists repeat pairs across turns. Current readiness comes from
    # each compartment's latest committed publication, not that cumulative sum.
    latest=list(core.db.execute('''SELECT j.compartment,j.validation FROM permission_jobs j
        WHERE j.rowid=(SELECT MAX(k.rowid) FROM permission_jobs k WHERE k.compartment=j.compartment)'''))
    readiness={r[0].removeprefix('binding-readiness:'):json.loads(r[1])['pending'] for r in
        core.db.execute("SELECT key,value FROM privacy_metadata WHERE key LIKE 'binding-readiness:%'")}
    pending=sum(readiness.get(r['compartment'],len(json.loads(r['validation'] or '{}').get('pending_bindings',[]))) for r in latest)
    report['memory_processing'].update(pending_binding_reviews_at_latest_publication=pending,
        historical_pending_pair_observations=report['memory_processing']['historical_binding_failures'],
        metric_semantics={'historical_binding_failures':'Legacy alias: repeated pending-pair observations, not unique failures.',
            'pending_binding_reviews_at_latest_publication':'Outstanding pairs at the last source or maintenance publication in each compartment.'})
    report['memory_processing']['ready_for_answers']=not pending and not report['memory_processing']['failure_hold_jobs'] and not any(
        extraction_status.get(status,0) for status in ('pending','incomplete','running'))
    if pending:report['degraded']=True
    if manifest_row and json.loads(manifest_row[0]).get('permission_workflow')=='combined-incremental':
        import statistics
        durations=[r[0] for r in core.db.execute("SELECT seconds FROM pipeline_timings WHERE stage='source_total'")]
        jobs=core.db.execute('SELECT COUNT(*) FROM permission_jobs').fetchone()[0]
        report['permission_inference']['semantic_calls_per_source']=round(
            sum(t.get('semantic_response',True) for t in timings)/max(1,jobs),3)
        report['permission_inference']['latency_seconds']={
            'mean':statistics.mean(durations) if durations else None,
            'median':statistics.median(durations) if durations else None,
            'p90':sorted(durations)[max(0,__import__('math').ceil(.9*len(durations))-1)] if durations else None,
            'max':max(durations) if durations else None}
    if any(extraction_status.get(s) for s in ('pending','incomplete','running')):report['degraded']=True
    if measured:
        permission_stats['processing_seconds']=round(sum(r['elapsed_s'] for r in measured),3)
        permission_stats['model_seconds']=round(sum(r.get('model_elapsed_s') or 0 for r in measured),3)
        permission_stats['mean_seconds_per_source']=round(permission_stats['processing_seconds']/len({r['job_id'] for r in measured}),3)
        permission_stats['measured_attempts']=len(measured)
        permission_stats['prompt_tokens']=sum((r.get('usage') or {}).get('prompt_tokens',0) for r in measured)
        permission_stats['completion_tokens']=sum((r.get('usage') or {}).get('completion_tokens',0) for r in measured)
        total=report['pipeline_seconds'].get('source_total',0)
        maintenance=[json.loads(r[0]) for r in core.db.execute("SELECT body FROM fact_attempts WHERE stage='binding_maintenance'")]
        maintenance_model=sum(r.get('model_elapsed_s') or 0 for r in maintenance)
        permission_stats['maintenance_model_seconds']=round(maintenance_model,3)
        permission_stats['total_source_model_seconds']=round(permission_stats['model_seconds']+maintenance_model,3)
        permission_stats['outside_model_seconds']=round(max(0,total-permission_stats['total_source_model_seconds']),3)
        # This ledger retains scalar timing even when text work caches are scrubbed.
        stages={}
        for r in core.db.execute('SELECT input_manifest,validation FROM permission_attempts WHERE validation IS NOT NULL'):
            value=json.loads(r['validation']);stage=json.loads(r['input_manifest']).get('stage','permission')
            item=stages.setdefault(stage,{'calls':0,'timed_calls':0,'seconds':0.,'model_seconds':0.,'prompt_tokens':0,'completion_tokens':0})
            item['calls']+=1
            if 'elapsed_s' in value:item['timed_calls']+=1;item['seconds']+=value['elapsed_s']
            item['model_seconds']+=value.get('model_elapsed_s') or 0
            for key in ('prompt_tokens','completion_tokens'):item[key]+=(value.get('usage') or {}).get(key,0)
        if maintenance:
            stages['binding_maintenance']={'calls':len(maintenance),'timed_calls':sum('elapsed_s' in r for r in maintenance),
                'seconds':sum(r.get('elapsed_s',0) for r in maintenance),'model_seconds':maintenance_model,
                **{key:sum((r.get('usage') or {}).get(key,0) for r in maintenance) for key in ('prompt_tokens','completion_tokens')}}
        if stages:report['memory_processing']['semantic_stages']=stages
        positions={}
        for row in core.db.execute("SELECT r.compartment,r.id,t.seconds FROM resources r JOIN permission_jobs j ON j.source_id=r.id JOIN pipeline_timings t ON t.job_id=j.id WHERE t.stage='source_total' ORDER BY r.rowid"):
            values=positions.setdefault(row['compartment'],[]);values.append(row['seconds'])
        buckets={'first_50':[],'51_to_100':[],'after_100':[]}
        for values in positions.values():
            buckets['first_50'].extend(values[:50]);buckets['51_to_100'].extend(values[50:100]);buckets['after_100'].extend(values[100:])
        permission_stats['latency_by_episode_position']={k:{'count':len(v),'mean':sum(v)/len(v) if v else None}
            for k,v in buckets.items()}
    if core.db.execute("SELECT 1 FROM privacy.sqlite_master WHERE name='workflow_timings'").fetchone():
        from agentic_memory.privacy.workflow_v20 import timing_summary
        report['workflow_v20']=timing_summary(core)
        preflight_path=run_dir/'provider-preflight.jsonl'
        preflights=[json.loads(line) for line in preflight_path.read_text().splitlines()] if preflight_path.exists() else []
        preflight_cost=sum((r.get('usage') or {}).get('cost',0) or 0 for r in preflights)
        report['workflow_v20']['provider_preflight_calls']=len(preflights)
        report['workflow_v20']['provider_preflight_cost']=preflight_cost
        report['workflow_v20']['all_provider_reported_cost']=report['workflow_v20']['model_cost']+preflight_cost
        latest_extractions={r['source_id']:r['status'] for r in core.db.execute(
            'SELECT source_id,status FROM fact_extractions ORDER BY rowid')}
        report['memory_processing']['historical_extraction_jobs']=report['memory_processing']['extraction_jobs']
        report['memory_processing']['extraction_jobs']={status:sum(s==status for s in latest_extractions.values())
            for status in set(latest_extractions.values())}
        report['permission_inference']['historical_incomplete']=not report['permission_inference']['complete']
        unresolved=core.db.execute("SELECT count(*) FROM permission_jobs WHERE status NOT IN ('complete','superseded','quarantined') AND id NOT LIKE 'host-recovery:%'").fetchone()[0]
        report['permission_inference']['complete']=not (unresolved or permission_stats['active_scoped_control_holds'])
        report['memory_processing']['ready_for_answers']=(report['permission_inference']['complete'] and not pending
            and all(s=='complete' for s in latest_extractions.values()))
        unknown=sum(not json.loads(r[0]).get('subjects') for r in core.db.execute(
            "SELECT body FROM fact_candidates WHERE state='active'"))
        report['memory_processing']['unattributed_candidates']=unknown
        if unknown:report['memory_processing']['ready_for_answers']=False
        report['degraded']=not report['memory_processing']['ready_for_answers']
        source_seconds=report.get('pipeline_seconds',{}).get('source_total',0)
        report['workflow_v20']['total_work_seconds']+=(source_seconds+report.get('pipeline_seconds',{}).get('evolution_total',0)
            +sum(r.get('seconds',0) for r in preflights))
        report['workflow_v20']['metric_semantics']={
            'source_latency':'Foreground source processing including foreground maintenance; host work reported separately.',
            'total_work_seconds':'Source foreground + host recovery/maintenance + answer-request + judge + enrichment wall time; model-call time and query-time maintenance are nested, not added twice.',
            'model_cost':'Reported provider usage only; unreported_usage_calls includes failures without billing receipts.'}
        report['permission_inference']['latency_seconds']['p95']=(
            report['workflow_v20']['latency_seconds'].get('source_foreground',{}).get('p95'))
        for values in permission_stats.get('latency_by_episode_position',{}).values():
            values['basis']='foreground; source_including_host reported in workflow_v20'
    versions={r[0].split('-',1)[0] for r in core.db.execute('SELECT version FROM permission_configurations')}
    if versions=={'21'}:
        from agentic_memory.privacy.workflow_v21 import summary as v21_summary
        from agentic_memory.privacy.diagnostics_v21 import summarize_predictions
        report.pop('workflow_v20',None)
        report['workflow_v21']=v21_summary(core)
        report['workflow_v21']['stage_diagnostics']=summarize_predictions(predictions)
        report['permission_inference']['latency_seconds']['p95']=report['workflow_v21']['source_seconds']['p95']
        for values in permission_stats.get('latency_by_episode_position',{}).values():
            values['basis']='v21 source processing including maintenance; no turn deadline'
    write_json(run_dir/'summary.json',report)
    write_json(run_dir/'boundary_results.json',done)
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gatemem-repo',type=Path,default=Path('/home/thaddus/GateMem'))
    parser.add_argument('--env-file',type=Path,default=ROOT/'.env')
    parser.add_argument('--run-dir',type=Path)
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--resume-code-revision',metavar='PREVIOUS_DIGEST',
                        help='Explicitly record a code revision while preserving prior predictions and the original manifest; requires --resume')
    parser.add_argument('--preflight',action='store_true')
    parser.add_argument('--plan',action='store_true')
    parser.add_argument('--offline',action='store_true',help='Explicit functional smoke mode: fake processors; no model-quality claims')
    parser.add_argument('--allow-paid-api',action='store_true',help='Explicit opt-in for a V20/V21 live run; omit for local-only validation')
    parser.add_argument('--max-boundaries',type=int,help='Additional boundaries this invocation; omitted means the full schedule')
    parser.add_argument('--max-checkpoints',type=int,help='Stop after this many chronological official predictions (including prior predictions on resume)')
    parser.add_argument('--no-evolution',action='store_true',help='Ablation only; full evolution is on by default')
    parser.add_argument('--permission-mode',choices=['inference','baseline'],default='inference',
                        help='Raw-stream inference (default) or metadata-assisted oracle baseline (not a raw-stream benchmark)')
    parser.add_argument('--v21-tag-weight',type=float,default=.12,help='V21 cheap tag-score ablation; zero disables it')
    parser.add_argument('--v21-context-tokens',type=int,default=6000,help='V21 conservative complete-evidence byte/token budget')
    parser.add_argument('--v21-rerank-candidates',type=int,default=0,help='V21 optional single-batch reranking; zero adds no call')
    parser.add_argument('--permission-version',default='19',help='21: AIM-inspired service/retrieval on V19 scheduling, without turn deadlines; 19: trusted personal OWNS with scoped grants (default); earlier versions retain their historical contracts')
    parser.add_argument('--episode-limit',type=int,help='Evaluate the first N episodes by ID; report exact coverage of that subset')
    args=parser.parse_args(argv)
    if args.max_checkpoints is not None and args.max_checkpoints<1:parser.error('--max-checkpoints must be positive')
    if args.resume_code_revision and not args.resume:parser.error('--resume-code-revision requires --resume')
    os.umask(0o077)
    load_environment(args.env_file)
    episodes,checkpoints=load_data(args.gatemem_repo/'bench/data/medical')
    events=schedule(episodes,checkpoints)
    if (len(episodes),len(checkpoints),len(events))!=(21,579,567):
        raise ValueError('This full medical evaluation requires the pinned 21/579/567 dataset')
    if args.episode_limit is not None:
        if not 1<=args.episode_limit<=len(episodes):parser.error('--episode-limit must be between 1 and 21')
        episodes=sorted(episodes,key=lambda e:e['episode_id'])[:args.episode_limit]
        selected={e['episode_id'] for e in episodes}
        checkpoints=[c for c in checkpoints if c['episode_id'] in selected]
        events=schedule(episodes,checkpoints)
    if args.plan:
        print(json.dumps({'episodes':len(episodes),'checkpoints':len(checkpoints),'boundaries':len(events),
            'query_boundaries':sum(bool(e['checkpoint_ids']) for e in events),
            'source_turns':sum(len(e['turns']) for e in episodes),'new_data_at_every_boundary':True},indent=2))
        return 0
    from agentic_memory.privacy.workflow_features import enabled
    if enabled(args.permission_version) and not args.offline and not args.allow_paid_api:
        parser.error('V20/V21 paid API calls are disabled. Use --offline for local checks; --allow-paid-api requires separate authorization.')
    if enabled(args.permission_version) and args.permission_mode!='inference':
        parser.error('V20/V21 requires raw-stream inference mode.')
    embedder,llm,judge=processors(args.offline)
    if args.preflight:
        print('Embedding:',embedder.space_id,'dimension',len(embedder.encode(['memory architecture'])[0]),flush=True)
        for role,model in [('memory-and-answer',llm),('judge',judge)]:
            if model:
                result=model.complete('Return JSON {"ok":true}.',{'probe':True})
                if result.get('ok') is not True:raise ValueError('Preflight schema failure')
                print(role,model.model,'available',flush=True)
        return 0
    if not args.run_dir:parser.error('--run-dir is required for an evaluation')
    run_dir=args.run_dir.resolve();run_dir.mkdir(parents=True,exist_ok=True);run_dir.chmod(0o700)
    lock=(run_dir/'run.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    journal=sqlite3.connect(str(run_dir/'evaluation.sqlite'))
    journal.row_factory=sqlite3.Row
    journal.execute('PRAGMA journal_mode=WAL');journal.execute('PRAGMA synchronous=FULL')
    journal.executescript('''
      CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS boundaries(id INTEGER PRIMARY KEY,state TEXT NOT NULL,delta_digest TEXT NOT NULL,turns INTEGER NOT NULL,queries INTEGER NOT NULL,seconds REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS official(id TEXT PRIMARY KEY,boundary INTEGER NOT NULL,prediction TEXT NOT NULL,score TEXT);
      CREATE TRIGGER IF NOT EXISTS immutable_prediction BEFORE UPDATE OF prediction ON official BEGIN SELECT RAISE(ABORT,'immutable official prediction'); END;
      CREATE TRIGGER IF NOT EXISTS immutable_score BEFORE UPDATE OF score ON official WHEN OLD.score IS NOT NULL BEGIN SELECT RAISE(ABORT,'immutable official score'); END;
    ''')
    manifest={'dataset_digest':digest({'episodes':episodes,'checkpoints':checkpoints}),
              'code_digest':digest({str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                                   for folder in (ROOT/'agentic_memory/privacy',Path(__file__).parent)
                                   for p in sorted(folder.iterdir())
                                   if p.is_file() and (p.suffix=='.py' or p.name=='fixture_policy.json')}),
              'embedding_fingerprint':getattr(embedder,'fingerprint',{'space_id':embedder.space_id}),
              'models':{'embedding':embedder.space_id,'memory':getattr(llm,'model','test:scripted'),
                        'judge':getattr(judge,'model',None)},'offline':args.offline,'evolution':not args.no_evolution,
              'evolution_schedule':'after-answers-durable-batches-of-4' if args.permission_mode=='inference' else 'baseline-policy-buckets',
              'schedule':'episode-id-then-turn-position-v2-with-data-tails',
              'permission_mode':args.permission_mode,'permission_version':args.permission_version,
              'agent_input_contract':'raw-stream-v1' if args.permission_mode=='inference' else 'metadata-assisted-oracle-baseline'}
    manifest['provider_routing']={role:openrouter_routing_preferences()
        for role,processor in [('memory',llm),('judge',judge)]
        if urlsplit(getattr(processor,'base_url','')).hostname=='openrouter.ai' or
            (urlsplit(getattr(processor,'base_url','')).hostname or '').endswith('.openrouter.ai')}
    manifest['source_schema_mode']=os.getenv('AMEM_SOURCE_SCHEMA_MODE','strict')
    family=args.permission_version.split('-',1)[0]
    if args.permission_mode=='inference' and family.isdigit() and int(family)>=13:
        manifest['permission_workflow']='single-pass'
        manifest['permission_max_tokens']=min(getattr(llm,'max_tokens',8192),int(os.getenv('AMEM_PERMISSION_MAX_TOKENS','3072')))
        if int(family)>=15:
            manifest.update(permission_workflow='combined-incremental',initial_requester_grants=0,
                            max_semantic_calls_per_turn=2,opaque_model_identities=True)
            manifest['incremental_max_tokens']=int(os.getenv('AMEM_INCREMENTAL_MAX_TOKENS','2048' if int(family)>=16 else '1024'))
        if int(family)>=19:
            manifest.update(personal_access_policy='trusted-personal-owns-across-tasks-v1',
                ownership_actions=['search','read','derive'],
                max_semantic_calls_per_turn_including_maintenance=2)
        if int(family)==20:
            manifest['judge_schema_mode']=os.getenv('AMEM_JUDGE_SCHEMA_MODE','strict')
            manifest['review_schema_mode']=os.getenv('AMEM_REVIEW_SCHEMA_MODE','strict')
            manifest['judge_max_tokens']=min(getattr(judge,'max_tokens',2048),4096)
            manifest['evolution_schedule']='after-answers-one-bounded-batch-no-final-drain-v20'
            manifest['workflow_v20']={'foreground_calls':2,'recovery_attempts_per_source':2,
                'source_target_mean_s':15,'source_target_p90_s':30,
                'pending_wait_s':5,'answer_target_p90_s':20,
                'publication_reserve_s':.25,
                'policy_tokens':512,'answer_tokens':4096,
                'judge_tokens':manifest['judge_max_tokens'],
                'host_schedule':'one-batch-between-source-turns-v1',
                'comparison_policy':'all-disclosed-subjects-required',
                'cache_version':'semantic-authority-v20'}
    if family=='21':
        from dataclasses import asdict
        from agentic_memory.privacy.retrieval_v21 import RetrievalConfig
        retrieval_config=RetrievalConfig(tag_weight=args.v21_tag_weight,
            max_context_tokens=args.v21_context_tokens,rerank_candidates=args.v21_rerank_candidates)
        manifest['workflow_v21']={'base':'v19','source_deadline_seconds':None,'answer_deadline_seconds':None,
            'foreground_semantic_calls_including_maintenance':2,'recovery':'explicit-batches-no-clock-deadline',
            'retrieval':asdict(retrieval_config),'diagnostics':'host-only-stage-state-v1'}
        manifest['review_schema_mode']=os.getenv('AMEM_REVIEW_SCHEMA_MODE','strict')
        manifest['workflow_v21']['policy_max_tokens']=512
        manifest['workflow_v21']['answer_max_tokens']=4096
    original_manifest,revisions=reconcile_manifest(journal,manifest,resume=args.resume,
                                                   previous_code_digest=args.resume_code_revision)
    # Preserve both the original experiment identity and its explicit lineage.
    if not (run_dir/'manifest.json').exists():write_json(run_dir/'manifest.json',original_manifest)
    write_json(run_dir/'effective_manifest.json',manifest)
    write_json(run_dir/'implementation_revisions.json',revisions)
    write_json(run_dir/'schedule.json',events)
    retry=RecoveryController(run_dir)
    core=PrivacyMemorySystem(run_dir/'memory.sqlite',embedder)
    scorer=GateMemScorer(args.gatemem_repo,judge)
    if enabled(args.permission_version):
        scorer.workflow_version=args.permission_version
        scorer.repair_path=run_dir/'judge-repair.json'
        def audit_judge(checkpoint,seconds,processor,error):
            compartment='encounter:'+digest(checkpoint['episode_id'])[:24]
            with core.transaction():
                details={'stage':'judge','phase':'judge','usage':getattr(processor,'usage',None),
                    'transport_attempts':getattr(processor,'transport_attempts',1),
                    'limits':getattr(processor,'last_effective_limits',None),'error':error}
                core.db.execute('INSERT INTO workflow_timings(compartment,stage,seconds,body) VALUES (?,?,?,?)',
                    (compartment,'model_call',seconds,encoded(details)))
                core.db.execute('INSERT INTO workflow_timings(compartment,stage,seconds,body) VALUES (?,?,?,?)',
                    (compartment,'judge_request',seconds,'{}'))
        scorer.audit=audit_judge
    by_id={e['episode_id']:e for e in episodes};by_query={q['checkpoint_id']:q for q in checkpoints}
    adapters={};count=0
    def get_adapter(eid):
        if eid not in adapters:
            permission_processor=EmptyPermissionProcessor() if args.offline else llm
            routes=list(dict.fromkeys([embedder.processor_id,llm.processor_id,permission_processor.processor_id]))
            if args.permission_mode=='inference':
                adapters[eid]=InferenceFixtureAdapter(core,episode_header(by_id[eid]),routes,permission_processor,
                    permission_version=args.permission_version,
                    progress=lambda message:print(message,flush=True),retry=retry)
            else:adapters[eid]=MedicalFixtureAdapter(core,by_id[eid],routes)
            if args.permission_version.split('-',1)[0]=='21':
                from agentic_memory.privacy.retrieval_v21 import configure
                configure(adapters[eid].updater,retrieval_config)
            core.register_processor(adapters[eid].service,llm)
        return adapters[eid]
    try:
        export_run(run_dir,journal,events,episodes,checkpoints,scorer,core,args.offline)
        for event in events:
            if args.max_checkpoints is not None and journal.execute('SELECT COUNT(*) FROM official').fetchone()[0]>=args.max_checkpoints:break
            existing=journal.execute('SELECT state FROM boundaries WHERE id=?',(event['boundary'],)).fetchone()
            if existing and existing[0]=='complete':continue
            if args.max_boundaries is not None and count>=args.max_boundaries:break
            count+=1;started=time.monotonic()
            eid=event['episode_id'];episode=by_id[eid]
            adapter=get_adapter(eid)
            turns=episode['turns'][event['start']:event['end']]
            if args.permission_mode=='inference':turns=[source_turn(turn) for turn in turns]
            delta_hash=digest(turns)
            print(f'boundary {event["boundary"]}/{len(events)}: ingest {len(turns)} new turns; ask {len(event["checkpoint_ids"])} original queries',flush=True)
            # Checkpoint placement/labels and the rest of the episode stay here.
            adapter.ingest_batch({'boundary':event['boundary']},turns,None if args.no_evolution else llm)
            with journal:
                journal.execute('INSERT INTO boundaries VALUES (?,\'ingested\',?,?,?,0) ON CONFLICT(id) DO UPDATE SET state=\'ingested\'',
                                (event['boundary'],delta_hash,len(turns),len(event['checkpoint_ids'])))
            # All predictions at a boundary precede judge feedback. No query writes
            # into canonical notes; every query sees the same committed memory.
            for cid in event['checkpoint_ids']:
                if args.max_checkpoints is not None and journal.execute('SELECT COUNT(*) FROM official').fetchone()[0]>=args.max_checkpoints:break
                if journal.execute('SELECT 1 FROM official WHERE id=?',(cid,)).fetchone():continue
                prediction=retry(lambda:answer(core,adapter,visible_query(by_query[cid]),llm),'answer '+cid)
                with journal:
                    journal.execute('INSERT INTO official VALUES (?,?,?,NULL)',(cid,event['boundary'],encoded(prediction)))
            for cid in event['checkpoint_ids']:
                row=journal.execute('SELECT prediction,score FROM official WHERE id=?',(cid,)).fetchone()
                if row is None:continue
                if row['score'] is not None:continue
                score=retry(lambda:scorer.score(by_query[cid],json.loads(row['prediction'])),'judge '+cid)
                with journal:journal.execute('UPDATE official SET score=? WHERE id=?',(encoded(score),cid))
            boundary_complete=journal.execute('SELECT COUNT(*) FROM official WHERE boundary=?',
                (event['boundary'],)).fetchone()[0]==len(event['checkpoint_ids'])
            if boundary_complete and args.permission_mode=='inference' and not args.no_evolution:
                adapter.flush_evolution(llm,max_batches=1)
            with journal:
                journal.execute('UPDATE boundaries SET state=?,seconds=? WHERE id=?',
                    ('complete' if boundary_complete else 'partial',time.monotonic()-started,event['boundary']))
            report=export_run(run_dir,journal,events,episodes,checkpoints,scorer,core,args.offline)
            print(f'committed {report["coverage"]["official_predictions"]}/{len(checkpoints)} predictions '
                  f'in {time.monotonic()-started:.1f}s; active artifacts {core.inventory(adapter.service)}',flush=True)
        if args.permission_mode=='inference' and not args.no_evolution:
            # Flush partial batches at invocation end, including a resume whose
            # source/query boundaries were already committed before interruption.
            pending={r[0] for r in core.db.execute(
                "SELECT DISTINCT compartment FROM evolution_queue WHERE status='pending'")}
            # Preserve the shared committed memory for any queries left at a
            # partially evaluated boundary by --max-checkpoints.
            partial={e['episode_id'] for e in events if journal.execute(
                "SELECT 1 FROM boundaries WHERE id=? AND state='partial'",(e['boundary'],)).fetchone()}
            for eid in by_id:
                if eid in partial:continue
                if 'encounter:'+digest(eid)[:24] in pending:
                    adapter=get_adapter(eid)
                    adapter.trusted_time({})
                    adapter.flush_evolution(llm,max_batches=1 if enabled(args.permission_version) else None,flush=True)
        report=export_run(run_dir,journal,events,episodes,checkpoints,scorer,core,args.offline)
        print(json.dumps(report,indent=2),flush=True)
        return 0
    except ProcessorBudgetFailure:
        export_run(run_dir,journal,events,episodes,checkpoints,scorer,core,args.offline)
        print('Run paused_budget; state saved. Restore provider allowance and resume with the same configuration.',flush=True)
        return 75
    except ModelContractError as exc:
        if not enabled(args.permission_version):raise
        export_run(run_dir,journal,events,episodes,checkpoints,scorer,core,args.offline)
        print('Run paused_contract; state and predictions saved. '+str(exc)+'; inspect recovery.json.',flush=True)
        return 76
    except BaseException:
        export_run(run_dir,journal,events,episodes,checkpoints,scorer,core,args.offline)
        raise
    finally:
        core.close();journal.close();lock.close()
