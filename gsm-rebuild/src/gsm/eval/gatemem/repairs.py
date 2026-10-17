"""Repairs of the failure reasons found in the v13 utility analysis.

All are on by default; each can be switched off (``gsm gatemem run --disable-repair NAME``) to measure it.

  delegate_floor  R3  whoever a scope-limited delegate is told, the people it acts for are told too
  keep_readers    R3  an update keeps the readers (and explicit denials) of the record it replaces
  scope_records   R4  each scope-limited group gets its own in-scope version, not the first one offered
  carry_forward   R5  an update that restates only part of a fact keeps the parts still in effect
  partial_delete  R6  a deletion removes only the targeted part of a record that also holds current facts
  delete_history  R6  a deletion request also reaches earlier versions of the records it targets; deleting a
                      record deletes its earlier versions
  activity_notes  U1  a chatter message stating the speaker's own role or task is kept, speaker-only, values masked
  own_activity    U1  the answerer also gets the asker's own superseded records whose information is still held
                      (the update chain ends in a current record) and that held no sensitive values, values
                      masked, never ones restricted from them
"""

REPAIRS = frozenset(
    {
        "delegate_floor",
        "keep_readers",
        "scope_records",
        "carry_forward",
        "partial_delete",
        "delete_history",
        "activity_notes",
        "own_activity",
    }
)
