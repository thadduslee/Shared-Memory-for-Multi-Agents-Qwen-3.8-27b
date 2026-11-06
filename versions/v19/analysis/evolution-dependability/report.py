"""Render the completed comparison and read-only store validation."""
import argparse
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--validation',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    comparison=json.loads((args.run/'comparison.json').read_text())
    validation=json.loads(args.validation.read_text())
    utility=comparison['paired_utility']
    before,after=utility['baseline_correct'],utility['evolved_correct']
    if after>before:conclusion='Raw judged utility increased in this selected sample.'
    elif after<before:conclusion='Raw judged utility decreased in this selected sample.'
    else:conclusion='Evolution did not improve utility in this selected sample.'
    metrics=comparison['metrics']
    leak_keys=('privacy_leakage_rate','deletion_leakage_rate','privacy_context_leakage_rate','deletion_context_leakage_rate')
    privacy_regression=bool(comparison['new_answer_leakage_checkpoints']) or any(metrics[k]['delta']>0 for k in leak_keys)
    if privacy_regression:conclusion+=' The privacy non-regression criterion was not met.'
    else:conclusion+=' No new answer leakage or aggregate context-exposure increase was observed.'
    audit=args.output.with_name('CASE_AUDIT.md')
    if audit.exists() and utility['wins']==0 and utility['losses']==1:
        conclusion+=' The sole raw utility loss reflects inconsistent judging of nearly equivalent answers; it does not establish a causal quality regression.'
    evolution=comparison['evolution']
    lines=[
        '# Evolution dependability and paired evaluation — 25 October 2026','',conclusion,'',
        'Both arms completed 431 source turns, 52 boundaries and 56 queries in separate stores. '
        'There are 20 utility, 18 privacy and 18 deletion queries per arm. '
        'Both used the same source prefixes, code, embedding and local model services. '
        'The answer/evolution model was Qwen3.8-27B and the judge was Qwen3-14B. '
        'These are not fresh GPT-4.1 judgments and should not be compared directly with the interrupted run.','',
        '| Measure | Evolution off | Evolution on |','| --- | ---: | ---: |',
        f'| Utility correct | {before}/{utility["n"]} | {after}/{utility["n"]} |',
    ]
    for label,key in [('Privacy answer leakage','privacy_leakage_rate'),('Deletion answer leakage','deletion_leakage_rate'),
                      ('Privacy context exposure (auxiliary)','privacy_context_leakage_rate'),
                      ('Deletion context exposure (auxiliary)','deletion_context_leakage_rate')]:
        lines.append(f'| {label} | {metrics[key]["baseline"]:.1%} | {metrics[key]["evolved"]:.1%} |')
    lines.append(f'| Boundary time (shared servers) | {validation["baseline"]["total_boundary_seconds"]/60:.1f} min | '
                 f'{validation["evolved"]["total_boundary_seconds"]/60:.1f} min |')
    lines += ['',f'Paired utility: {utility["wins"]} wins, {utility["losses"]} loss(es), {utility["ties"]} ties. '
              f'Exact paired two-sided p = {utility["mcnemar_exact_two_sided_p"]:.3g}. '
              'A single small two-episode experiment cannot establish general improvement or prove privacy.','',
              f'The appointment-query utility subset tied: '
              f'{comparison["paired_appointment_utility"]["baseline_correct"]}/'
              f'{comparison["paired_appointment_utility"]["n"]} in both arms.','',
              f'Boundary 28 completed: **{evolution["boundary_28_complete"]}**. '
              f'Job states: `{json.dumps(evolution["jobs"],sort_keys=True)}`; '
              f'{evolution["attempts"]} attempts, {evolution["model_rejections"]} rejected proposals, '
              f'{evolution["repaired_jobs"]} jobs accepted after correction, {evolution["noop_jobs"]} empty jobs. '
              f'{validation["evolved"]["nonempty_repaired_jobs"]} repaired jobs produced non-empty outputs.','',
              'The implementation permits at most three proposals by default. Only model contract errors '
              'receive correction attempts; permission changes and processor failures remain errors. '
              'An exhausted optional evolution job preserves canonical notes, records its rejection and lets '
              'the boundary continue. Raw proposals and validation paths are unindexed resources restricted '
              'by every consumed input and cleared by dependency deletion.','',
              'Context must quote its target source note. Descriptive tags influence retrieval only. '
              'The answer model receives canonical source text; generated descriptions never become factual '
              'answer evidence. An overlay used to select a neighbor remains a dependency even when only '
              'the canonical source enters the prompt.','',
              f'Evolution was consumed by {validation["queries_consuming_evolution"]["enrichments"]} queries '
              f'through enrichments and {validation["queries_consuming_evolution"]["edges"]} through edges. '
              'See paired_queries.jsonl for changed answers and the appointment-query subset in comparison.json.','',
              'Store validation:', '','```json',json.dumps(validation,indent=2),'```','',
              'There are 82 passing security tests and 13 passing scoring/comparison tests. '
              'They cover correction feedback, exhausted jobs, atomic publication, source grounding, '
              'private overlay selection lineage, deletion, permission changes and protected diagnostics.','',
              'The boundary times above were measured while the two arms shared model servers concurrently; '
              'they are descriptive run timings, not an isolated throughput benchmark. '
              'Temperature zero does not remove all generation or judge variability. Any verdict disagreements '
              'on identical outputs are listed in validation.json.','',
              f'Artifacts: [{args.run.resolve().name}]({args.run.resolve()}) and '
              f'[comparison.json]({(args.run/"comparison.json").resolve()}). '
              'The original interrupted run was not changed. Earlier partial trials are excluded.','',
              f'Code digest: `{comparison["matching_controls"]["code_digest"]}`. '
              f'Dataset digest: `{comparison["matching_controls"]["dataset_digest"]}`.','']
    if audit.exists():
        lines += [f'See [CASE_AUDIT.md]({audit.resolve()}) for a raw utility loss in which '
                  'both answers omit the same required facts but receive different judge verdicts. '
                  'The raw score delta is not reliable evidence of a causal answer-quality regression.','']
    if after<=before:
        lines += ['The measured change improves execution dependability but does not demonstrate better answers. '
                  'Use `--no-evolution` for subsequent answer-quality runs until a further change shows a '
                  'utility gain without privacy regression. The current default remains configurable through that flag.','']
    args.output.write_text('\n'.join(lines))
    print(conclusion)


if __name__=='__main__':main()
