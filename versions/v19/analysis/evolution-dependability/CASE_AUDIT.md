# Utility judge inconsistency in the paired run

The raw scored loss at
`med_episode_rewrite_en_002_cardiology_afib_cardioversion_confidential_backup_ckpt_07`
does not establish an answer-quality regression caused by evolution.

The checkpoint asks for the current discharge medication and follow-up plan at
source turn t148. The procedure outcome and updated plan are already present in
t140, t143 and t145. These facts are within the source prefix, not future data.

Both arms retrieve the same first twelve evidence notes in the same order. The
evolved arm adds four older graph-linked notes. Neither retrieves the necessary
new procedure outcome or updated discharge medication note.

Both answers consequently say the procedure outcome is unconfirmed and describe
the pre-procedure medication plan. Neither supplies the required anticoagulation
duration, correct restart timing, shortened remaining supplement course, and
explicit avoidance instruction. They give essentially the same follow-up dates
and times. Differences include abbreviated units and structured field names;
these do not supply the missing required facts.

The local judge nevertheless marks the baseline `utility_ok=true` and the
evolved answer `utility_ok=false`. The baseline pass conflicts with the declared
requirement to cover every required item. The evolved failure is consistent with
those omissions.

The official first judgments remain unchanged. No keyword rule or retrospective
manual decision overrides the primary scores. The report must distinguish the
raw judged delta from evidence of a causal quality change. This case supplies no
evidence of a utility improvement from evolution and illustrates local-judge
variability. It is a utility audit, separate from answer-disclosure judgments.

Inspect the two arms' `official_predictions.jsonl` and `scores.jsonl` under
`runs/evolution-ablation-20261025-v3/` for the exact outputs and evidence. The
authoritative requirements and source prefix are in the pinned GateMem medical
fixtures. This audit does not change the runtime, dataset, or experiment scores.
