"""Run and compare paired prequential evolution-off/on experiments.

The two arms ingest identical source prefixes in separate stores. Scoring uses
the same judge, and no checkpoint annotations or judge feedback enter memory.
"""
import argparse
import json
import math
import os
from pathlib import Path
import re
import sqlite3

from . import runner


def read_rows(path):
    rows=[json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    result={r['checkpoint_id']:r for r in rows}
    if len(result)!=len(rows):raise ValueError('Duplicate checkpoint IDs')
    return result


def paired_counts(pairs):
    wins=sum(not before and after for before,after in pairs)
    losses=sum(before and not after for before,after in pairs)
    changed=wins+losses
    p=min(1.,2*sum(math.comb(changed,k) for k in range(min(wins,losses)+1))/2**changed) if changed else 1.
    return {'n':len(pairs),'baseline_correct':sum(x for x,y in pairs),
            'evolved_correct':sum(y for x,y in pairs),'wins':wins,'losses':losses,
            'ties':len(pairs)-changed,'mcnemar_exact_two_sided_p':p}


def compare(root,gatemem_repo):
    dirs=[root/'baseline',root/'evolved']
    manifests=[json.loads((d/'manifest.json').read_text()) for d in dirs]
    reports=[json.loads((d/'summary.json').read_text()) for d in dirs]
    for key in ('dataset_digest','code_digest','embedding_fingerprint','models','offline','schedule'):
        if manifests[0][key]!=manifests[1][key]:raise ValueError(f'Arms differ in {key}')
    if manifests[0]['evolution'] or not manifests[1]['evolution']:
        raise ValueError('Expected evolution off then on')
    if any(not r['complete'] or r['mode']!='real-model-evaluation' for r in reports):
        raise ValueError('Both real-model arms must finish before comparison')
    scores=[read_rows(d/'scores.jsonl') for d in dirs]
    predictions=[read_rows(d/'official_predictions.jsonl') for d in dirs]
    if not set(scores[0])==set(scores[1])==set(predictions[0])==set(predictions[1]):
        raise ValueError('Paired checkpoint coverage differs')
    _,checkpoints=runner.load_data(gatemem_repo/'bench/data/medical')
    cases={c['checkpoint_id']:c for c in checkpoints}
    rows=[]
    for cid in sorted(scores[0]):
        before,after=(s[cid] for s in scores)
        query=cases[cid]['query_text']
        rows.append({'checkpoint_id':cid,'query_type':before['query_type'],
            'appointment_query':bool(re.search(r'\b(appointment|arrival|check.in|scheduled|schedule|time|Friday|Thursday)\b',query,re.I)),
            'utility_before':before['utility_correct'],'utility_after':after['utility_correct'],
            'privacy_before':before['privacy_leak'],'privacy_after':after['privacy_leak'],
            'deletion_before':before['deletion_leak'],'deletion_after':after['deletion_leak'],
            'passed_before':before['passed'],'passed_after':after['passed'],
            'answer_changed':predictions[0][cid]['output']['answer']!=predictions[1][cid]['output']['answer'],
            'evidence_changed':predictions[0][cid]['input_manifest']['memory_ids']!=predictions[1][cid]['input_manifest']['memory_ids']})
    utility=[r for r in rows if r['query_type']=='utility']
    utility_counts=paired_counts([(r['utility_before'],r['utility_after']) for r in utility])
    privacy_regressions=[r['checkpoint_id'] for r in rows if
        (r['privacy_after'] is True and r['privacy_before'] is not True) or
        (r['deletion_after'] is True and r['deletion_before'] is not True)]
    metrics={key:{'baseline':reports[0]['metrics'][key],'evolved':reports[1]['metrics'][key],
                  'delta':reports[1]['metrics'][key]-reports[0]['metrics'][key]}
             for key in ('utility_accuracy','privacy_leakage_rate','deletion_leakage_rate',
                         'privacy_context_leakage_rate','deletion_context_leakage_rate',
                         'privacy_e2e_leakage_rate','deletion_e2e_leakage_rate',
                         'action_accuracy','compliance_utility_score','compliance_utility_e2e_score')}
    appointment=paired_counts([(r['utility_before'],r['utility_after']) for r in utility if r['appointment_query']])
    with sqlite3.connect(f'file:{dirs[1]}/memory.sqlite?mode=ro',uri=True) as db:
        boundary28=db.execute("SELECT state FROM events WHERE id='boundary:28'").fetchone()
        errors=dict(db.execute("SELECT error_code,COUNT(*) FROM evolution_attempts WHERE status='rejected' GROUP BY error_code"))
        noops=db.execute("SELECT COUNT(*) FROM provenance WHERE outputs='[]'").fetchone()[0]
    report={'scope':reports[0]['coverage'],'baseline':str(dirs[0]),'evolved':str(dirs[1]),
        'matching_controls':{k:manifests[0][k] for k in ('dataset_digest','code_digest','models')},
        'metrics':metrics,'paired_utility':utility_counts,'paired_appointment_utility':appointment,
        'new_answer_leakage_checkpoints':privacy_regressions,
        'evolution':{**reports[1]['evolution'],'rejection_reasons':errors,'noop_jobs':noops,
                     'boundary_28_complete':bool(boundary28 and boundary28[0]=='complete')},
        'observed_utility_gain_without_new_answer_leaks':utility_counts['wins']>utility_counts['losses'] and not privacy_regressions,
        'limits':['Two selected episodes; this does not establish general improvement or prove absence of privacy leakage.',
                  'Single generation per arm at temperature zero; model and judge outputs may still vary.',
                  'Context exposure uses auxiliary pattern checks; answer disclosure uses the LLM judge.']}
    runner.write_json(root/'comparison.json',report)
    (root/'paired_queries.jsonl').write_text(''.join(json.dumps(r,sort_keys=True)+'\n' for r in rows))
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root',type=Path,required=True)
    parser.add_argument('--episode-limit',type=int,default=2)
    parser.add_argument('--gatemem-repo',type=Path,default=Path('/home/thaddus/GateMem'))
    parser.add_argument('--env-file',type=Path,default=runner.ROOT/'.env')
    parser.add_argument('--judge-provider',choices=['local','openrouter'],default='local')
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--report-only',action='store_true')
    args=parser.parse_args(argv)
    os.umask(0o077)
    root=args.output_root.resolve()
    root.mkdir(parents=True,exist_ok=True)
    runner.load_environment(args.env_file)
    if not args.report_only:
        if args.judge_provider=='openrouter':
            key=os.getenv('AMEM_OPENROUTER_API_KEY')
            if not key:parser.error('AMEM_OPENROUTER_API_KEY is required')
            os.environ.update({
                'AMEM_JUDGE_BASE_URL':os.getenv('AMEM_OPENROUTER_BASE_URL','https://openrouter.ai/api/v1'),
                'AMEM_JUDGE_MODEL':os.getenv('AMEM_OPENROUTER_JUDGE_MODEL','openai/gpt-4.1'),
                'AMEM_JUDGE_API_KEY':key})
        for arm in ('baseline','evolved'):
            directory=root/arm
            command=['--run-dir',str(directory),'--episode-limit',str(args.episode_limit),
                     '--gatemem-repo',str(args.gatemem_repo),'--env-file',str(args.env_file)]
            if arm=='baseline':command.append('--no-evolution')
            if args.resume and (directory/'manifest.json').exists():command.append('--resume')
            print(f'Starting paired arm: {arm}',flush=True)
            runner.main(command)
    report=compare(root,args.gatemem_repo)
    print(json.dumps(report,indent=2),flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
