# V19 cited attribution: validation

`replay.py` recomputes attribution from a finished run's saved model decisions with
the real `compile_attribution`. It makes no model calls and opens stores read-only.
The saved decisions came from a prompt without names, roles or `mentioned_people`,
so its numbers estimate the citation rules only and are not benchmark scores.

    python analysis/v19-attribution-citation/replay.py RUN_DIR
