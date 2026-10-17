A participant asks for earlier information to be removed from a shared memory. You are shown their message and
a numbered list of records this participant can see, including earlier versions. Pick the records that hold
the information to remove.

For each pick, give `part`: the exact substring of the record to remove when the record ALSO holds other
information that should stay; give null when the whole record should go. Choose none if nothing matches.

Reply with one JSON object only: {"targets": [{"id": "<record id>", "part": null}]}
