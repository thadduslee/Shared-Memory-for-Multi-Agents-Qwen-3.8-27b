"""Allowlisted agent input. Original fixtures remain evaluator-only ground truth."""
import re

TITLES={'dr','dr.','nurse','pharmacist','lab','tech'}
# Trusted identity metadata that crosses the boundary. Relationships, access
# levels, subject hints and parenthetical relationship labels do not.
INPUT_CONTRACT='identity-metadata-stream-v1'


def identity_directory(episode):
    """Person -> name, role and the literal names they can be referred to by."""
    if 'directory' in episode:return episode['directory']
    people={}
    for principal in episode.get('entities',{}).get('principals',[]):
        name=re.sub(r'\s*\([^)]*\)','',principal.get('display_name','')).strip()
        words=[word for word in name.split() if word.casefold() not in TITLES]
        people[principal['principal_id']]={'name':name,'role':principal.get('role'),
            'aliases':list(dict.fromkeys(filter(None,[name,' '.join(words),*words])))}
    return people


def entity_directory(episode, kinds):
    """Non-person entities, such as projects, from their IDs alone.

    Only the distinct ``<kind>_id`` values cross: who belongs to which project,
    access levels and every other relationship field stay behind the boundary.
    ``project_maple`` becomes "Project Maple", also called "Maple". Directory
    aliases match case-sensitively and as whole words, so "Maple" never matches
    inside "Maplemark".
    """
    found={}
    for relation in episode.get('entities',{}).get('relationships',[]):
        for kind in kinds:
            value=relation.get(kind+'_id')
            if not isinstance(value,str) or not value.startswith(kind+'_'):continue
            short=' '.join(word.capitalize() for word in value[len(kind)+1:].split('_') if word)
            if not short:continue
            name=kind.capitalize()+' '+short
            found[value]={'name':name,'role':kind,'aliases':[name,short]}
    return dict(sorted(found.items()))


def episode_header(episode, entity_kinds=()):
    # Routing identity and the identity directory: no relationships, access
    # levels, context, subject hints or future turns.
    directory=identity_directory(episode)
    if entity_kinds and 'directory' not in episode:
        clash=set(directory)&set(entity_directory(episode,entity_kinds))
        if clash:raise ValueError(f'Entity IDs collide with people: {sorted(clash)}')
        directory={**directory,**entity_directory(episode,entity_kinds)}
    return {'episode_id':episode['episode_id'],'directory':directory}


def source_turn(turn):
    """Keep the event text verbatim and only its source attribution/envelope.

    Relationships, access levels and arbitrary nested annotations cannot cross this
    boundary. Speaker names and roles come only from identity_directory. Consent
    stated in the raw text is legitimate evidence.
    """
    principal=turn['speaker']['principal_id']
    if not isinstance(principal,str) or not principal:
        raise ValueError('Source speaker requires an authenticated identity')
    if not isinstance(turn['turn_id'],str) or not isinstance(turn['text'],str):
        raise ValueError('Source turn ID and text must be strings')
    result={'turn_id':turn['turn_id'],'speaker':{'principal_id':principal},'text':turn['text']}
    # reply_to: the earlier turn this one answers, as chat tools record it. No GateMem
    # turn has it, so for this benchmark the field never crosses and nothing changes.
    for key in ('timestamp','turn_kind','reply_to'):
        if key in turn:
            if not isinstance(turn[key],str):raise ValueError('Source envelope must contain strings')
            result[key]=turn[key]
    return result
