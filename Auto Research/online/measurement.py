"""Independent measurement under a pinned version; never an adaptation input."""
from online.schedule import (
    build_schedule,
    episode_header,
    visible_checkpoint,
)
from online.storage import digest, write_json
from online.validation import code_manifest


async def measure_holdout(dataset, workspace, backend, output):
    """Caller supplies a separate evaluation set and authorizes any model costs.

    This function has no runner journal, Architect or Developer interface. Results
    are written outside repair history and cannot select candidate revisions.
    """
    pinned = code_manifest(workspace)
    checkpoints = {c['checkpoint_id']: c for c in dataset.checkpoints}
    images, cursors, results = {}, {}, []
    previous_policy = backend.policy_provider
    previous_annotations = backend.benchmark_annotations
    backend.policy_provider = getattr(dataset, 'policy_for_episode', lambda h: {})
    backend.benchmark_annotations = bool(getattr(dataset,'benchmark_annotations',False))
    try:
        for event in build_schedule(dataset):
            eid = event['episode_id']
            episode = dataset.episodes_by_id[eid]
            header = getattr(dataset,'header_for_episode',episode_header)(episode)
            start, end = cursors.get(eid, -1)+1, event['turn_position']+1
            from copy import deepcopy
            image, _ = await backend.memory(workspace, images.get(eid), header,
                delta=getattr(dataset,'sanitize_turns',deepcopy)(episode['turns'][start:end]))
            images[eid], cursors[eid] = image, end-1
            for cid in event['checkpoint_ids']:
                cp = checkpoints[cid]
                prediction = await backend.predict(workspace, image, header, visible_checkpoint(cp))
                score = await backend.judge(prediction, cp)
                results.append({'checkpoint_id': cid, 'prediction': prediction, 'score': score,
                                'boundary': event['event_boundary_index'], 'memory_digest': digest(image.hex())})
    finally:
        backend.policy_provider = previous_policy
        backend.benchmark_annotations = previous_annotations
    if code_manifest(workspace) != pinned:
        raise ValueError('measurement workspace changed')
    report = {'measurement': 'independent_holdout', 'used_to_select_repairs': False,
              'pinned_code': pinned, 'dataset_digest': digest({'episodes': dataset.episodes,
                  'checkpoints': dataset.checkpoints}), 'results': results,
              'count': len(results), 'passed': sum(bool(r['score'].get('passed')) for r in results)}
    report['unknown'] = sum(r['score'].get('passed') is None for r in results)
    report['known_evaluations'] = len(results)-report['unknown']
    write_json(output, report)
    return report
