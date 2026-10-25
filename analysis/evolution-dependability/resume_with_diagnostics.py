"""Observe rejected citations without changing the pinned runner or model requests.

Only invalid, unreleased outputs are recorded in private evaluation storage.
No model output, prompt, scoring rule, or authorization decision is changed.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from evaluation.gatemem_medical import runner


def main():
    os.umask(0o077)
    arguments = sys.argv[1:]
    index = arguments.index('--run-dir')
    directory = Path(arguments[index + 1]).resolve() / 'rejected_answers'
    directory.mkdir(mode=0o700, exist_ok=True)
    factory = runner.processors

    def observed_processors(offline=False):
        embedder, llm, judge = factory(offline)
        complete = llm.complete

        def observed_complete(system, payload, **kwargs):
            output = complete(system, payload, **kwargs)
            if isinstance(payload, dict) and isinstance(payload.get('evidence'), list):
                evidence_ids = [item['id'] for item in payload['evidence']]
                cited = output.get('used_record_ids')
                valid = (isinstance(cited, list) and all(isinstance(item, str) for item in cited)
                         and set(cited) <= set(evidence_ids))
                if not valid:
                    path = directory / (uuid.uuid4().hex + '.json')
                    runner.write_json(path, {
                        'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
                        'query': payload.get('query'), 'as_of_turn_id': payload.get('as_of_turn_id'),
                        'requester_id': payload.get('requester_id'), 'model': llm.model,
                        'evidence_ids': evidence_ids, 'output': output,
                        'unexpected_ids': [item for item in cited if item not in evidence_ids]
                                          if isinstance(cited, list) else None,
                        'note': 'Recorded before the existing runner rejects this output; '
                                'the observer does not release, alter, or accept it.'})
                    print(f'Rejected citation diagnostic: {path}', flush=True)
            return output

        llm.complete = observed_complete
        return embedder, llm, judge

    runner.processors = observed_processors
    return runner.main(arguments)


if __name__ == '__main__':
    raise SystemExit(main())
