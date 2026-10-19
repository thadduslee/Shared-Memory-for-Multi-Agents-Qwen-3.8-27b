"""Public stream contract. Source adapters supply explicit policy and evaluation."""
from copy import copy, deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass
class StreamDataset:
    episodes: list[dict[str, Any]]
    checkpoints: list[dict[str, Any]]

    @property
    def episodes_by_id(self):
        return {ep['episode_id']: ep for ep in self.episodes}

    @staticmethod
    def policy_for_episode(episode):
        return {}  # an inferred domain never grants access

    @staticmethod
    def sanitize_turns(turns):
        return deepcopy(turns)  # source keys are data, not benchmark annotations


def extend_dataset(dataset, episodes, checkpoints):
    """Append authenticated source envelopes; never revise earlier inputs."""
    result=copy(dataset)
    result.episodes=deepcopy(dataset.episodes)
    result.checkpoints=deepcopy(dataset.checkpoints)
    known={ep['episode_id']:ep for ep in result.episodes}
    old_positions={eid:len(ep['turns'])-1 for eid,ep in known.items()}
    seen=set()
    for fragment in deepcopy(episodes):
        eid=fragment['episode_id']
        if eid in seen or not fragment.get('turns'):
            raise ValueError('stream extension requires distinct episodes and nonempty turn batches')
        seen.add(eid)
        if eid in known:
            old=known[eid]
            if {k:v for k,v in fragment.items() if k!='turns'} != {k:v for k,v in old.items() if k!='turns'}:
                raise ValueError('stream extension cannot change earlier episode metadata')
            ids={t['turn_id'] for t in old['turns']}
            if ids & {t['turn_id'] for t in fragment['turns']}:
                raise ValueError('stream extension cannot replace an earlier turn')
            old['turns'].extend(fragment['turns'])
        else:
            known[eid]=fragment
            result.episodes.append(fragment)
    old_ids={c['checkpoint_id'] for c in result.checkpoints}
    for cp in deepcopy(checkpoints):
        if cp['checkpoint_id'] in old_ids:
            raise ValueError('stream extension cannot replace an earlier question')
        old_ids.add(cp['checkpoint_id'])
        ep=known.get(cp['episode_id'])
        positions={t['turn_id']:i for i,t in enumerate(ep['turns'])} if ep else {}
        if positions.get(cp['as_of_turn_id'],-1)<=old_positions.get(cp['episode_id'],-1):
            raise ValueError('new questions must address newly appended data')
        result.checkpoints.append(cp)
    return result
