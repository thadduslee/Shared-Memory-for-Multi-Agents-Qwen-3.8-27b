"""Host-only context selection for incremental permission interpretation.

Routing is deliberately an over-approximation, never an authorization decision.
The small ledger index lets the learner discover records outside that selection.
Only live sources from the job's causal prefix are searchable/retrievable.
"""
from copy import deepcopy
import re

RECENT_TURNS = 6
BINDING_WINDOW = 2
PAGE_SIZE = 6
MAX_CONTEXT_ROUNDS = 3
MAX_CONTEXT_REQUESTS = 4
REQUEST_KINDS = ('source', 'search', 'activity', 'assignment', 'bindings')


def control_record(record):
    return deepcopy({k: v for k, v in record.items() if k != 'bindings'})


def evidence_ids(record):
    """Control evidence, excluding the unbounded history of fact bindings."""
    refs = set()
    for values in record.get('evidence', {}).values():
        refs.update(values)
    for key in ('performance', 'corroboration'):
        refs.update(record.get(key, []))
    for item in record.get('information_needs', []) + record.get('unresolved', []):
        refs.update(item['evidence'])
    refs.update(record.get('supersession', {}).get('evidence', []))
    return refs


def context_view(sources, state, new_source_id, requests=()):
    """Return a model projection; the canonical ledger remains untouched."""
    ids = list(sources)
    positions = {sid: i for i, sid in enumerate(ids)}
    selected_sources = set(ids[-RECENT_TURNS:])
    selected_sources.update([new_source_id] if new_source_id in sources else [])
    recent = [sources[sid] for sid in ids[-RECENT_TURNS:]]
    actors = {s['source']['participant'] for s in recent}
    subjects = {s['source'].get('subject') for s in recent} - {None}
    selected = {kind: set() for kind in state}
    selected['restriction'].update(state['restriction'])  # Never hide a denial.
    for aid, activity in state['activity'].items():
        if (activity['participant'] in actors or activity.get('subject') in actors | subjects
                or activity['status'] in {'observed', 'tentative'} or activity.get('unresolved')):
            selected['activity'].add(aid)
    for aid, assignment in state['assignment'].items():
        if (assignment['participant'] in actors or assignment['subject'] in actors | subjects
                or assignment['status'] == 'proposed'):
            selected['assignment'].add(aid)
            subjects.add(assignment['subject'])
    # A speaker may change ANOTHER recipient's access to the same subject.
    selected['assignment'].update(aid for aid, a in state['assignment'].items()
                                  if a['subject'] in subjects)
    pages = {}
    results = []

    def neighbors(sid):
        i = positions[sid]
        selected_sources.update(ids[max(0, i - 1):i + 2])

    for request in requests:
        kind, reference, offset = request['kind'], request['reference'], request['offset']
        found = []
        total = None
        if kind == 'source':
            neighbors(reference)
            found = [reference]
        elif kind == 'search':
            tokens = set(re.findall(r'\w+', reference.casefold()))
            ranked = []
            for sid, source in sources.items():
                body = ' '.join([source['text'], source['source']['participant'],
                                 source['source'].get('event_id', '')]).casefold()
                words = set(re.findall(r'\w+', body))
                score = len(tokens & words)
                if score:
                    ranked.append((score, positions[sid], sid))
            ranked.sort(reverse=True)
            total = len(ranked)
            found = [sid for _, _, sid in ranked[offset:offset + PAGE_SIZE]]
            for sid in found:
                neighbors(sid)
        elif kind in {'assignment', 'activity'}:
            selected[kind].add(reference)
        elif kind == 'bindings':
            selected['assignment'].add(reference)
            bindings = state['assignment'][reference].get('bindings', [])
            total = len(bindings)
            pages.setdefault(reference, set()).update(range(offset, min(total, offset + PAGE_SIZE)))
            found = [b['source_id'] for b in bindings[offset:offset + PAGE_SIZE] if b['source_id'] in sources]
            selected_sources.update(found)
        results.append({**request, 'source_ids': found, 'total_matches': total})

    # Include linked activities and explicit event-based expiry conditions.
    for aid in selected['assignment']:
        assignment = state['assignment'][aid]
        for activity_id in (assignment.get('activity_id'), assignment.get('self_use_activity'),
                            assignment.get('validity', {}).get('end_activity')):
            if activity_id in state['activity']:
                selected['activity'].add(activity_id)
    for restriction in state['restriction'].values():
        end = restriction.get('validity', {}).get('end_activity')
        if end in state['activity']:
            selected['activity'].add(end)

    view = {kind: {} for kind in state}
    index = {kind: {} for kind in state}
    for kind, records in state.items():
        for rid, record in records.items():
            index[kind][rid] = {k: deepcopy(record[k]) for k in (
                'participant', 'participants', 'subject', 'status', 'activity',
                'information_scope', 'activity_id', 'validity') if k in record}
            if rid not in selected[kind]:
                continue
            row = control_record(record)
            for sid in evidence_ids(row) & sources.keys():
                # Original requests are necessary to interpret short acceptances.
                neighbors(sid)
            if kind == 'assignment':
                bindings = record.get('bindings', [])
                count = len(bindings)
                positions_to_load = set(range(max(0, count - BINDING_WINDOW), count)) | pages.get(rid, set())
                row['binding_count'] = count
                row['bindings'] = [deepcopy(bindings[i]) for i in sorted(positions_to_load)]
                for binding in row['bindings']:
                    selected_sources.add(binding['source_id'])
                    selected_sources.update(evidence_ids(binding))
                    for decision in binding.get('restriction_decisions', []):
                        selected_sources.update(decision['evidence'])
            view[kind][rid] = row
    return ({sid: source for sid, source in sources.items() if sid in selected_sources}, view,
            {'mode': 'incremental', 'recent_turns': RECENT_TURNS, 'ledger_index': index,
             'retrieval_results': results, 'live_source_count': len(sources),
             'loaded_source_count': len(sources.keys() & selected_sources),
             'request_kinds': list(REQUEST_KINDS), 'page_size': PAGE_SIZE})
