#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /home/thaddus/a-mem-variant
run_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec > >(tee -a "$run_dir/launcher.log") 2>&1
trap 'status=$?; printf "%s\n" "$status" > "$run_dir/exit-status"' EXIT
if [[ -e "$run_dir/evaluation.sqlite" ]]; then
  echo 'This launcher starts a fresh run. To continue an existing run, use resume.sh.' >&2
  exit 2
fi
python3 -u - <<'HEALTH'
import time
import urllib.request

ports = {8000, 8001}
deadline = time.monotonic() + 1800
last = 0
while ports:
    for port in sorted(ports):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3) as response:
                if response.status == 200:
                    ports.remove(port)
                    print(f'Model service {port} healthy', flush=True)
        except (OSError, ValueError):
            pass
    if not ports:
        break
    if time.monotonic() > deadline:
        raise SystemExit('Model startup timed out; inspect the model and judge tmux windows and server logs.')
    if time.monotonic() - last >= 30:
        print('Waiting for local model services:', sorted(ports), flush=True)
        last = time.monotonic()
    time.sleep(5)
HEALTH
bash scripts/run_gatemem.sh --permission-mode inference --permission-version 12 --run-dir "$run_dir"
