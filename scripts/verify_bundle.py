#!/usr/bin/env python3
"""Check this repository against the medical run it archives.

    python scripts/verify_bundle.py            # exit 0 when everything checks out
    python scripts/verify_bundle.py --diff     # also print the diff of every changed code file

1. Code: every file the medical run froze (runs/medical/frozen-source.sha256, 120 files)
   is either byte-identical here, or listed in CHANGED_SINCE_MEDICAL_RUN with the reason.
   An unlisted change, or a listed file that is in fact unchanged, is an error. The
   frozen bytes of changed files are not in this repository; docs/changes-since-medical-run.patch
   holds the diff, and --diff recomputes it when the original frozen-source is on this machine.
2. Archives: every file in runs/medical, references/*, policies and data matches the
   sha256 in its EXPORT_MANIFEST.json.
3. The frozen medical policy matches role_policy.json.sha256 and the generated policy
   archived in policies/.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'pipeline'))
import export_run  # noqa: E402

# Moved without changes (frozen path -> path here).
MOVED = {'FIXES.md': 'docs/design/FIXES.md', 'OFFICE_HOST.md': 'docs/design/OFFICE_HOST.md'}
# Every code file changed after the medical run, and why. docs/CHANGES_SINCE_MEDICAL_RUN.md explains each.
CHANGED_SINCE_MEDICAL_RUN = {
    'evaluation/gatemem_medical/hosts.py': 'education and household hosts (no hand-written tables, requires_policy), '
                                           'MENTIONED_ROLES and policy_excluded_roles for the policy builder',
    'evaluation/gatemem_medical/runner.py': 'pinned education/household dataset sizes, vendored data as the default '
                                            '--gatemem-repo, refuse a requires_policy host without --role-policy',
    'evaluation/gatemem_medical/rejudge.py': 'vendored data as the default --gatemem-repo',
    'evaluation/gatemem_medical/evolution_ablation.py': 'vendored data as the default --gatemem-repo',
    'evaluation/gatemem_medical/access_audit.py': 'vendored medical data as the default --data',
    'scripts/build_role_policy.py': 'education/household wording, defaults for --gatemem-repo, --exclude-roles and '
                                    '--authorities; medical and office prompts byte-identical (tests)',
    'scripts/infer_role_policy.py': 'usage text points at the vendored data',
    'evaluation/gatemem_medical/tests/test_scoring.py': 'test data path: vendored data',
    'evaluation/gatemem_medical/tests/test_office_host.py': 'test data path: vendored data',
    'evaluation/gatemem_medical/tests/test_input_boundary.py': 'test data path: vendored data',
    'evaluation/gatemem_medical/tests/test_role_policy.py': 'test data path: vendored data',
    'security_tests/test_privacy_fallback_episodes_v19.py': 'test data path: vendored data',
}
ORIGINAL_FROZEN = Path('/home/thaddus/v19-evaluations/medical_domain_new_system/frozen-source')


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_code(show_diff=False):
    problems, same, changed = [], 0, []
    for line in (REPO / 'runs/medical/frozen-source.sha256').read_text().splitlines():
        digest, name = line.split(None, 1)
        rel = name.removeprefix('frozen-source/')
        here = REPO / MOVED.get(rel, rel)
        if not here.is_file():
            problems.append(f'missing code file {rel}')
        elif sha256(here) == digest:
            same += 1
            if rel in CHANGED_SINCE_MEDICAL_RUN:
                problems.append(f'{rel} is listed as changed but is identical to the medical run')
        else:
            changed.append(rel)
            if rel not in CHANGED_SINCE_MEDICAL_RUN:
                problems.append(f'{rel} differs from the medical run and is not listed in CHANGED_SINCE_MEDICAL_RUN')
            if show_diff and (ORIGINAL_FROZEN / rel).is_file():
                subprocess.run(['diff', '-u', '--label', f'medical-run/{rel}', '--label', rel,
                                str(ORIGINAL_FROZEN / rel), str(here)])
    return problems, same, changed


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    problems, same, changed = check_code('--diff' in argv)
    print(f'code: {same} files identical to the medical run, {len(changed)} changed (all listed with a reason)'
          if not problems else 'code: problems found')
    folders = ['runs/medical', 'references/medical_domain_original', 'references/office_domain', 'policies', 'data']
    for folder in folders:
        found = export_run.verify(REPO / folder)
        problems += found
        n = len(json.loads((REPO / folder / 'EXPORT_MANIFEST.json').read_text())['files'])
        print(f'{folder}: {n} files' + (' OK' if not found else f', {len(found)} problems'))
    frozen = (REPO / 'runs/medical/role_policy.json.sha256').read_text().split()[0]
    for path in ('runs/medical/role_policy.json', 'policies/medical-v2-20261108T165625Z/policy.json'):
        if sha256(REPO / path) != frozen:
            problems.append(f'{path} does not match the frozen medical policy {frozen}')
    print(f'medical policy: {frozen}' + (' OK' if not problems else ''))
    for p in problems:
        print('PROBLEM:', p)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
