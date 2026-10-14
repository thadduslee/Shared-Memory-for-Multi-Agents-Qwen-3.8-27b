"""Scheduling on public identities and episode-local turn position only."""
from collections import defaultdict

from gatemem_adapter import (  # noqa: F401 (public adapter export)
    strip_hidden_fields,
    visible_checkpoint,
)

POLICY = 'episode-id-then-turn-position-v1'


def episode_header(episode):
    return strip_hidden_fields({k: episode[k] for k in ('episode_id', 'domain', 'entities') if k in episode})


def build_schedule(dataset, policy=POLICY):
    if policy != POLICY:
        raise ValueError(f'unknown ordering policy: {policy}')
    episodes = dataset.episodes_by_id
    if len(episodes) != len(dataset.episodes):
        raise ValueError('duplicate episode ID')
    positions = {}
    for eid, ep in episodes.items():
        ids = [t['turn_id'] for t in ep['turns']]
        if len(ids) != len(set(ids)):
            raise ValueError(f'duplicate turn ID in {eid}')
        positions[eid] = {tid: i for i, tid in enumerate(ids)}
    groups, seen = defaultdict(list), set()
    for cp in dataset.checkpoints:
        cid, eid, tid = cp['checkpoint_id'], cp['episode_id'], cp['as_of_turn_id']
        if cid in seen:
            raise ValueError(f'duplicate checkpoint ID: {cid}')
        seen.add(cid)
        if eid not in positions or tid not in positions[eid]:
            raise ValueError(f'unknown event boundary: {eid}/{tid}')
        groups[eid, positions[eid][tid], tid].append(cid)
    return [{'event_boundary_index': i, 'episode_id': eid, 'as_of_turn_id': tid,
                 'turn_position': pos, 'checkpoint_ids': sorted(ids)}
            for i, ((eid, pos, tid), ids) in enumerate(sorted(groups.items()), 1)]


def structural_summary(turns):
    # No text, query bodies, checkpoint identities or gold fields.
    return {'n_turns': len(turns), 'shapes': sorted({
        tuple(sorted(t.keys())) for t in turns}),
        'kinds': sorted({str(t.get('turn_kind', '')) for t in turns}),
        'speaker_shapes': sorted({tuple(sorted((t.get('speaker') or {}).keys())) for t in turns})}


def compatibility(summary, manifest):
    required = {'turn_id', 'text', 'speaker'}
    supported = set(manifest['supported_turn_fields'])
    bad = any(not required.issubset(shape) or not set(shape).issubset(supported)
              for shape in summary['shapes'])
    return {'decision': 'CODE_OR_SCHEMA_CHANGE' if bad else 'NO_CHANGE',
            'reason': 'unrecognized turn structure' if bad else 'supported turn structure',
            'structural_summary': summary, 'schema_version': manifest['schema_version']}
