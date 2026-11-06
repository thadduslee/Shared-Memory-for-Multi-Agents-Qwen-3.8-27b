"""Exact source/text choices for constrained binding generation.

Choices select text only. They supply no subject, permission or scope decision.
Arbitrary finer excerpts can be requested through bounded exact-quote retrieval.
"""
from copy import deepcopy
import re

from .lifecycle import assertive_spans


def binding_quotes(text, extra=()):
    spans=assertive_spans(text)
    choices=[]
    def add(quote):
        if not isinstance(quote,str) or not quote or quote in choices:return
        start=text.find(quote)
        if start<0 or start!=text.rfind(quote):return
        if not any(left<=start and start+len(quote)<=right for left,right in spans):return
        choices.append(quote)
    for left,right in spans:
        add(text[left:right])
        # Preserve sentence context by default; semicolons/newlines offer narrow
        # clauses without domain-specific field or permission classifications.
        for part in re.split(r'(?<=[.!])\s+|;\s*|\n+',text[left:right]):add(part.strip())
    for quote in extra:add(quote)
    return choices


def couple_binding_quotes(payload, schema):
    """Each schema branch pairs a source with its actual exact excerpts."""
    payload=deepcopy(payload);schema=deepcopy(schema)
    extras={s['id']:[] for s in payload['sources']}
    texts={s['id']:s['text'] for s in payload['sources']}
    for assignment in payload['permission_state']['assignment'].values():
        for binding in assignment.get('bindings',[]):
            sid=binding['source_id']
            if sid in texts:
                extras[sid].extend(texts[sid][left:right] for left,right in binding['spans'])
    for result in payload.get('context',{}).get('retrieval_results',[]):
        if result['kind']=='quote':
            for sid in result['source_ids']:
                if sid in extras:extras[sid].append(result['reference'])
    choices={}
    for source in payload['sources']:
        quotes=binding_quotes(source['text'],extras[source['id']])
        source['binding_quotes']=quotes
        if quotes:choices[source['id']]=quotes
    variants=[]
    for variant in schema['properties']['operations']['items']['anyOf']:
        if variant['properties']['op']['enum']!=['bind_information']:
            variants.append(variant);continue
        for sid,quotes in choices.items():
            branch=deepcopy(variant);props=branch['properties']
            props['quote']={'type':'string','enum':quotes}
            props['source_id']={'type':'string','enum':[sid]}
            # Select actual text before its paired source, preventing independent
            # source/text decoding from combining a fact with another turn.
            order=('op','assignment_id','quote','source_id')
            branch['properties']={**{k:props[k] for k in order},
                                  **{k:v for k,v in props.items() if k not in order}}
            variants.append(branch)
    schema['properties']['operations']['items']['anyOf']=variants
    payload['contract_notes']['quote_selection']=(
        'For each binding choose a quote and its matching source_id from that source binding_quotes. '
        'Never attach text from another source. If the exact narrower excerpt you need is missing, '
        'request context_requests [{kind:"quote",reference:"exact source substring",offset:0}] '
        'with no mutations; the host will return matching live sources and add the excerpt to choices. '
        'Selecting text never authorizes it: original scope, necessity and final restrictions still apply.')
    return payload,schema
