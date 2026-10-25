"""Read-only experiment audit; temporary policy replay, no model/network calls.

Run from any directory with python3. Reports are written beside this script.
Test vectors are used ONLY to exercise ingestion; no retrieval-quality claims.
"""
from pathlib import Path
import collections
import csv
import hashlib
import json
import os
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from agentic_memory.privacy.system import PrivacyMemorySystem, digest
from agentic_memory.privacy.processors import HashEmbedder
from evaluation.gatemem_medical.adapter import MedicalFixtureAdapter, schedule, visible_query

DATA = Path('/home/thaddus/GateMem/bench/data/medical')
SOURCE = ROOT / 'runs/gatemem-medical-full-20261020-v3'
REJUDGE = ROOT / 'runs/gatemem-medical-full-20261020-v3-gpt-4.1-openrouter'
STAFF = {'scheduler', 'nurse', 'pharmacist', 'social_worker', 'reception', 'lab_tech'}


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def scope_for(cp):
    """Analyst labels for reporting only; never installed as permissions."""
    role, query = cp['asker']['role'], cp['query_text'].lower()
    if role == 'scheduler':
        return 'appointments, changes, arrival and applicable preparation instructions'
    if role == 'lab_tech':
        return 'repeat-lab scheduling date (not unrestricted lab-result access)'
    if 'contact' in query or 'callback' in query or 'access' in query:
        return 'current contact routing' + (' and sharing/revocation status' if 'access' in query else '')
    if role == 'social_worker':
        return 'transportation assistance and backup ride arrangements'
    if role == 'pharmacist' or 'medication' in query or 'medicine' in query or 'recovery list' in query:
        return 'active medication/home-care instructions and stopped medicines'
    if role == 'reception' or 'schedule' in query or 'booking' in query or 'reminder' in query:
        return 'appointments, arrival/preparation' + (' and current contact routing' if 'late reminder' in query else '')
    if any(word in query for word in ('trigger', 'red flag', 'warning', 'precaution', 'symptom')):
        return 'current return/call precautions'
    if 'staging' in query or 'lab recap' in query:
        return 'current clinical/laboratory summary for the involved care task'
    if 'monitoring' in query:
        return 'monitoring tests and follow-up timing'
    if 'prep' in query or 'after-care' in query:
        return 'procedure preparation or after-care instructions'
    return 'current care/follow-up instructions for the involved task'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    os.umask(0o077)
    episodes = read_jsonl(DATA / 'episodes.jsonl')
    checkpoints = read_jsonl(DATA / 'checkpoints.jsonl')
    by_episode = {e['episode_id']: e for e in episodes}
    by_cp = {c['checkpoint_id']: c for c in checkpoints}
    predictions = {p['checkpoint_id']: p for p in read_jsonl(SOURCE / 'official_predictions.jsonl')}
    scores = read_jsonl(REJUDGE / 'scores.jsonl')
    selected = {}
    for s in scores:
        cp = by_cp[s['checkpoint_id']]
        pred = predictions[s['checkpoint_id']]
        if (s['query_type'] == 'utility' and cp['asker']['role'] in STAFF
                and pred['output']['action'] != 'answer'
                and not pred['output']['retrieved_memory']):
            selected[cp['checkpoint_id']] = (cp, pred)
    assert len(selected) == 118
    config = json.loads((ROOT / 'evaluation/gatemem_medical/fixture_policy.json').read_text())
    entries = []
    with tempfile.TemporaryDirectory(prefix='amem-policy-audit-') as tmp:
        core = PrivacyMemorySystem(Path(tmp) / 'memory.sqlite', HashEmbedder())
        adapters = {}
        try:
            for event in schedule(episodes, checkpoints):
                episode = by_episode[event['episode_id']]
                if episode['episode_id'] not in adapters:
                    adapters[episode['episode_id']] = MedicalFixtureAdapter(
                        core, episode, [core.embedder.processor_id, 'vllm:qwen'])
                adapter = adapters[episode['episode_id']]
                adapter.ingest_batch(event, episode['turns'][event['start']:event['end']], None)
                for cid in event['checkpoint_ids']:
                    if cid not in selected:
                        continue
                    cp, pred = selected[cid]
                    pid = cp['asker']['principal_id']
                    prefix = episode['turns'][:event['end']]
                    own = [t for t in prefix if t['speaker']['principal_id'] == pid]
                    relationships = [r for r in episode['entities'].get('relationships', []) if pid in r.values()]
                    ctx = adapter.query_context(visible_query(cp))
                    memories = list(core.db.execute(
                        "SELECT id,policy_id FROM resources WHERE compartment=? AND state='active' AND kind='memory'",
                        (episode['episode_id'],)))
                    readable = sum(core.allowed(ctx, r['id'], 'read') for r in memories)
                    searchable = sum(core.allowed(ctx, r['id'], 'search') for r in memories)
                    groups = [r[0] for r in core.db.execute('SELECT group_id FROM memberships WHERE principal_id=?', (pid,))]
                    own_notes = []
                    for t in own:
                        rid = f"note:obs:{episode['episode_id']}:{t['turn_id']}"
                        stored = core.db.execute('SELECT state,policy_id,attributes FROM resources WHERE id=?', (rid,)).fetchone()
                        own_notes.append({'turn_id': t['turn_id'], 'memory_id': rid,
                                          'state': stored['state'], 'policy_id': stored['policy_id'],
                                          'subjects': json.loads(stored['attributes']).get('subjects', []),
                                          'read_allowed': core.allowed(ctx, rid)})
                    # Adjacent turns provide context without asserting that speech is a grant.
                    context_positions = set()
                    for i, t in enumerate(prefix):
                        if t['speaker']['principal_id'] == pid:
                            context_positions.update(range(max(0, i-1), min(len(prefix), i+2)))
                    permission_terms = ('access', 'scope', 'permission', 'restrict', 'assigned', 'covering', 'care team')
                    relevant_policy_turns = [t for t in prefix if
                        any(term in t['text'].lower() for term in permission_terms) and
                        (t['speaker']['principal_id'] in {adapter.primary, *config['control_principals']}
                         or t['speaker']['principal_id'] == pid)]
                    entries.append({
                        'checkpoint_id': cid, 'episode_id': episode['episode_id'],
                        'as_of_turn_id': cp['as_of_turn_id'], 'boundary': event['boundary'],
                        'asker': cp['asker'], 'query_text': cp['query_text'],
                        'primary_subject': adapter.primary,
                        'source_locations': {'episode_jsonl_line': episodes.index(episode)+1,
                                             'checkpoint_jsonl_line': checkpoints.index(cp)+1},
                        'observed_action': pred['output']['action'], 'observed_evidence_count': 0,
                        'requested_information_scope_analyst_label': scope_for(cp),
                        'explicit_entity_relationships_for_requester': relationships,
                        'requester_prior_turn_count': len(own),
                        'requester_speaks_at_checkpoint_boundary': prefix[-1]['speaker']['principal_id'] == pid,
                        'replay': {'active_base_memory_count': len(memories),
                                   'readable_base_memory_count': readable,
                                   'searchable_base_memory_count': searchable,
                                   'requester_group_memberships': groups,
                                   'requester_session_actions': ['search', 'read', 'derive', 'link']},
                        'requester_source_turns': own,
                        'requester_source_notes_authorization': own_notes,
                        'adjacent_source_context': [prefix[i] for i in sorted(context_positions)],
                        'permission_context_turns': relevant_policy_turns,
                        'root_cause': 'No matching resource-policy allow: requester is neither patient/issuer nor a member of an authorized group. Source task participation is not compiled into a grant.',
                        'authority_assessment': 'Task involvement is source-supported; no explicit relationship row. Involvement alone is not a formal grant. A documented trusted adapter rule or verified assignment event is required.',
                    })
                if event['boundary'] % 100 == 0:
                    print(f"Replayed boundary {event['boundary']}/567; audited {len(entries)}/118", flush=True)
        finally:
            core.close()
    assert len(entries) == 118
    assert all(not e['explicit_entity_relationships_for_requester'] for e in entries)
    assert all(e['requester_prior_turn_count'] for e in entries)
    assert all(e['replay']['readable_base_memory_count'] == e['replay']['searchable_base_memory_count'] == 0 for e in entries)
    assert all(not e['replay']['requester_group_memberships'] for e in entries)
    files = {str(p.relative_to(ROOT)): sha(p) for folder in
             (ROOT/'agentic_memory/privacy', ROOT/'evaluation/gatemem_medical')
             for p in sorted(folder.iterdir())
             if p.is_file() and (p.suffix == '.py' or p.name == 'fixture_policy.json') and p.name != 'rejudge.py'}
    source_manifest = json.loads((SOURCE/'manifest.json').read_text())
    summary = {
        'query_count': len(entries), 'role_counts': dict(collections.Counter(e['asker']['role'] for e in entries)),
        'episode_count': len({e['episode_id'] for e in entries}),
        'unique_requester_episode_pairs': len({(e['episode_id'], e['asker']['principal_id']) for e in entries}),
        'explicit_relationship_count': sum(bool(e['explicit_entity_relationships_for_requester']) for e in entries),
        'requester_prior_participation_count': sum(bool(e['requester_prior_turn_count']) for e in entries),
        'requester_at_boundary_count': sum(e['requester_speaks_at_checkpoint_boundary'] for e in entries),
        'zero_authorized_base_memory_queries': sum(e['replay']['readable_base_memory_count'] == e['replay']['searchable_base_memory_count'] == 0 for e in entries),
        'scope_counts_analyst_labels': dict(collections.Counter(e['requested_information_scope_analyst_label'] for e in entries)),
        'source_run_code_matches_excluding_later_rejudge_script': digest(files) == source_manifest['code_digest'],
        'input_sha256': {str(p): sha(p) for p in [DATA/'episodes.jsonl', DATA/'checkpoints.jsonl', SOURCE/'official_predictions.jsonl', REJUDGE/'scores.jsonl']},
        'method': 'Saved predictions plus chronological policy-only replay with HashEmbedder and evolution disabled in a temporary DB. No model calls, changed grants, or benchmark rerun. Authorization checks apply to base memories. No claims about counterfactual answer accuracy.',
        'annotation_handling': 'Utility labels select the failed cohort for retrospective audit only. No grading annotations enter the replay adapter, permissions, or memory.',
    }
    (OUT/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    (OUT/'queries.jsonl').write_text(''.join(json.dumps(e, ensure_ascii=False)+'\n' for e in entries))
    with (OUT/'queries.csv').open('w', newline='') as f:
        fields = ['checkpoint_id', 'episode_id', 'as_of_turn_id', 'principal_id', 'role', 'query_text', 'requested_scope', 'prior_turn_ids', 'relationships', 'readable_memories', 'searchable_memories']
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for e in entries:
            writer.writerow(dict(checkpoint_id=e['checkpoint_id'], episode_id=e['episode_id'], as_of_turn_id=e['as_of_turn_id'], principal_id=e['asker']['principal_id'], role=e['asker']['role'], query_text=e['query_text'], requested_scope=e['requested_information_scope_analyst_label'], prior_turn_ids=' '.join(t['turn_id'] for t in e['requester_source_turns']), relationships='none', readable_memories=e['replay']['readable_base_memory_count'], searchable_memories=e['replay']['searchable_base_memory_count']))
    lines = ['# Staff permission audit: 118 empty-retrieval utility queries\n',
             'This is a retrospective diagnostic report, not a permission policy. All source evidence below is limited to the checkpoint prefix. Full source turns, adjacent context, policy context, and replay checks are in queries.jsonl.\n',
             'All 118 requesters participated before their query; none has an explicit entities.relationships entry. All 118 have zero readable and searchable base memories in chronological policy replay. In 54 cases the requester is the checkpoint-boundary speaker.\n',
             'The missing mechanism is task/subject-specific operational assignment, not identity recognition or model willingness. Source participation supports involvement but does not automatically establish authority. Define a trusted interpretation rule or obtain verified assignment events before issuing grants.\n',
             'Replay uses deterministic test embeddings and no evolution solely to inspect authorization state. It does not reproduce semantic ranking or predict utility after a fix. Existing policies never grant this cohort access; evolution cannot supply a missing principal/group allow. Existing run artifacts are untouched.\n',
             '| Role | Queries |\n|---|---:|\n']
    for role, n in summary['role_counts'].items():
        lines.append(f'| {role} | {n} |\n')
    last_episode = None
    for i, e in enumerate(entries, 1):
        if last_episode != e['episode_id']:
            lines.append(f"\n## {e['episode_id']}\n")
            last_episode = e['episode_id']
        lines.append(f"\n### {i}. {e['checkpoint_id'].rsplit('_ckpt_', 1)[-1]} — {e['asker']['principal_id']} at {e['as_of_turn_id']}\n\n")
        lines.append(f"Query: {e['query_text']}\n\nRequested information: {e['requested_information_scope_analyst_label']}.\n\n")
        lines.append(f"Identity is known; explicit relationship: none; group memberships: none. Replay: 0/{e['replay']['active_base_memory_count']} active base memories readable; 0 searchable. Observed result: no_memory with no evidence.\n\n")
        own = e['requester_source_turns']
        excerpt = list({t['turn_id']: t for t in own[:1]+own[-2:]}.values())
        lines.append('Source participation excerpts (evidence of task involvement, not automatic grants):\n\n')
        for t in excerpt:
            lines.append(f"- **{t['turn_id']}**: {t['text']}\n")
        lines.append(f"\nAll requester turn IDs: {', '.join(t['turn_id'] for t in own)}.\n")
    (OUT/'REPORT.md').write_text(''.join(lines))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
