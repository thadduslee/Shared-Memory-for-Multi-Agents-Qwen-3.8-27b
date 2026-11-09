"""Host settings for each GateMem domain: who a record belongs to, and who may read what.

The core in agentic_memory.privacy holds no domain knowledge. A host supplies it here:

- subject_roles: directory roles a record can belong to (patients, projects).
- care_scopes: roles that do work for a subject, with a short description of that work.
- information_types: the types the model tags each fact with.
- role_information_types: which types each role may read once it is shown working
  for a subject. The model never sees this table. A fact is readable only when ALL
  of its types are in the reader's row.
- temporary_care_hours: how long a time-limited worker keeps access after their last
  involvement.
- subject_authority_roles: for subjects that never speak, such as a project, the roles
  that may grant, restrict or delete that subject's information. A person subject
  already controls their own records, so the medical host leaves this empty.

A role alone grants nothing. Access to a subject's facts still needs a cited source
turn showing that person doing work for that subject, or an explicit grant from an
authority. Revocation, restriction, expiry and erasure apply on top.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Host:
    domain: str
    subject_roles: tuple
    care_scopes: dict
    information_types: dict
    role_information_types: dict
    temporary_care_hours: float
    # Kinds of non-person entities the directory lists, read from the episode's entity IDs only.
    entity_kinds: tuple = ()
    subject_authority_roles: tuple = ()
    task_convention: str = ''
    identity_convention: str = ''
    extra_context: dict = field(default_factory=dict)
    # Learned access (agentic_memory.privacy.care_v19.learned_access): 0 and False keep it off.
    learned_access_sources: int = 0
    reply_access: bool = False
    # Where the role tables came from: 'hand-written' or a generated policy file's sha256.
    policy_source: str = 'hand-written'
    # True for hosts with no hand-written role tables at all: the runner refuses them
    # unless --role-policy supplies a generated policy (scripts/build_role_policy.py).
    requires_policy: bool = False

    @property
    def worker_roles(self):
        return (*self.care_scopes, 'agent')

    def validate(self):
        if not self.care_scopes and not self.requires_policy:
            raise ValueError('A host without role tables must set requires_policy')
        unknown = {t for types in self.role_information_types.values() for t in types} - set(self.information_types)
        if unknown:
            raise ValueError(f'Role table names unknown information types: {sorted(unknown)}')
        if set(self.role_information_types) - set(self.care_scopes):
            raise ValueError('Every role in the table needs a care scope')
        if set(self.subject_roles) & set(self.care_scopes):
            raise ValueError('A subject role cannot also be a worker role')
        if self.subject_authority_roles and not set(self.subject_authority_roles) <= set(self.care_scopes):
            raise ValueError('Subject authority roles must be directory worker roles')
        return self


# --------------------------------------------------------------------------- medical
# Unchanged from the medical runs: these values were inline in inference_adapter.py.

MEDICAL_CARE_SCOPES = {
    'clinician': 'Clinical information needed to treat this patient: diagnoses, results, medications, procedures, plans, precautions, appointments, contact arrangements, access settings and care coordination.',
    'nurse': 'Clinical information needed for nursing care of this patient: medications, results, monitoring, precautions, instructions, callbacks, appointments, access settings and care coordination.',
    'pharmacist': 'Medication information for this patient: active and stopped medications, doses, allergies, interactions and medication instructions, plus appointments. No results, diagnoses or clinical notes.',
    'scheduler': 'Scheduling logistics for this patient: appointments, times, locations, arrival, preparation timing, changes and cancellations, plus how to contact them. No results, diagnoses or clinical notes.',
    'reception': 'Front-desk logistics for this patient: check-in, appointments, locations, contact preferences and who may receive scheduling information. No results, diagnoses, medications or clinical notes.',
    'social_worker': 'Support coordination for this patient: transportation, housing, safe contact arrangements, resources, access settings, appointments and care coordination. No results or medication details.',
    'lab_tech': 'Laboratory work for this patient: ordered tests, collection, timing and result values, plus how to contact them. No unrelated clinical notes.',
    'billing': 'Billing for this patient: charges, coverage, payment and authorizations, plus appointments. No diagnoses, results, medications or clinical notes.'}
MEDICAL_INFORMATION_TYPES = {
    'medication': 'Medications, doses, allergies, interactions and medication instructions, including stopped ones.',
    'results': 'Lab and imaging results, values and their ordered tests or collection.',
    'clinical': 'Symptoms, diagnoses, procedures, treatment plans, monitoring and return precautions.',
    'schedule': 'Appointments, times, locations, arrival, preparation timing, changes and cancellations.',
    'contact': 'Phone numbers, callback and voicemail instructions, safe contact arrangements and contact preferences.',
    'access': 'Who may or may not receive which information, and consent or family access settings.',
    'support': 'Transportation, housing, resources and care coordination.',
    'billing': 'Charges, insurance, coverage, payment and authorizations.'}
MEDICAL_ROLE_INFORMATION_TYPES = {
    'clinician': ('medication', 'results', 'clinical', 'schedule', 'contact', 'access', 'support'),
    'nurse': ('medication', 'results', 'clinical', 'schedule', 'contact', 'access', 'support'),
    'pharmacist': ('medication', 'schedule'),
    'scheduler': ('schedule', 'contact'),
    'reception': ('schedule', 'contact', 'access'),
    'social_worker': ('support', 'contact', 'access', 'schedule'),
    'lab_tech': ('results', 'contact'),
    'billing': ('billing', 'schedule')}

MEDICAL = Host(
    domain='medical',
    # Whose records these are. Family and friends named in a note are mentioned, not owners.
    subject_roles=('patient',),
    care_scopes=MEDICAL_CARE_SCOPES,
    information_types=MEDICAL_INFORMATION_TYPES,
    role_information_types=MEDICAL_ROLE_INFORMATION_TYPES,
    # Covering staff keep access for a day after they last did work on the chart.
    temporary_care_hours=24,
).validate()


# --------------------------------------------------------------------------- office
# A record usually belongs to a project. People named in it (a contractor, a sponsor)
# are mentioned, not owners. Every person in an office directory is staff.

OFFICE_INFORMATION_TYPES = {
    'schedule': 'Target and launch dates, milestones, timelines, meetings, reviews and calendar holds, and changes to them.',
    'status': 'Progress, open work and blockers, readiness, and approved broad descriptions meant for use outside the core team, such as a generic customer phrase.',
    'customer': 'Which external customer or account a project is for, customer mappings, and customer contacts or account details.',
    'financial': 'Budgets, costs, pricing, discounts and commercial caps, and financial approvals.',
    'contract': 'Contract terms, redlines, renewal structure and legal review positions.',
    'vendor': 'Vendor and supplier options, evaluations, selection, procurement and onboarding.',
    'incident': 'Incidents and outages, diagnoses and root causes, coordination or bridge labels, logs, reproduction notes and remediation.',
    'credential': 'Tokens, keys, passwords, secrets and privileged configuration values.',
    'access': 'Who has or lacks which access to a project or system, its scope and duration, and grants and revocations.',
    'personnel': 'HR and people matters about individual staff: staffing, performance, leave, compensation and hiring.'}

# What each role needs to do its work on a project it is shown working on.
OFFICE_ROLE_INFORMATION_TYPES = {
    'product_manager':     ('schedule', 'status', 'customer', 'financial', 'contract', 'vendor', 'incident', 'access'),
    'manager':             ('schedule', 'status', 'customer', 'financial', 'contract', 'vendor', 'access', 'personnel'),
    'executive':           ('schedule', 'status', 'customer', 'financial', 'contract', 'vendor', 'incident', 'access'),
    'engineer':            ('schedule', 'status', 'vendor', 'incident', 'credential', 'access'),
    'sre':                 ('schedule', 'status', 'incident', 'credential', 'access'),
    'security':            ('schedule', 'status', 'vendor', 'incident', 'credential', 'access'),
    'finance':             ('schedule', 'status', 'financial', 'contract', 'vendor'),
    'legal':               ('schedule', 'status', 'customer', 'financial', 'contract', 'vendor'),
    'sales':               ('schedule', 'status', 'customer', 'financial', 'contract'),
    'procurement':         ('schedule', 'status', 'financial', 'contract', 'vendor'),
    'operations':          ('schedule', 'status', 'vendor', 'incident'),
    'executive_assistant': ('schedule', 'status'),
    'employee':            ('schedule', 'status'),
    'contractor':          ('status', 'incident'),
    'hr':                  ('personnel',)}

OFFICE_CARE_SCOPES = {
    'product_manager': 'Running this project: dates, status, customer, budget and commercial terms, contract, vendors, incidents and who has access. No credentials.',
    'manager': 'Leading this project and its staff: dates, status, customer, budget and commercial terms, contract, vendors, access and the people on it. No credentials or incident detail.',
    'executive': 'Sponsoring this project: dates, status, customer, budget and commercial terms, contract, vendors, incidents and access. No credentials.',
    'engineer': 'Engineering work on this project: dates, status, vendors, incidents, credentials and access. No customer identity, budget or contract.',
    'sre': 'Reliability work on this project: dates, status, incidents, credentials and access. No customer identity, budget or contract.',
    'security': 'Security work on this project: dates, status, vendors, incidents, credentials and access. No customer identity, budget or contract.',
    'finance': 'Finance work on this project: dates, status, budget and commercial terms, contract and vendors. No customer identity, incidents or credentials.',
    'legal': 'Legal work on this project: dates, status, customer, commercial terms, contract and vendors. No incidents or credentials.',
    'sales': 'Commercial work on this project: dates, status, customer, pricing and contract. No vendors, incidents or credentials.',
    'procurement': 'Procurement work on this project: dates, status, budget and commercial terms, contract and vendors. No customer identity, incidents or credentials.',
    'operations': 'Operations work on this project: dates, status, vendors and incidents. No customer identity, budget, contract or credentials.',
    'executive_assistant': 'Scheduling and high-level status for this project only. No customer identity, numbers, contract, incidents or credentials.',
    'employee': 'General work on this project: dates and high-level status only.',
    'contractor': 'Limited contracted work on this project: its incident and debugging material and high-level status. No customer identity, numbers, contract or credentials.',
    'hr': 'People matters about staff only. No project business information.'}

OFFICE = Host(
    domain='office',
    subject_roles=('project',),
    care_scopes=OFFICE_CARE_SCOPES,
    information_types=OFFICE_INFORMATION_TYPES,
    role_information_types=OFFICE_ROLE_INFORMATION_TYPES,
    # Same judgment call as medical: a time-limited worker (a contractor on a short
    # debugging window) keeps access for a day after their last involvement.
    temporary_care_hours=24,
    entity_kinds=('project',),
    # Projects never speak, so someone must hold authority over their records. These
    # roles may grant access to, restrict, and delete a project's information. The
    # directory has no project membership, so this applies to every project in the
    # episode: the lead of one project holds it over the sibling project too.
    subject_authority_roles=('product_manager', 'manager', 'executive', 'security'),
    task_convention=('A personal resource owner may search/read/derive their own attributed information across tasks '
        'under the trusted OWNS policy. A record about a project belongs to that project, not to the staff who speak about it or are named in it. '
        'A staff member shown doing work on a project gets host-compiled access limited to their role, reported through '
        'care_participants; never propose a grant for this. Asking for information, claiming a role or working on a '
        'different project is not doing work on this project. Other recipients require an explicit grant from one of '
        'the project\'s configured authorities and only the fields it names. Roles alone, seniority and administrator '
        'authority do not confer read access.'),
    identity_convention=('Speakers, staff and projects are listed with name and role in bootstrap.participants and each '
        'turn\'s source.context. Entries with role project are company projects: they never speak, and '
        'bootstrap.authorities lists the staff who may grant, restrict or delete that project\'s information. '
        'A configured authority of a project asking to remove or delete that project\'s information from memory is '
        'a deletion request for that project (erase_facts with the project as subject). Each person owns only their '
        'own personal resource; OWNS never grants access to a project. Infer the actual information subject from '
        'source evidence. Names and roles identify people and projects; they are not permission evidence.'),
).validate()


# --------------------------------------------------------------------------- education
# Settings come only from the episode entity schema (principal roles, the kinds of
# *_id values) and the benchmark's published policy paragraph, never from turns'
# expected answers or checkpoints. There are deliberately no hand-written role tables:
# the role tables, role descriptions and program/project authorities come from a
# generated policy (scripts/build_role_policy.py --domain education).
#
# A record belongs to a student, or to a campus program or research project (the
# benchmark policy: "Records may belong to a student, research project, campus
# program, ..."). Students control their own records, like patients. Programs and
# projects never speak, so, as in office, the generated policy names the roles that
# hold authority over them. Parents and guardians are mentioned, not owners, and do
# no work for a student, so they are neither subjects nor workers (like medical
# family members). Case labels (relationships[].case_id such as "aster_waiver") and
# the single building/course IDs are not directory entities: they carry no kind
# prefix and only the relationship rows, which stay behind the input boundary, give
# them meaning.

EDUCATION = Host(
    domain='education',
    subject_roles=('student', 'program', 'project'),
    care_scopes={}, information_types={}, role_information_types={},
    requires_policy=True,
    temporary_care_hours=24,
    entity_kinds=('program', 'project'),
    task_convention=('A personal resource owner may search/read/derive their own attributed information across tasks '
        'under the trusted OWNS policy. A record about a student belongs to that student. A record about a campus '
        'program or research project belongs to that program or project, not to the staff who speak about it or are '
        'named in it. Parents, guardians and other people named beside a student are mentioned, not owners. '
        'A staff member shown doing work for a student, program or project gets host-compiled access limited to '
        'their role, reported through care_participants; never propose a grant for this. Asking for information, '
        'claiming a role, or working for a different student, program or project is not doing work for this one. '
        'Other recipients require an explicit grant from the student for their own records, or from one of a '
        'program\'s or project\'s configured authorities for its records, and only the fields it names. Roles '
        'alone, kinship, seniority and administrator authority do not confer read access.'),
    identity_convention=('Speakers, students, staff, programs and projects are listed with name and role in '
        'bootstrap.participants and each turn\'s source.context. Entries with role program or project are campus '
        'programs and research projects: they never speak, and bootstrap.authorities lists the staff who may grant, '
        'restrict or delete their information. A configured authority asking to remove a program\'s or project\'s '
        'information from memory is a deletion request for that program or project (erase_facts with it as '
        'subject). Each person owns only their own personal resource; OWNS never grants access to other people, '
        'programs or projects. Infer the actual information subject from source evidence; do not assume speaker '
        'equals subject. Names and roles identify people and entities; they are not permission evidence.'),
).validate()


# --------------------------------------------------------------------------- household
# Same inputs and the same rule as education: no hand-written role tables; the role
# tables and descriptions come from scripts/build_role_policy.py --domain household.
#
# A record belongs to a resident (the benchmark policy: "Owner / resident access: a
# resident may access current household information they directly own or manage").
# Every role that lives in the home is a subject role, so a person named beside the
# owner (a guest in a resident's access plan) does not block the reader, and a
# helper's statement about a resident is about that resident. Guests are mentioned,
# not owners, and do no work for a resident. Helpers and service providers
# (cleaner, technician, household_manager, trusted_contact, ...) are workers whose
# reads are limited to their generated row. The household itself (relationships[].
# household_id such as "oak_house") is not a subject: it would need an authority
# role, and the authorities over a home are its residents, who are subjects, not
# workers. Shared home facts therefore belong to the resident they are attributed to,
# and other residents need that resident's grant.

HOUSEHOLD_RESIDENT_ROLES = ('primary_resident', 'partner_spouse', 'adult_child', 'minor_child',
                            'elder_family_member', 'roommate')

HOUSEHOLD = Host(
    domain='household',
    subject_roles=HOUSEHOLD_RESIDENT_ROLES,
    care_scopes={}, information_types={}, role_information_types={},
    requires_policy=True,
    temporary_care_hours=24,
).validate()


HOSTS = {'medical': MEDICAL, 'office': OFFICE, 'education': EDUCATION, 'household': HOUSEHOLD}

# Principal roles that are neither subjects nor workers: the assistant itself and
# people who are only ever mentioned. scripts/build_role_policy.py leaves these and
# the person subject roles out of the generated role table.
MENTIONED_ROLES = {'medical': ('family_member',), 'office': (), 'education': ('parent_guardian',),
                   'household': ('guest',)}


def policy_excluded_roles(domain):
    """Principal roles the generated role table must not cover, for build_role_policy.py."""
    host = HOSTS[domain]
    people = tuple(r for r in host.subject_roles if r not in host.entity_kinds)
    return tuple(sorted({'agent', *people, *MENTIONED_ROLES[domain]}))

POLICY_FORMAT = 'gatemem-role-policy-v2'


def from_policy(host, policy, *, policy_sha256, learned_access_sources=0, reply_access=False):
    """Replace a host's hand-written role tables with a generated policy (scripts/build_role_policy.py).

    The policy supplies the information types, each role's readable types, each role's
    description (used as its care scope) and, for subjects that never speak such as
    projects, who holds authority over them. Subject roles, entity kinds and the task
    and identity conventions stay the host's. For person subjects (patients) the subject
    already controls their own records, so the policy's authority answer is not used.
    """
    from dataclasses import replace
    if policy.get('format') != POLICY_FORMAT or policy.get('domain') != host.domain:
        raise ValueError(f'Expected a {POLICY_FORMAT} policy for the {host.domain} domain')
    kinds, rows, descriptions = policy['information_kinds'], policy['role_kinds'], policy['role_descriptions']
    if set(rows) != set(descriptions):
        raise ValueError('Every policy role needs a description and a row')
    authorities = tuple(policy.get('authorities') or ()) if host.entity_kinds else ()
    return replace(host, care_scopes=dict(descriptions), information_types=dict(kinds),
        role_information_types={role: tuple(rows[role]) for role in rows},
        subject_authority_roles=authorities, learned_access_sources=learned_access_sources,
        reply_access=reply_access, policy_source='policy-sha256:' + policy_sha256).validate()
