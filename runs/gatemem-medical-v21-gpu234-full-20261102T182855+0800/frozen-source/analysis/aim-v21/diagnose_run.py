"""Read-only historical stage diagnosis; never rewrites predictions or scores."""
import argparse
from collections import Counter
import json
from pathlib import Path


def analyze(run):
    predictions = [json.loads(line) for line in (run/'official_predictions.jsonl').read_text().splitlines() if line.strip()]
    scores = {r['checkpoint_id']: r for r in
              (json.loads(line) for line in (run/'scores.jsonl').read_text().splitlines() if line.strip())}
    rows = []
    for prediction in predictions:
        manifest = prediction.get('input_manifest', {})
        processing = manifest.get('processing', {})
        observations = []
        if processing.get('scoped_control_holds'):
            observations.append('unresolved_control_holds_in_compartment')
        if processing.get('incomplete_extractions') or processing.get('unattributed_candidates'):
            observations.append('extraction_or_attribution_incomplete')
        if processing.get('pending_binding_reviews'):
            observations.append('fact_assignment_pairs_pending')
        if not manifest.get('memory_ids'):
            observations.append('no_authorized_evidence_retrieved')
        elif manifest.get('answer_decision') and not any(s == 'supported' for s in manifest['answer_decision']):
            observations.append('retrieved_packet_does_not_support_requested_parts')
        score = scores.get(prediction['checkpoint_id'], {})
        rows.append({'checkpoint_id': prediction['checkpoint_id'], 'as_of_turn_id': prediction['as_of_turn_id'],
            'action': prediction['output']['action'], 'strict_over_refusal': score.get('over_refusal'),
            'strict_utility_correct': score.get('utility_correct'), 'observed_stages': observations,
            'recorded_processing': processing, 'v21_diagnosis': manifest.get('diagnosis_v21'),
            'causal_interpretation': 'checkpoint observations; individual blockers require exact-fact/subject matching'})
    counts = Counter(stage for row in rows for stage in row['observed_stages'])
    return {'run': str(run.resolve()), 'strict_scores_modified': False,
            'future_state_used': False, 'counts': dict(counts), 'checkpoints': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as handle:
        json.dump(analyze(args.run), handle, indent=2)
        handle.write('\n')
    args.output.chmod(0o600)
    print(args.output)


if __name__ == '__main__':
    main()
