"""Host-only scaling probe with identical scripted decisions, no API calls."""
import argparse
import cProfile
import io
import json
import pstats
import statistics
import time
from pathlib import Path
from unittest.mock import patch

from security_tests.test_incremental_permissions import IncrementalTests
from agentic_memory.privacy.permissions import PermissionUpdater


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--turns',type=int,default=150);args=parser.parse_args()
    # A saved pre-change checkout can use version 16 for the same scripted
    # literal grants and facts; source decisions and read boundaries are equal.
    h=IncrementalTests();h.setUp()
    import os
    h.updater=PermissionUpdater(h.core,h.host,'work',h.bootstrap,h.model,version=os.getenv('AMEM_BENCHMARK_VERSION','17'))
    durations=[];profile=cProfile.Profile()
    try:
        # External structured-output validation is normally performed by the
        # provider. Exclude the scripted fixture's local provider emulation.
        with patch('jsonschema.validate'):
            for name in ['helper','two','three','four','five']:h.turn('I can arrange a pickup.',actor=name)
            h.policy=lambda p:[h.grant(p,name,slot=i) for i,name in enumerate(['helper','two','three','four','five'])]
            h.turn('All five named helpers may read my pickup instructions.')
            h.policy=lambda p:[];h.classify=lambda p,s:'instruction'
            for i in range(args.turns):
                if i==args.turns-1:profile.enable()
                start=time.monotonic();h.turn(f'Pickup parcel {i} at loading station {i+1000}.')
                durations.append(time.monotonic()-start)
                if i==args.turns-1:profile.disable()
        stats={'source_turns':args.turns,'readers':5,'first_25_mean':statistics.mean(durations[:25]),
            'last_25_mean':statistics.mean(durations[-25:]),'durations':durations,
            'pending_reviews':len(json.loads(h.core.db.execute('SELECT validation FROM permission_jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0])['pending_bindings']),
            'readable_last_reader':len(h.selected('five'))}
        args.output.mkdir(parents=True,exist_ok=False)
        (args.output/'measurements.json').write_text(json.dumps(stats,indent=2)+'\n')
        out=io.StringIO();pstats.Stats(profile,stream=out).sort_stats('cumulative').print_stats(25)
        (args.output/'profile.txt').write_text(out.getvalue())
        print(json.dumps({k:v for k,v in stats.items() if k!='durations'},indent=2))
    finally:h.tearDown()


if __name__=='__main__':main()
