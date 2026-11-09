#!/usr/bin/env python3
"""Build a domain's role access policy with the system's own model, from fair inputs only.

Inputs, and nothing else:
  - the job titles, read from entities.principals[].role in the domain's episodes.jsonl
    (no turns, no relationships, no checkpoints are read), minus the roles that are
    subjects or only ever mentioned (hosts.policy_excluded_roles, or --exclude-roles);
  - the benchmark's published policy paragraph, bench.domains.get_query_policy_block(),
    which the benchmark puts into every tested system's prompt.

Step 1, one call (temperature 0, seed 0): a short description of each job's work, then
the kinds of information that work creates and uses, named by what the information is
about. These are frozen.
Step 2, --votes calls (sampling temperature, seeds 1..N), run in parallel: given the
frozen descriptions and kinds, which kinds each job needs. A cell is open when a strict
majority of votes opens it. For subjects that never speak (--authorities), the same
votes also pick who may approve, restrict or delete their information.

Output directory (never overwritten):
  policy.json      the policy, format gatemem-role-policy-v2, read by runner --role-policy
  provenance.json  exact prompts, schemas, model, endpoint, input files and sha256, every reply

Usage:
  python scripts/build_role_policy.py --domain medical \
      --base-url http://127.0.0.1:18000/v1 --model qwen3.8:27b --api-key dummy --out <new dir>

--gatemem-repo defaults to the vendored benchmark in data/gatemem. For medical and
office the prompts are byte-identical to the ones that built the archived policies
(tests: evaluation/gatemem_medical/tests/test_policy_builder.py). Education and
household only change the words for the organisation, its staff and its subjects.
"""
import argparse
import hashlib
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

FORMAT = 'gatemem-role-policy-v2'
PROMPT_VERSION = 2
SYSTEM = "You design access policies for {an_org_lower}'s AI memory assistant. Reply with JSON only."
# Wording per domain. 'authorities': subjects that never speak (projects, programs)
# need roles that control them; they are voted on with the role table.
ORGANISATION = {'an_org': 'An organisation', 'an_org_lower': 'an organisation', 'org': 'organisation',
                'staff': 'staff', 'a_staff_member': 'A staff member'}
DOMAINS = {'office': {'subject': 'project', 'subject_plural': 'projects', 'authorities': True},
           'medical': {'subject': 'patient', 'subject_plural': 'patients', 'authorities': False},
           'education': {'subject': 'student', 'subject_plural': 'students', 'authorities': True,
                         'authority_subject': 'campus program or research project'},
           'household': {'subject': 'resident', 'subject_plural': 'residents', 'authorities': False,
                         'an_org': 'A household', 'an_org_lower': 'a household', 'org': 'household',
                         'staff': 'helpers and service providers', 'a_staff_member': 'A helper or service provider'}}


def wording(domain):
    """Every placeholder the prompts use, with the organisation defaults filled in."""
    d = {**ORGANISATION, **DOMAINS[domain]}
    d.setdefault('authority_subject', d['subject'])
    return {k: v for k, v in d.items() if k != 'authorities'}

SETTING = """{an_org} uses an AI assistant with long-term memory. The assistant stores facts from {staff} conversations. Most stored facts are about one of the {org}'s {subject_plural}.

The access policy for this domain is:
<policy>
{policy}
</policy>"""

STEP1 = SETTING + """

Job titles of the {staff} in this {org}:
{roles}

Tasks:
1. For each job title, describe in two or three sentences the day-to-day work a person with that title does for a {subject}: what they create, check, decide and act on.
2. Define the kinds of information that {staff} create and use about {subject_plural}: 6 to 12 kinds. Name each kind by what the information is about, not by who may see it or how sensitive it is. Together the kinds should cover the work you described, and kinds that the policy treats differently should be kept apart. For each kind, give a short snake_case name and one sentence saying what it covers."""

STEP2 = SETTING + """

Each stored fact is labelled with one or more of the kinds of information below. {a_staff_member} who has been shown doing work for a {subject} may read that {subject}'s facts, but only facts whose every kind is allowed for their job title. Anyone else needs an explicit approval.

Job titles and their work:
{described_roles}

Kinds of information:
{kinds}

Task: for each job title, list the kinds a person with that title needs in order to do the work described above for a {subject} they work for. Follow the policy above and give least privilege: include a kind when the described work needs it, and leave it out otherwise. Give a one-sentence reason for each title.{authority_task}"""

AUTHORITY_TASK = """
Also list the job titles that may approve access to a {authority_subject}'s information, restrict it, or ask for it to be deleted from memory, with a one-sentence reason."""


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def job_titles(episodes, exclude):
    titles = set()
    with open(episodes) as fh:
        for line in fh:
            if line.strip():
                titles |= {p['role'] for p in json.loads(line)['entities']['principals']} - exclude
    return sorted(titles)


def step1_schema(titles):
    return {'type': 'object', 'additionalProperties': False, 'required': ['roles', 'information_kinds'], 'properties': {
        'roles': {'type': 'array', 'minItems': len(titles), 'maxItems': len(titles), 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['role', 'description'],
            'properties': {'role': {'type': 'string', 'enum': titles}, 'description': {'type': 'string'}}}},
        'information_kinds': {'type': 'array', 'minItems': 6, 'maxItems': 12, 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['name', 'description'],
            'properties': {'name': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{1,30}$'},
                           'description': {'type': 'string'}}}}}}


def step2_schema(titles, kinds, authorities):
    props = {'roles': {'type': 'array', 'minItems': len(titles), 'maxItems': len(titles), 'items': {
        'type': 'object', 'additionalProperties': False, 'required': ['role', 'kinds', 'reason'],
        'properties': {'role': {'type': 'string', 'enum': titles},
                       'kinds': {'type': 'array', 'items': {'type': 'string', 'enum': sorted(kinds)}},
                       'reason': {'type': 'string'}}}}}
    if authorities:
        props['authorities'] = {'type': 'object', 'additionalProperties': False, 'required': ['roles', 'reason'],
            'properties': {'roles': {'type': 'array', 'items': {'type': 'string', 'enum': titles}},
                           'reason': {'type': 'string'}}}
    return {'type': 'object', 'additionalProperties': False, 'required': sorted(props), 'properties': props}


def check_step1(answer, titles):
    roles = {r['role']: r['description'].strip() for r in answer['roles']}
    kinds = {k['name']: k['description'].strip() for k in answer['information_kinds']}
    if set(roles) != set(titles) or len(roles) != len(answer['roles']):
        raise ValueError('every job title exactly once')
    if len(kinds) != len(answer['information_kinds']):
        raise ValueError('duplicate kind names')
    if not all(roles.values()) or not all(kinds.values()):
        raise ValueError('empty description')
    return {'role_descriptions': {t: roles[t] for t in titles}, 'information_kinds': kinds}


def check_step2(answer, titles, kinds, authorities):
    rows = {}
    for r in answer['roles']:
        if r['role'] in rows:
            raise ValueError(f"title listed twice: {r['role']}")
        if set(r['kinds']) - set(kinds):
            raise ValueError(f"{r['role']} uses undefined kinds")
        rows[r['role']] = {'kinds': sorted(set(r['kinds'])), 'reason': r['reason']}
    if set(rows) != set(titles):
        raise ValueError('every job title exactly once')
    vote = {'role_kinds': {t: rows[t]['kinds'] for t in titles}, 'role_reasons': {t: rows[t]['reason'] for t in titles}}
    if authorities:
        vote['authorities'] = sorted(set(answer['authorities']['roles']))
        vote['authorities_reason'] = answer['authorities']['reason']
    return vote


def tally(votes, titles, kinds, authorities):
    """A cell (and an authority) is open when a strict majority of the votes opens it."""
    need = len(votes) // 2 + 1
    counts = {t: {k: sum(k in v['role_kinds'][t] for v in votes) for k in kinds} for t in titles}
    result = {'votes': len(votes), 'majority': need, 'role_kinds': {t: [k for k in kinds if counts[t][k] >= need] for t in titles},
              'role_kind_votes': counts}
    if authorities:
        auth = {t: sum(t in v['authorities'] for v in votes) for t in titles}
        result.update(authorities=[t for t in titles if auth[t] >= need], authority_votes=auth)
    return result


def call(args, messages, schema, temperature, seed):
    body = {'model': args.model, 'messages': messages, 'temperature': temperature, 'seed': seed,
            'max_tokens': args.max_tokens,
            'response_format': {'type': 'json_schema', 'json_schema': {'name': 'role_policy', 'schema': schema, 'strict': True}}}
    req = urllib.request.Request(args.base_url.rstrip('/') + '/chat/completions', data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', **({'Authorization': f'Bearer {args.api_key}'} if args.api_key else {})})
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=args.timeout) as resp:
        reply = json.loads(resp.read())
    msg = reply['choices'][0]['message']
    return {'content': msg.get('content'), 'reasoning': msg.get('reasoning_content') or msg.get('reasoning'),
            'finish_reason': reply['choices'][0].get('finish_reason'), 'usage': reply.get('usage'),
            'seconds': round(time.monotonic() - started, 1), 'temperature': temperature, 'seed': seed}


def ask(args, label, messages, schema, temperature, seed, checker, log):
    """Retry with the same seed until the reply passes its checker (format slips only)."""
    for attempt in range(1, args.attempts + 1):
        raw = {'step': label, 'attempt': attempt, **call(args, messages, schema, temperature, seed)}
        try:
            raw['parsed'] = checker(json.loads(raw['content']))
            log.append(raw)
            print(f'  {label}: ok in {raw["seconds"]}s', flush=True)
            return raw['parsed']
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            raw['error'] = f'{type(e).__name__}: {e}'
            log.append(raw)
            print(f'  {label}: attempt {attempt} rejected: {raw["error"]}', file=sys.stderr, flush=True)
    raise SystemExit(f'{label}: no valid reply after {args.attempts} attempts')


REPO = Path(__file__).resolve().parents[1]


def default_excluded_roles(domain):
    """The host's subject and mentioned-only roles, plus the assistant (evaluation/gatemem_medical/hosts.py)."""
    sys.path.insert(0, str(REPO))
    from evaluation.gatemem_medical.hosts import policy_excluded_roles
    return ','.join(policy_excluded_roles(domain))


def step1_messages(domain, policy_text, titles):
    w = wording(domain)
    return [{'role': 'system', 'content': SYSTEM.format(**w)}, {'role': 'user', 'content': STEP1.format(
        policy=policy_text, roles='\n'.join(f'- {t}' for t in titles), **w)}]


def step2_messages(domain, policy_text, titles, frozen, authorities):
    w = wording(domain)
    return [{'role': 'system', 'content': SYSTEM.format(**w)}, {'role': 'user', 'content': STEP2.format(
        policy=policy_text, **w,
        described_roles='\n'.join(f'- {t}: {frozen["role_descriptions"][t]}' for t in titles),
        kinds='\n'.join(f'- {k}: {v}' for k, v in frozen['information_kinds'].items()),
        authority_task=AUTHORITY_TASK.format(**w) if authorities else '')}]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--domain', required=True, choices=sorted(DOMAINS))
    ap.add_argument('--gatemem-repo', default=str(REPO / 'data/gatemem'))
    ap.add_argument('--exclude-roles',
                    help='comma list of principal roles the table does not cover: the assistant, the subjects '
                         'themselves and people only ever mentioned (default: from hosts.py)')
    ap.add_argument('--authorities', action=argparse.BooleanOptionalAction, default=None,
                    help='subjects that never speak (projects, programs): also vote on who controls them '
                         '(default: on for office and education)')
    ap.add_argument('--base-url', required=True)
    ap.add_argument('--model', required=True)
    ap.add_argument('--api-key', default='')
    ap.add_argument('--out', required=True, help='new directory; refused if it exists')
    ap.add_argument('--votes', type=int, default=5)
    ap.add_argument('--vote-temperature', type=float, default=0.6)
    ap.add_argument('--attempts', type=int, default=3)
    ap.add_argument('--max-tokens', type=int, default=32000)
    ap.add_argument('--timeout', type=float, default=3600)
    args = ap.parse_args()
    if args.votes < 1 or args.votes % 2 == 0:
        ap.error('--votes must be odd, so a strict majority always exists')
    if args.exclude_roles is None:
        args.exclude_roles = default_excluded_roles(args.domain)
    if args.authorities is None:
        args.authorities = DOMAINS[args.domain]['authorities']

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    repo = Path(args.gatemem_repo).resolve()
    episodes, domains_py = repo / 'bench/data' / args.domain / 'episodes.jsonl', repo / 'bench/domains.py'
    sys.path.insert(0, str(repo))
    from bench.domains import get_query_policy_block
    policy_text = get_query_policy_block(args.domain)
    titles = job_titles(episodes, set(filter(None, args.exclude_roles.split(','))))
    log = []
    provenance = {'format': FORMAT, 'prompt_version': PROMPT_VERSION, 'domain': args.domain,
        'started_utc': datetime.now(timezone.utc).isoformat(), 'model': args.model, 'base_url': args.base_url,
        'max_tokens': args.max_tokens, 'votes': args.votes, 'vote_temperature': args.vote_temperature,
        'authorities': args.authorities, 'wording': wording(args.domain),
        'inputs': {'job_titles_from': str(episodes), 'job_titles_field': 'entities.principals[].role',
                   'episodes_sha256': sha256(episodes), 'excluded_roles': args.exclude_roles,
                   'policy_from': f'{domains_py} get_query_policy_block({args.domain!r})',
                   'domains_py_sha256': sha256(domains_py), 'not_read': ['turns', 'relationships', 'checkpoints.jsonl']},
        'job_titles': titles, 'policy_text': policy_text, 'script_sha256': sha256(__file__), 'replies': log}
    save = lambda: (out / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')

    print(f'{len(titles)} job titles: {", ".join(titles)}', flush=True)
    m1 = step1_messages(args.domain, policy_text, titles)
    s1 = step1_schema(titles)
    provenance['step1'] = {'messages': m1, 'schema': s1}
    print('step 1: job descriptions and kinds (temperature 0, seed 0)', flush=True)
    frozen = ask(args, 'step1', m1, s1, 0.0, 0, lambda a: check_step1(a, titles), log)
    save()

    kinds = frozen['information_kinds']
    m2 = step2_messages(args.domain, policy_text, titles, frozen, args.authorities)
    s2 = step2_schema(titles, kinds, args.authorities)
    provenance['step2'] = {'messages': m2, 'schema': s2}
    print(f'step 2: {args.votes} votes in parallel (temperature {args.vote_temperature}, seeds 1..{args.votes})', flush=True)
    checker = lambda a: check_step2(a, titles, kinds, args.authorities)
    with ThreadPoolExecutor(max_workers=args.votes) as pool:
        votes = list(pool.map(lambda seed: ask(args, f'vote{seed}', m2, s2, args.vote_temperature, seed, checker, log),
                              range(1, args.votes + 1)))
    save()

    policy = {'format': FORMAT, 'domain': args.domain, 'model': args.model, 'prompt_version': PROMPT_VERSION,
        'script_sha256': provenance['script_sha256'], 'information_kinds': kinds,
        'role_descriptions': frozen['role_descriptions'], **tally(votes, titles, kinds, args.authorities)}
    (out / 'policy.json').write_text(json.dumps(policy, indent=2) + '\n')
    provenance['finished_utc'] = datetime.now(timezone.utc).isoformat()
    save()
    print(f'wrote {out}', flush=True)


if __name__ == '__main__':
    main()
