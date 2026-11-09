#!/usr/bin/env python3
"""Have the system's own model write a domain's role access policy from fair inputs only.

Inputs, and nothing else:
  1. the job titles, read from entities.principals[].role in the domain's episodes.jsonl
     (no turns, no relationships, no checkpoints are read);
  2. the benchmark's published policy paragraph for the domain, bench.domains.get_query_policy_block(),
     which the benchmark puts into every tested system's prompt.

The model returns: the kinds of information facts are labelled with, which kinds each job title
may read on a project it works on, and which titles hold authority over project information.

Output directory (never overwritten):
  policy.json      the primary answer (temperature 0, seed 0) -- the one to freeze and use
  samples.jsonl    extra answers at a sampling temperature, for a stability report
  provenance.json  exact prompt, model, endpoint, input files and their sha256, raw replies

Usage:
  python scripts/infer_role_policy.py --domain office --gatemem-repo data/gatemem \
      --base-url http://127.0.0.1:18000/v1 --model qwen3.8:27b --api-key dummy --out <new dir>
"""
import argparse
import hashlib
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROMPT_VERSION = 1

SYSTEM = 'You design access policies for an organisation\'s AI memory assistant. Reply with JSON only.'

TASK = """An organisation uses an AI assistant with long-term memory. The assistant stores facts from staff conversations. Most stored facts are about one of the organisation's {subject_plural}. Each fact is labelled with one or more kinds of information. A staff member who has been shown doing work on a {subject} may read that {subject}'s facts, but only facts whose every kind is allowed for their job title. Anyone else needs an explicit approval from someone with authority over that {subject}'s information.

The access policy for this domain is:
<policy>
{policy}
</policy>

Job titles in this organisation:
{roles}

Tasks:
1. {kinds_task}
2. For each job title, list the kinds a person with that title needs to do their job on a {subject} they work on. Follow the policy above and give least privilege: leave a kind out unless the title needs it. Give a one-sentence reason for each title.
3. List the job titles that may approve access to a {subject}'s information, restrict it, or ask for it to be deleted from memory. Give a one-sentence reason."""

KINDS_TASK_FREE = ('Define the kinds of information used to label facts about {subject_plural}: 6 to 12 kinds that together '
    'cover what staff discuss about {subject_plural}, keeping apart what the policy treats differently. For each, give a '
    'short snake_case name and one sentence saying what it covers.')
KINDS_TASK_FIXED = 'Use exactly these kinds of information, with these names:\n{kinds}'

DOMAINS = {'office': {'subject': 'project', 'subject_plural': 'projects'},
           'medical': {'subject': 'patient', 'subject_plural': 'patients'}}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def job_titles(episodes, exclude):
    titles = []
    with open(episodes) as fh:
        for line in fh:
            if line.strip():
                for p in json.loads(line)['entities']['principals']:
                    if p['role'] not in exclude and p['role'] not in titles:
                        titles.append(p['role'])
    return sorted(titles)


def schema(titles, fixed_kinds):
    kind_name = {'type': 'string', 'enum': sorted(fixed_kinds)} if fixed_kinds else {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{1,30}$'}
    props = {
        'roles': {'type': 'array', 'minItems': len(titles), 'maxItems': len(titles), 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['role', 'kinds', 'reason'],
            'properties': {'role': {'type': 'string', 'enum': titles},
                           'kinds': {'type': 'array', 'items': kind_name},
                           'reason': {'type': 'string'}}}},
        'authorities': {'type': 'object', 'additionalProperties': False, 'required': ['roles', 'reason'],
            'properties': {'roles': {'type': 'array', 'items': {'type': 'string', 'enum': titles}},
                           'reason': {'type': 'string'}}},
    }
    required = ['roles', 'authorities']
    if not fixed_kinds:
        props = {'information_kinds': {'type': 'array', 'minItems': 6, 'maxItems': 12, 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['name', 'description'],
            'properties': {'name': kind_name, 'description': {'type': 'string'}}}}, **props}
        required = ['information_kinds', *required]
    return {'type': 'object', 'additionalProperties': False, 'required': required, 'properties': props}


def check(answer, titles, fixed_kinds):
    """Return a normalised policy, or raise ValueError saying what is wrong."""
    kinds = dict(fixed_kinds) if fixed_kinds else {k['name']: k['description'] for k in answer['information_kinds']}
    if not fixed_kinds and len(kinds) != len(answer['information_kinds']):
        raise ValueError('duplicate kind names')
    rows = {}
    for r in answer['roles']:
        if r['role'] in rows:
            raise ValueError(f"title listed twice: {r['role']}")
        unknown = set(r['kinds']) - set(kinds)
        if unknown:
            raise ValueError(f"{r['role']} uses undefined kinds {sorted(unknown)}")
        rows[r['role']] = {'kinds': sorted(set(r['kinds'])), 'reason': r['reason']}
    if set(rows) != set(titles):
        raise ValueError(f'titles missing: {sorted(set(titles) - set(rows))}')
    return {'information_kinds': kinds,
            'role_kinds': {t: rows[t]['kinds'] for t in titles},
            'role_reasons': {t: rows[t]['reason'] for t in titles},
            'authorities': sorted(set(answer['authorities']['roles'])),
            'authorities_reason': answer['authorities']['reason']}


def call(args, messages, response_schema, temperature, seed):
    body = {'model': args.model, 'messages': messages, 'temperature': temperature, 'max_tokens': args.max_tokens,
            'response_format': {'type': 'json_schema', 'json_schema': {'name': 'role_policy', 'schema': response_schema, 'strict': True}}}
    if seed is not None:
        body['seed'] = seed
    req = urllib.request.Request(args.base_url.rstrip('/') + '/chat/completions', data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', **({'Authorization': f'Bearer {args.api_key}'} if args.api_key else {})})
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=args.timeout) as resp:
        reply = json.loads(resp.read())
    msg = reply['choices'][0]['message']
    return {'content': msg.get('content'), 'reasoning': msg.get('reasoning_content') or msg.get('reasoning'),
            'finish_reason': reply['choices'][0].get('finish_reason'), 'usage': reply.get('usage'),
            'seconds': round(time.monotonic() - started, 1), 'temperature': temperature, 'seed': seed}


def ask(args, messages, response_schema, titles, fixed_kinds, temperature, seed, log):
    for attempt in range(1, args.attempts + 1):
        raw = call(args, messages, response_schema, temperature, seed)
        raw['attempt'] = attempt
        try:
            raw['policy'] = check(json.loads(raw['content']), titles, fixed_kinds)
            log.append(raw)
            return raw['policy']
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            raw['error'] = f'{type(e).__name__}: {e}'
            log.append(raw)
            print(f'  attempt {attempt} rejected: {raw["error"]}', file=sys.stderr)
    raise SystemExit(f'no valid policy after {args.attempts} attempts')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--domain', required=True, choices=sorted(DOMAINS))
    ap.add_argument('--gatemem-repo', required=True, help='GateMem checkout: bench/domains.py and bench/data/<domain>')
    ap.add_argument('--base-url', required=True)
    ap.add_argument('--model', required=True)
    ap.add_argument('--api-key', default='')
    ap.add_argument('--out', required=True, help='new directory; refused if it exists')
    ap.add_argument('--exclude-roles', default='agent', help='comma list of principal roles that are not staff readers')
    ap.add_argument('--fixed-kinds', help='JSON file {name: description}: diagnostic mode, the model only fills the table')
    ap.add_argument('--samples', type=int, default=5, help='extra answers for the stability report')
    ap.add_argument('--sample-temperature', type=float, default=0.6)
    ap.add_argument('--attempts', type=int, default=3)
    ap.add_argument('--max-tokens', type=int, default=24000)
    ap.add_argument('--timeout', type=float, default=3600)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    repo = Path(args.gatemem_repo).resolve()
    episodes = repo / 'bench/data' / args.domain / 'episodes.jsonl'
    domains_py = repo / 'bench/domains.py'
    sys.path.insert(0, str(repo))
    from bench.domains import get_query_policy_block
    policy_text = get_query_policy_block(args.domain)

    titles = job_titles(episodes, set(filter(None, args.exclude_roles.split(','))))
    fixed_kinds = json.loads(Path(args.fixed_kinds).read_text()) if args.fixed_kinds else None
    d = DOMAINS[args.domain]
    kinds_task = (KINDS_TASK_FIXED.format(kinds='\n'.join(f'- {k}: {v}' for k, v in fixed_kinds.items())) if fixed_kinds
                  else KINDS_TASK_FREE.format(**d))
    prompt = TASK.format(policy=policy_text, roles='\n'.join(f'- {t}' for t in titles), kinds_task=kinds_task, **d)
    messages = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': prompt}]
    response_schema = schema(titles, fixed_kinds)

    provenance = {
        'prompt_version': PROMPT_VERSION, 'domain': args.domain, 'mode': 'fixed-kinds diagnostic' if fixed_kinds else 'inferred',
        'started_utc': datetime.now(timezone.utc).isoformat(),
        'model': args.model, 'base_url': args.base_url, 'max_tokens': args.max_tokens,
        'inputs': {'job_titles_from': str(episodes), 'job_titles_field': 'entities.principals[].role',
                   'episodes_sha256': sha256(episodes), 'excluded_roles': args.exclude_roles,
                   'policy_from': f'{domains_py} get_query_policy_block({args.domain!r})', 'domains_py_sha256': sha256(domains_py),
                   'fixed_kinds_file': args.fixed_kinds, 'not_read': ['turns', 'relationships', 'checkpoints.jsonl']},
        'job_titles': titles, 'policy_text': policy_text, 'messages': messages, 'response_schema': response_schema,
        'script_sha256': sha256(__file__), 'replies': []}

    def save():
        (out / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')

    print(f'{len(titles)} job titles: {", ".join(titles)}')
    print('primary answer (temperature 0, seed 0) ...', flush=True)
    primary = ask(args, messages, response_schema, titles, fixed_kinds, 0.0, 0, provenance['replies'])
    (out / 'policy.json').write_text(json.dumps({'domain': args.domain, 'model': args.model, 'prompt_version': PROMPT_VERSION,
        'script_sha256': provenance['script_sha256'], **primary}, indent=2) + '\n')
    save()
    with open(out / 'samples.jsonl', 'w') as fh:
        for i in range(args.samples):
            print(f'sample {i + 1}/{args.samples} (temperature {args.sample_temperature}) ...', flush=True)
            fh.write(json.dumps(ask(args, messages, response_schema, titles, fixed_kinds, args.sample_temperature, None,
                                    provenance['replies'])) + '\n')
            fh.flush()
            save()
    provenance['finished_utc'] = datetime.now(timezone.utc).isoformat()
    save()
    print(f'wrote {out}')


if __name__ == '__main__':
    main()
