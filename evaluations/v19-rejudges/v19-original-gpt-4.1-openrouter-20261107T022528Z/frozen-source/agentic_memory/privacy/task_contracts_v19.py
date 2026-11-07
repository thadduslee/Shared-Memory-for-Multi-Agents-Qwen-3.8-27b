"""Trusted, opt-in V19 task contracts. Dependency rules are never model output.

The registry proves category membership over a COMPLETE fact. The existing
semantic binding review remains independently necessary. Neither mechanism
alone grants a document, subject, or recipient permission.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import re

from .system import AccessDenied, digest, encoded, instant

SECTIONS = ('prerequisite_inputs', 'outputs', 'resulting_instructions',
            'updates_cancellations', 'follow_through')
READ_ACTIONS = {'read', 'search', 'derive'}


class TaskContractError(ValueError):
    """Unknown, ambiguous, unsupported, or out-of-envelope task."""


class DependencyRegistry:
    """Host configuration, copied at entry and pinned by content hash in storage.

    Patterns are trusted configuration, not instructions, topic hints or runtime
    model classifications. {items} expands only to escaped task-specific literals.
    """
    def __init__(self, configuration):
        self._body = deepcopy(configuration)
        b = self._body
        if set(b) != {'id', 'version', 'tenant', 'categories', 'tasks', 'dialogue', 'terminal_patterns'}:
            raise TaskContractError('Invalid registry fields')
        if any(not isinstance(b[k], str) or not b[k] for k in ('id', 'version', 'tenant')):
            raise TaskContractError('Registry identity/version/tenant required')
        if not b['categories'] or not b['tasks']:
            raise TaskContractError('Empty registry')
        for name, category in b['categories'].items():
            if not name or set(category) != {'description', 'pattern', 'dependencies'}:
                raise TaskContractError('Invalid category')
            if not category['description'] or not isinstance(category['pattern'], str):
                raise TaskContractError('Category description and complete-fact pattern required')
            re.compile(category['pattern'].replace('{items}', '(?:literal)'))
            if not isinstance(category['dependencies'], list):
                raise TaskContractError('Invalid dependencies')
        self.closure(list(b['categories']))  # validates even unreachable cycles
        for task in b['tasks'].values():
            if set(task) != {*SECTIONS, 'objective', 'actions', 'max_lifetime_seconds'}:
                raise TaskContractError('Invalid task category')
            if not task['objective'] or not task['actions'] or not set(task['actions']) <= READ_ACTIONS:
                raise TaskContractError('Only configured read-pipeline actions are supported')
            if type(task['max_lifetime_seconds']) is not int or task['max_lifetime_seconds'] <= 0:
                raise TaskContractError('Bounded task lifetime required')
            for section in SECTIONS:
                if not isinstance(task[section], list):raise TaskContractError('Invalid section')
                self.closure(task[section])
        for rule in b['dialogue']:
            if set(rule) != {'task_category', 'offer_pattern', 'item_separator', 'acceptances'}:
                raise TaskContractError('Invalid dialogue rule')
            self.task(rule['task_category'])
            if set(re.compile(rule['offer_pattern']).groupindex) != {'items'}:
                raise TaskContractError('Offer must capture task items only')
            re.compile(rule['item_separator'])
            for acceptance in rule['acceptances']:
                if set(acceptance) != {'pattern', 'closed_categories', 'excluded_categories'}:
                    raise TaskContractError('Invalid acceptance policy')
                re.compile(acceptance['pattern'])
                for key in ('closed_categories', 'excluded_categories'):
                    values = acceptance[key]
                    if values is None and key == 'closed_categories':continue
                    if not isinstance(values, list) or set(values) - b['categories'].keys():
                        raise TaskContractError('Unknown closed/excluded category')
        for terminal in b['terminal_patterns']:
            if set(terminal) != {'pattern','state'} or terminal['state'] not in {'CANCELLED','REVOKED','COMPLETED'}:
                raise TaskContractError('Invalid terminal policy')
            if '{task_id}' not in terminal['pattern']:raise TaskContractError('Terminal needs an exact task reference')
            re.compile(terminal['pattern'].replace('{task_id}', 'literal'))

    @property
    def body(self):return deepcopy(self._body)

    @property
    def identity(self):
        return {k: self._body[k] for k in ('id', 'version')} | {'digest': digest(self._body)}

    def task(self, name):
        if name not in self._body['tasks']:raise TaskContractError('Unknown task category')
        return deepcopy(self._body['tasks'][name])

    def closure(self, roots, envelope=None):
        result = set(); visiting = set()
        def visit(name):
            if name not in self._body['categories']:raise TaskContractError('Unknown dependency category')
            if name in visiting:raise TaskContractError('Dependency cycle')
            if name in result:return
            if envelope is not None and name not in envelope:
                raise TaskContractError('Dependency exceeds authority envelope')
            visiting.add(name)
            for dependency in self._body['categories'][name]['dependencies']:visit(dependency)
            visiting.remove(name);result.add(name)
        for root in roots:visit(root)
        return sorted(result)

    def categories(self, receipt):
        task = self.task(receipt['task_category'])
        roots = [c for section in SECTIONS for c in task[section]]
        categories = self.closure(roots, receipt['authority_envelope']['categories'])
        closed = receipt['operational_constraints']['closed_categories']
        denied = set(receipt['explicit_exclusions'])
        # Denied prerequisites make their dependants unusable too. No partial
        # closure may smuggle an excluded prerequisite back into the contract.
        return [c for c in categories if not (set(self.closure([c])) & denied)
                and (closed is None or set(self.closure([c])) <= set(closed))]

    def fact_allowed(self, receipt, text):
        items = receipt['operational_constraints']['items']
        literals = '(?:' + '|'.join(re.escape(i) for i in items) + ')'
        matches = [name for name, c in self._body['categories'].items()
                   if re.fullmatch(c['pattern'].replace('{items}', literals), text)]
        # Ambiguous classification is unavailable, even if both categories allow.
        return len(matches) == 1 and matches[0] in self.categories(receipt)


def install(updater, registry):
    if registry is not None and updater.version.split('-', 1)[0] != '19':
        raise TaskContractError('Task contracts are V19 only')
    if updater.version.split('-', 1)[0] != '19':return
    c = updater.core
    c.db.execute('''CREATE TABLE IF NOT EXISTS privacy.task_policies_v19(
        compartment TEXT PRIMARY KEY, body TEXT NOT NULL)''')
    c.db.execute('''CREATE TABLE IF NOT EXISTS privacy.task_receipts_v19(
        compartment TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL,
        body TEXT NOT NULL, PRIMARY KEY(compartment,id,version))''')
    old = c.db.execute('SELECT body FROM task_policies_v19 WHERE compartment=?', (updater.compartment,)).fetchone()
    body = registry.body if registry is not None else None
    if old and json.loads(old[0]) != body:
        raise TaskContractError('Registry change requires explicit migration to a fresh compartment')
    if old is None:
        c.db.execute('INSERT INTO task_policies_v19 VALUES (?,?)', (updater.compartment, encoded(body)))


def registry_for(core, compartment):
    if not core.db.execute("SELECT 1 FROM privacy.sqlite_master WHERE name='task_policies_v19'").fetchone():return None
    def load():
        row = core.db.execute('SELECT body FROM task_policies_v19 WHERE compartment=?', (compartment,)).fetchone()
        return DependencyRegistry(json.loads(row[0])) if row and json.loads(row[0]) is not None else None
    return core.snapshot_read(('task-registry-v19',compartment),load)


def receipts(core, compartment):
    if not core.db.execute("SELECT 1 FROM privacy.sqlite_master WHERE name='task_receipts_v19'").fetchone():return {}
    return {r['id']: json.loads(r['body']) for r in core.db.execute('''SELECT a.* FROM task_receipts_v19 a
        WHERE compartment=? AND version=(SELECT MAX(version) FROM task_receipts_v19 b
        WHERE b.compartment=a.compartment AND b.id=a.id) ORDER BY id''', (compartment,))}


def source(updater, source_id, *, host_evidence=False):
    c = updater.core
    if not c._source_live(source_id) or not c._current(source_id):
        raise TaskContractError('Original evidence is unavailable')
    row = c._resource(updater.ctx, source_id, 'derive', updater.processor.processor_id)
    kinds={'observation','host_task_evidence'} if host_evidence else {'observation'}
    if row['compartment'] != updater.compartment or row['kind'] not in kinds:
        raise TaskContractError('Evidence crosses namespace/type boundary')
    return row['payload']


def evidence(source_id, body):
    return {'source_id': source_id, 'digest': digest(body), 'quote': body['text'],
            'participant': body['source']['participant']}


def build_receipt(registry, *, task_id, namespace, subject, worker, requester,
                  beneficiary, authority, category, items, activation, expiry,
                  original_evidence, closed=None, exclusions=(), envelope=None):
    task = registry.task(category)
    start, end = instant(activation), instant(expiry)
    if start is None or end is None or end <= start or end-start > task['max_lifetime_seconds']:
        raise TaskContractError('Task lifetime exceeds policy')
    if not items or len(items) > 64 or any(not isinstance(i, str) or not i.strip() for i in items):
        raise TaskContractError('Exact task items required')
    roots = [c for section in SECTIONS for c in task[section]]
    if any(not isinstance(v,str) or not v or len(v)>256 for v in
           (task_id,namespace,subject,worker,requester,beneficiary,authority)):
        raise TaskContractError('Bounded task and principal identities required')
    envelope = deepcopy(envelope) if envelope is not None else {
        'subject': subject, 'worker': worker, 'actions': task['actions'],
        'categories': registry.closure(roots), 'activation': activation, 'expiry': expiry}
    if (envelope['subject'] != subject or envelope['worker'] != worker
            or not set(task['actions']) <= set(envelope['actions'])
            or start < instant(envelope['activation']) or end > instant(envelope['expiry'])):
        raise TaskContractError('Task exceeds subject/action/lifetime envelope')
    registry.closure(roots, envelope['categories'])
    known = registry.body['categories'].keys()
    if (closed is not None and set(closed) - known) or set(exclusions) - known:
        raise TaskContractError('Unknown field limit')
    row = {'contract_version': 1, 'id': task_id, 'version': 1, 'namespace': namespace,
           'tenant': registry.body['tenant'], 'subject': subject, 'worker': worker,
           'requester': requester, 'beneficiary': beneficiary, 'authority': authority,
           'service_objective': task['objective'], 'task_category': category,
           **{s: task[s] for s in SECTIONS}, 'allowed_actions': task['actions'],
           'operational_constraints': {'items': list(items), 'closed_categories': closed},
           'explicit_exclusions': list(exclusions), 'activation': activation, 'expiry': expiry,
           'state': 'ACTIVE', 'original_evidence': original_evidence,
           'generated_topic_hints': [], 'topic_hints_are_authority': False,
           'dependency_policy': registry.identity, 'authority_envelope': envelope}
    row['authorized_categories'] = registry.categories(row)
    return row


def _write(updater, receipt):
    updater.core.db.execute('INSERT INTO task_receipts_v19 VALUES (?,?,?,?)',
        (updater.compartment, receipt['id'], receipt['version'], encoded(receipt)))


def publish(updater, receipt):
    """Called only after a trusted evidence validator. Never a model operation."""
    c = updater.core
    old = receipts(c, updater.compartment).get(receipt['id'])
    if old:
        if old != receipt:raise TaskContractError('Conflicting task replay; never reactivate an ended receipt')
        return old
    registry = registry_for(c, updater.compartment)
    aid = 'contract-task:' + digest([updater.compartment, receipt['id']])
    state = updater._state()
    if aid in state['assignment']:raise TaskContractError('Task assignment ID collision')
    refs = [v['source_id'] for k, v in receipt['original_evidence'].items() if isinstance(v, dict) and 'source_id' in v]
    scope = encoded({'objective': receipt['service_objective'],
        'authorized_categories': {name: registry.body['categories'][name]['description']
                                  for name in receipt['authorized_categories']},
        'constraints': receipt['operational_constraints'], 'exclusions': receipt['explicit_exclusions']})
    a = {'id': aid, 'participant': receipt['worker'], 'subject': receipt['subject'],
         'authority': receipt['authority'], 'basis': 'explicit', 'status': 'active',
         'actions': receipt['allowed_actions'], 'information_scope': scope,
         'task_receipt_v19': receipt['id'], 'task_contract_digest_v19': digest(receipt),
         'bindings': [], 'validity': {'effective_from': receipt['activation'], 'effective_until': receipt['expiry']},
         'evidence': {'information_scope': refs, 'authority': refs},
         'scope_evidence': [{'source_id': sid, 'text': source(updater, sid,host_evidence=True)['text']} for sid in refs],
         'policy_hash': digest(__import__('dataclasses').asdict(updater.bootstrap)), 'inference_version': updater.version}
    # A new receipt cannot erase a previously authenticated recipient withdrawal.
    # New task convenience is not subject reauthorization. Keep residual field /
    # action semantics, including their independent identity/evidence receipts.
    inherited={}
    for prior in state['assignment'].values():
        if prior['subject']!=a['subject'] or prior['participant']!=a['participant']:continue
        for request in prior.get('recipient_revocations',[]):inherited[digest(request)]=request
    # Also preserve withdrawals made before any grant existed. Their meaning
    # comes from authenticated source/identity receipts, never the grant set.
    from . import revocation_v19
    from .permissions import PermissionProposalRejected
    prefix={r['id']:json.loads(r['payload']) for r in c.db.execute(
        "SELECT id,payload FROM resources WHERE compartment=? AND kind='observation' AND state='active' ORDER BY rowid",
        (updater.compartment,)) if c._current(r['id'])}
    for sid,body in prefix.items():
        if not re.search(r'\brevoke\b',body['text'],re.I):continue
        try:requests=revocation_v19.requests(updater,prefix,sid)
        except PermissionProposalRejected:continue  # retained uncertainty/holds still gate access
        for request in requests:
            if request['subject']==a['subject'] and a['participant'] in request['recipients']:
                inherited[digest(request)]=request
    if inherited:
        a['recipient_revocations']=deepcopy(list(inherited.values()))
        a['evidence']['recipient_resolution']=sorted({p['source_id'] for r in inherited.values()
            for p in [r['reference'],*[p for values in r['identity_relationship_evidence'].values() for p in values]]})
    _write(updater, receipt)
    state['assignment'][aid] = a
    updater._publish((state, {('assignment', aid)}, []), 'task-receipt:' + receipt['id'])
    c.db.execute('UPDATE permission_bootstrap SET revision=revision+1 WHERE compartment=?', (updater.compartment,))
    c._tickets.clear()
    return receipt


def accept_dialogue(updater, offer_source_id, acceptance_source_id, *, task_id, generated_topic_hints=()):
    """Host-only explicit admission of two original authenticated observations.

    Closed grammars are configurable. The caller cannot choose an arbitrary task
    category, dependency, subject, recipient, field limit or generated topic.
    """
    c = updater.core;c._session(updater.ctx, 'manage_policy', updater.compartment)
    with c.transaction():
        updater._current_configuration()
        registry = registry_for(c, updater.compartment)
        if registry is None:raise TaskContractError('Task registry is disabled')
        if (not isinstance(generated_topic_hints,(tuple,list)) or len(generated_topic_hints)>32
                or any(not isinstance(v,str) or len(v)>256 for v in generated_topic_hints)):
            raise TaskContractError('Bounded non-authoritative topic hints required')
        offer, acceptance = source(updater, offer_source_id), source(updater, acceptance_source_id)
        for sid in (offer_source_id, acceptance_source_id):
            job = c.db.execute('SELECT status FROM permission_jobs WHERE id=?', (updater._job_id(sid),)).fetchone()
            if not job or job[0] != 'complete':raise TaskContractError('Evidence needs completed control assessment')
        ordered = [r[0] for r in c.db.execute("SELECT id FROM resources WHERE compartment=? AND kind='observation' ORDER BY rowid", (updater.compartment,))]
        old=receipts(c,updater.compartment).get(task_id)
        if old:
            if (old['original_evidence'].get('mode')=='dialogue_acceptance'
                    and old['original_evidence']['offer']==evidence(offer_source_id,offer)
                    and old['original_evidence']['acceptance']==evidence(acceptance_source_id,acceptance)):
                return old
            raise TaskContractError('Conflicting task replay')
        if any(r['original_evidence'].get('mode')=='dialogue_acceptance'
               and r['original_evidence']['offer']['source_id']==offer_source_id
               and r['original_evidence']['acceptance']['source_id']==acceptance_source_id
               for r in receipts(c,updater.compartment).values()):
            raise TaskContractError('Original acceptance already has a stable task identity')
        if ordered[-1]!=acceptance_source_id:
            raise TaskContractError('New dialogue admission must occur at its acceptance boundary')
        if ordered.index(offer_source_id) >= ordered.index(acceptance_source_id):
            raise TaskContractError('Offer must precede acceptance')
        subject = acceptance['source']['participant'];worker = offer['source']['participant']
        bootstrap = updater._source_bootstrap({sid: source(updater, sid) for sid in ordered
                    if c._source_live(sid) and c._current(sid)})
        if subject not in bootstrap.authorities.get(subject, {}).get('permission', []) or worker == subject:
            raise TaskContractError('Acceptance lacks supported subject authority')
        if offer['source'].get('subject') not in (None, subject) or acceptance['source'].get('subject') not in (None, subject):
            raise TaskContractError('Evidence has a different subject')
        matches = []
        for rule in registry.body['dialogue']:
            m = re.fullmatch(rule['offer_pattern'], offer['text'])
            if m:
                for limit in rule['acceptances']:
                    if re.fullmatch(limit['pattern'], acceptance['text']):matches.append((rule, limit, m))
        if len(matches) != 1:raise TaskContractError('Unknown or ambiguous dialogue mapping')
        rule, limit, match = matches[0]
        # Bare acceptance cannot select among compatible workers/offers. No
        # recency, title, roster or existing grant may break the tie.
        compatible_rules=[r for r in registry.body['dialogue'] if any(
            re.fullmatch(a['pattern'],acceptance['text']) for a in r['acceptances'])]
        compatible = [sid for sid in ordered[:ordered.index(acceptance_source_id)]
            if c._source_live(sid) and c._current(sid)
            and source(updater, sid)['source']['participant'] != subject
            and source(updater, sid)['source'].get('subject') in (None, subject)
            and any(re.fullmatch(r['offer_pattern'], source(updater, sid)['text']) for r in compatible_rules)]
        if compatible != [offer_source_id]:raise TaskContractError('Ambiguous original offer')
        task = registry.task(rule['task_category'])
        activation = acceptance['source'].get('observed_at')
        if instant(activation) is None:raise TaskContractError('Trusted observation time required')
        expiry = (datetime.fromisoformat(activation.replace('Z', '+00:00')) +
                  timedelta(seconds=task['max_lifetime_seconds'])).isoformat()
        receipt = build_receipt(registry, task_id=task_id, namespace=updater.compartment,
            subject=subject, worker=worker, requester=subject, beneficiary=subject, authority=subject,
            category=rule['task_category'], items=re.split(rule['item_separator'], match['items']),
            activation=activation, expiry=expiry, closed=limit['closed_categories'], exclusions=limit['excluded_categories'],
            original_evidence={'mode': 'dialogue_acceptance', 'offer': evidence(offer_source_id, offer),
                               'acceptance': evidence(acceptance_source_id, acceptance)})
        receipt['generated_topic_hints']=list(generated_topic_hints)
        return publish(updater, receipt)


def gate(core, compartment, assignment, *, candidate=None, action=None):
    rid = assignment.get('task_receipt_v19')
    if not rid:return True
    def validated():
        registry = registry_for(core, compartment)
        receipt = receipts(core, compartment).get(rid)
        if (registry is None or receipt is None or receipt.get('contract_version')!=1
                or receipt['state'] != 'ACTIVE'):return None
        if (receipt['dependency_policy'] != registry.identity or receipt['namespace'] != compartment
                or receipt['tenant'] != registry.body['tenant']
                or receipt['subject'] != assignment.get('subject') or receipt['worker'] != assignment.get('participant')
                or not set(assignment['actions']) <= set(receipt['allowed_actions'])):return None
        now = instant(core.clock())
        if now is None or not instant(receipt['activation']) <= now < instant(receipt['expiry']):return None
        for value in receipt['original_evidence'].values():
            if not isinstance(value, dict) or 'source_id' not in value:continue
            sid = value['source_id']
            if not core._source_live(sid) or not core._current(sid):return None
            row = core.db.execute('SELECT payload FROM resources WHERE id=?', (sid,)).fetchone()
            if not row or digest(json.loads(row[0])) != value['digest']:return None
        return registry, receipt
    pair = core.snapshot_read(('task-contract-v19', compartment, rid, assignment.get('subject'),
        assignment.get('participant'),tuple(assignment.get('actions',())),core.clock()), validated)
    if pair is None:return False
    registry, receipt = pair
    if action is not None and action not in receipt['allowed_actions']:return False
    return candidate is None or (candidate.get('subject') == receipt['subject'] and
                                 registry.fact_allowed(receipt, candidate['quote']))


def end(updater, receipt, state, terminal_evidence):
    """End only this receipt's grant. Historical binding decisions stay reusable."""
    if receipt['state']!='ACTIVE':return receipt
    updated={**receipt,'version':receipt['version']+1,'state':state,'terminal_evidence':terminal_evidence}
    _write(updater,updated)
    ledger=updater._state();touched=set()
    for aid,a in ledger['assignment'].items():
        if a.get('task_receipt_v19')==receipt['id']:
            a['status']='expired' if state=='EXPIRED' else 'revoked';touched.add(('assignment',aid))
    updater._publish((ledger,touched,[]),'task-ended:'+receipt['id'])
    updater.core.db.execute('UPDATE permission_bootstrap SET revision=revision+1 WHERE compartment=?',(updater.compartment,))
    updater.core._tickets.clear()
    return updated


def synchronize(updater, sources):
    registry=registry_for(updater.core,updater.compartment)
    if registry is None:return
    for receipt in receipts(updater.core,updater.compartment).values():
        if receipt['state']!='ACTIVE':continue
        now=instant(updater.core.clock())
        if now is not None and now>=instant(receipt['expiry']):
            end(updater,receipt,'EXPIRED',{'clock':updater.core.clock()});continue
        acceptance=receipt['original_evidence'].get('acceptance',{}).get('source_id')
        ordered=list(sources)
        if acceptance not in ordered:continue
        for sid in ordered[ordered.index(acceptance)+1:]:
            body=sources[sid];actor=body['source']['participant']
            if body['source'].get('subject') not in (None,receipt['subject']):continue
            for rule in registry.body['terminal_patterns']:
                if not re.fullmatch(rule['pattern'].replace('{task_id}',re.escape(receipt['id'])),body['text']):continue
                boot=updater._source_bootstrap(sources)
                if (actor in boot.authorities.get(receipt['subject'],{}).get('restrict',[])
                        or actor==receipt['worker'] and rule['state']=='COMPLETED'):
                    receipt=end(updater,receipt,rule['state'],evidence(sid,body));break


def protect_assignments(old, new):
    """The semantic wire cannot strip/replace trusted receipt constraints."""
    for aid, a in old['assignment'].items():
        if not a.get('task_receipt_v19'):continue
        b = new['assignment'].get(aid, {})
        for key in ('task_receipt_v19', 'task_contract_digest_v19', 'information_scope',
                    'participant', 'subject', 'authority', 'actions', 'scope_evidence'):
            if a.get(key) != b.get(key):raise TaskContractError('Model cannot modify a trusted task contract')
        av,bv=a.get('validity',{}),b.get('validity',{})
        for key in ('effective_from','effective_until'):
            if av.get(key)==bv.get(key):continue
            old_time,new_time=instant(av.get(key)),instant(bv.get(key))
            if new_time is None or (old_time is not None and
                (new_time<old_time if key=='effective_from' else new_time>old_time)):
                raise TaskContractError('Model cannot extend a trusted task lifetime')
        if av.get('end_activity')!=bv.get('end_activity'):
            raise TaskContractError('Model cannot replace task end conditions')


def report(updater):
    registry = registry_for(updater.core, updater.compartment)
    rows = receipts(updater.core, updater.compartment)
    configured=bool(updater.core.db.execute("SELECT 1 FROM privacy.sqlite_master WHERE name='host_task_policies_v19'").fetchone()
        and updater.core.db.execute('SELECT 1 FROM host_task_policies_v19 WHERE compartment=?',(updater.compartment,)).fetchone())
    return {'enabled': registry is not None, 'dependency_policy': registry.identity if registry else None,
            'dialogue_receipts': sum(r['original_evidence']['mode'] == 'dialogue_acceptance' for r in rows.values()),
            'host_receipts': sum(r['original_evidence']['mode'] == 'authenticated_host' for r in rows.values()),
            'host_integration': 'configured' if configured else 'disabled',
            'host_events_available': any(r['original_evidence']['mode']=='authenticated_host' for r in rows.values()),
            'real_model_validation': 'needed'}
