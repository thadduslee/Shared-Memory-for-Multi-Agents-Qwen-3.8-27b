"""Allowlisted agent input. Original fixtures remain evaluator-only ground truth."""


def episode_header(episode):
    # Routing identity only: no entities, context, subject hints or future turns.
    return {'episode_id':episode['episode_id']}


def source_turn(turn):
    """Keep the event text verbatim and only its source attribution/envelope.

    Roles, relationships, access levels and arbitrary nested annotations cannot
    cross this boundary. Consent stated in the raw text is legitimate evidence.
    """
    principal=turn['speaker']['principal_id']
    if not isinstance(principal,str) or not principal:
        raise ValueError('Source speaker requires an authenticated identity')
    if not isinstance(turn['turn_id'],str) or not isinstance(turn['text'],str):
        raise ValueError('Source turn ID and text must be strings')
    result={'turn_id':turn['turn_id'],'speaker':{'principal_id':principal},'text':turn['text']}
    for key in ('timestamp','turn_kind'):
        if key in turn:
            if not isinstance(turn[key],str):raise ValueError('Source envelope must contain strings')
            result[key]=turn[key]
    return result
