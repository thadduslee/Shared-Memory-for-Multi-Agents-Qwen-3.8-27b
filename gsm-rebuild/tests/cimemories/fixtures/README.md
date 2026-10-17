# CIMemories-format fixtures

Hand-written, synthetic profiles in the upstream CIMemories layout (with `labels_combined`, as
`gsm cimemories label` writes it). Not taken from the official data. Attribute keys, raw values,
`information_domain`, `event`, bio fields other than the statements, and every `labels_*` field carry
`CANARY-*` strings so tests can prove none of it reaches a method.
