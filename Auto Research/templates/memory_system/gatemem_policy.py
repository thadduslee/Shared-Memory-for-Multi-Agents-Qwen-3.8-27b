"""Explicit GateMem adapter policy; the generic memory core never infers grants."""
import json
from pathlib import Path


def benchmark_policy(episode):
    if episode.get('domain') != 'medical':
        return {}
    return json.loads((Path(__file__).parent/'policies'/'medical.json').read_text())
