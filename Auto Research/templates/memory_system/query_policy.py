"""Candidate-editable guidance; the host SQL gateway remains authoritative."""
QUERY_GUIDANCE = (
    'Choose fixed retrieval when it suffices. For relational retrieval, use the '
    'provided current schema and parameterized SELECT joins. Return canonical '
    'memory_id columns for every contributing source. Never invent tables.'
)
