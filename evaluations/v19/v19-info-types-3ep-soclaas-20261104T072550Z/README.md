V19 short run: information types with the host access table, show all relevant facts, no owner review, through SOCLAAS.

Live run: run.log  Progress: status.json  Results: results/summary.json
Follow: tail -f /home/thaddus/v19-evaluations/v19-info-types-3ep-soclaas-20261104T072550Z/run.log
Compare: python analysis/v19-attribution-citation/compare.py /home/thaddus/v19-evaluations/v19-cited-attribution-3ep-soclaas-20261104T041323Z /home/thaddus/v19-evaluations/v19-info-types-3ep-soclaas-20261104T072550Z

First 3 episodes by ID; all chat roles use qwen3.8:27b at http://localhost:19000/v1; no GPU services.
Runs in its own session, outside tmux and VS Code; supervise.py logs the sender of any stop signal.
Input contract identity-metadata-stream-v1, so results are not raw-stream benchmark results.
