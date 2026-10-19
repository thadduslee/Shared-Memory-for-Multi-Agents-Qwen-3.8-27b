"""Bound prompt views while leaving authoritative artifacts untouched."""
import json


def compact(value, *, items=8, chars=1600):
    if isinstance(value, dict):
        return {k: compact(v, items=items, chars=chars) for k, v in value.items()}
    if isinstance(value, list):
        shown = [compact(v, items=items, chars=chars) for v in value[:items]]
        return shown + ([{'omitted_items': len(value) - items}] if len(value) > items else [])
    if isinstance(value, str) and len(value) > chars:
        return value[:chars] + f' [omitted {len(value)-chars} characters; request diagnostic if needed]'
    return value


def prompt_view(context, source, level):
    """Representative traces retain their actual IDs; omission is explicit."""
    if level == 0:
        return {'context': context, 'implementation': source}
    view = compact(context, items=max(2, 8 // level), chars=max(300, 1800 // level))
    # Avoid repeating the same claims/packet at several levels of the prompt.
    dossier = view.get('architect_incident_dossier') or {}
    for target in dossier.get('targets') or []:
        if isinstance(target, dict):
            for key in ('renderer_input', 'evidence_packet'):
                target.pop(key, None)
    snippets = {name: text[:max(2000, 16000 // level)] for name, text in source.items()}
    return {'context': view, 'implementation': snippets,
            'compaction_level': level, 'implementation_excerpted': True,
            'missing_evidence_action': 'REQUEST_DIAGNOSTIC; never infer a fact from an omitted excerpt'}


def serialized_view(context, source, level):
    return json.dumps(prompt_view(context, source, level), ensure_ascii=False)
