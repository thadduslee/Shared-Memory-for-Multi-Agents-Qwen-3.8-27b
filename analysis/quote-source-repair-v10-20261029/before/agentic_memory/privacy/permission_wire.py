"""Compact model deltas; canonical evidence and enforcement stay unchanged."""
from copy import deepcopy


def delta_contract(payload, schema):
    payload=deepcopy(payload)
    schema=deepcopy(schema)
    payload['contract_notes']={**payload['contract_notes'], 'wire':'compact-delta-v1',
        'binding':'Reuse assignment_id; select source_id and exact quote. Return necessity, '
                  'scope_supported, context_preserved, metadata_safe; evidence for necessity, '
                  'subject and authorization; explicit restriction_decisions for ALL final '
                  'restrictions, including new ones. Do not repeat assignment definitions.',
        'coverage':'Return {reviewed:[every coverage_required principal], evidence:[source IDs], '
                   'unresolved:[{principal,evidence,reason}]}. Empty unresolved means no '
                   'unresolved recipient. Changed labels are derived from validated operations.'}
    for variant in schema['properties']['operations']['items']['anyOf']:
        props=variant['properties']
        if props['op']['enum']!=['bind_information']:continue
        checks=props.pop('self_check')['properties']
        props.pop('explanation')
        for key in ('scope_supported','context_preserved','metadata_safe'):
            props[key]={**checks[key],'minLength':1,'maxLength':320}
        props['necessity']={**props['necessity'],'minLength':1,'maxLength':320}
        props['evidence']['properties'].pop('source_id',None)
        props['evidence']['required']=[k for k in props['evidence']['required'] if k!='source_id']
        variant['required']=[k for k in variant['required'] if k not in {'self_check','explanation'}]
        variant['required']+=['scope_supported','context_preserved','metadata_safe']
    if 'coverage' in schema['properties']:
        original=schema['properties']['coverage']['items']['properties']
        schema['properties']['coverage']={
            'type':'object','additionalProperties':False,
            'properties':{
                'reviewed':{'type':'array','items':original['principal']},
                'evidence':{**original['evidence'],'minItems':1},
                'unresolved':{'type':'array','items':{
                    'type':'object','additionalProperties':False,
                    'properties':{k:original[k] for k in ('principal','evidence','reason')},
                    'required':['principal','evidence','reason']}}},
            'required':['reviewed','evidence','unresolved']}

    def compact_proposal(value):
        """Keep repair examples in the same vocabulary as the response schema."""
        value=deepcopy(value)
        for op in value.get('operations',[]):
            if op.get('op')!='bind_information' or 'self_check' not in op:continue
            checks=op.pop('self_check')
            op.pop('explanation',None)
            for key in ('scope_supported','context_preserved','metadata_safe'):
                op[key]=checks.get(key,'')
            op.get('evidence',{}).pop('source_id',None)
            if 'spans' in op:
                source=next((s for s in payload['sources'] if s['id']==op['source_id']),None)
                spans=op.pop('spans')
                if source and len(spans)==1:
                    op['quote']=source['text'][spans[0][0]:spans[0][1]]
                else:op['spans']=spans  # Invalid examples remain explicit; never guess a quote.
        if isinstance(value.get('coverage'),list):
            entries=value['coverage']
            value['coverage']={
                'reviewed':[d['principal'] for d in entries],
                'evidence':list(dict.fromkeys(s for d in entries for s in d['evidence'])),
                'unresolved':[{k:d[k] for k in ('principal','evidence','reason')}
                              for d in entries if d['decision']=='unresolved']}
        return value

    if 'correction' in payload:
        payload['correction']['previous_proposal']=compact_proposal(payload['correction']['previous_proposal'])

    def expand(value):
        from .permissions import PermissionProposalRejected
        def fail(message):raise PermissionProposalRejected(message)
        if not isinstance(value,dict):fail('Permission response must be an object')
        value=deepcopy(value)
        if not isinstance(value.get('operations'),list):fail('operations must be a list')
        for op in value['operations']:
            if not isinstance(op,dict):fail('Each operation must be an object')
            # Legacy canonical objects remain usable by scripted/offline callers.
            if op.get('op')!='bind_information' or 'self_check' in op:continue
            keys=('scope_supported','context_preserved','metadata_safe')
            if any(k not in op for k in (*keys,'necessity','source_id','evidence')):
                fail('Compact binding requires necessity, scope, context, metadata and evidence')
            if not isinstance(op['evidence'],dict):fail('Binding evidence must be an object')
            if 'explanation' in op or 'source_id' in op['evidence']:
                fail('Compact binding includes duplicate canonical bookkeeping')
            checks={k:op.pop(k) for k in keys}
            op['self_check']={**checks,'task_requires':op['necessity'],
                'restrictions_checked':'Host verifies explicit decisions cover the final restriction set.'}
            op['explanation']=op['necessity']
            op['evidence']['source_id']=[op['source_id']]
        coverage=value.get('coverage')
        if isinstance(coverage,dict):
            if set(coverage)!={'reviewed','evidence','unresolved'}:
                fail('Compact coverage requires reviewed, evidence and unresolved')
            reviewed=coverage['reviewed'];unresolved=coverage['unresolved']
            if (not isinstance(reviewed,list) or any(not isinstance(p,str) for p in reviewed)
                    or len(set(reviewed))!=len(reviewed) or not isinstance(unresolved,list)):
                fail('Coverage must review each required principal exactly once')
            shared=coverage['evidence']
            if not isinstance(shared,list) or not shared or any(not isinstance(s,str) for s in shared):
                fail('Coverage requires nonempty shared source evidence')
            pending={}
            for entry in unresolved:
                if (not isinstance(entry,dict) or set(entry)!={'principal','evidence','reason'}
                        or not isinstance(entry['principal'],str) or entry['principal'] not in reviewed
                        or entry['principal'] in pending):fail('Invalid unresolved coverage entry')
                if (not isinstance(entry['evidence'],list) or not entry['evidence']
                        or any(not isinstance(s,str) for s in entry['evidence'])):
                    fail('Unresolved coverage requires live source evidence')
                pending[entry['principal']]=entry
            value['coverage']=[{'principal':principal,
                'decision':'unresolved' if principal in pending else 'unchanged',
                'evidence':list(dict.fromkeys(shared+pending.get(principal,{}).get('evidence',[]))),
                'reason':pending[principal]['reason'] if principal in pending else
                         'Model acknowledged recipient review; host derives changes from validated operations.'}
                for principal in reviewed]
        return value

    return payload,schema,expand
