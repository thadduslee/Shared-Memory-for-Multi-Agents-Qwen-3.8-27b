Full V19 run: cited attribution, information types (every tag must be allowed), show all relevant facts, no owner review,
model-chosen per-type revocation and deletion, through SOCLAAS.

Live run: run.log  Progress: status.json  Results: results/summary.json
Follow: tail -f /home/thaddus/v19-evaluations/v19-model-controls-full-soclaas-20261104T103839Z/run.log
Compare: python analysis/v19-attribution-citation/compare.py /home/thaddus/v19-evaluations/v19-privacy-fallback-gpu45-20261103T153843Z /home/thaddus/v19-evaluations/v19-model-controls-full-soclaas-20261104T103839Z
Stop: kill -TERM $(pgrep -u thaddus -f '^python3 -u supervise.py$')

All 21 episodes; all chat roles use qwen3.8:27b at http://localhost:19000/v1; no GPU services.
Runs in its own session, outside tmux and VS Code; supervise.py logs the sender of any stop signal.
Input contract identity-metadata-stream-v1, so results are not raw-stream benchmark results.
