# V21 citation transport repair — 2 November 2026

The first native GPU-run answer failed because `CompletionProcessor` expanded
short citation handles (`m1`, etc.) to full record IDs before validating against
the model-facing schema, whose allowed values were still the short handles.
The three rejected outputs matched the supplied evidence IDs and the original
schema. Repeating generation could not repair this host-side ordering error.

The processor now restores property names, validates the complete model-facing
schema, expands reference values, and validates the original canonical schema.
Unknown aliases, unprovided record IDs and duplicate citations remain rejected.
The same ordering applies to compact evolution references. No model call was
added, and permission inference, access checks and release checks were unchanged.

Twenty new transport regressions exercise the real processor with mocked HTTP
responses across V19/V20/V21, strict/JSON/portable modes, negative citation cases,
corrupted restoration and evolution references. The full suite passed all
**712 tests**. See [regression.xml](regression.xml) and
[validation.json](validation.json).

[saved-answer-replay.json](saved-answer-replay.json) records three successful
offline replays. Since the old traces contain post-restoration outputs, these
reconstruct the equivalent short-ID replies from the saved evidence mapping;
they are not raw provider-byte recordings or published predictions.

The user explicitly requested repair and continuation of the GPU 2–4 run. Its
original frozen source, failed attempts, manifests and pre-resume checkpoint
were preserved. A separate source revision changes only `privacy/processors.py`
and is resumed with `--resume-code-revision` acknowledging the previous digest.
Only the exhausted first-answer retry allowance was renewed by an appended,
audited host event; permission budgets were not reset. The continuation generates
a fresh answer against the saved source prefix rather than publishing a failed
draft. The runner records the implementation revision in its manifest lineage.
