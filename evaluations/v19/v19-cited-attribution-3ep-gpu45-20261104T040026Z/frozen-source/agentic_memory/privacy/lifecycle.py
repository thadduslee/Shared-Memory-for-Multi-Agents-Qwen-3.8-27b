"""Generic, subject-bounded logical erasure of exact source-derived values.

Selectors persist as hashes, not a new archive of deleted values. Source audit
observations remain host-only; readable fields and their generated descendants
are deleted, and later matching fields cannot be published or released.
"""
import hashlib
import json
import re

from .system import encoded


def parts(text):
    words = [m.group().lower() for m in re.finditer(r'\w+', text)]
    result=[]
    for word in words:
        if word.isdecimal() and result and result[-1].isdecimal():
            result[-1]+=word
        else:result.append(word)
    return result


def fingerprint(words):
    return hashlib.sha256(' '.join(words).encode()).hexdigest()


def question_spans(text):
    """Explicit question sentences are evidence of requests, not asserted facts.

    Keep them in the host source prefix for task/consent interpretation. A
    selected assertion in another sentence of the same event remains usable.
    """
    return [(m.start(),m.end()) for m in re.finditer(r'.+?(?:[.!?](?=\s|$)|$)',text,re.S)
            if m.group().rstrip().endswith('?')]


def assertive_spans(text):
    """Candidate ranges outside explicit questions; never an authority grant."""
    spans=[]
    previous=0
    for start,end in [*question_spans(text),(len(text),len(text))]:
        left=previous
        while left<start and text[left].isspace():left+=1
        right=start
        while right>left and text[right-1].isspace():right-=1
        if left<right:spans.append([left,right])
        previous=end
    return spans


def rules(core, compartment, subject):
    def load():
        from .scheduled_forgetting import due_rules
        return [tuple(r) for r in core.db.execute(
            'SELECT fingerprint,words FROM source_erasures WHERE compartment=? AND subject=?', (compartment,subject))]+due_rules(core,compartment,subject)
    return core.snapshot_read(('erasure-rules',compartment,subject,core.clock()),load)


def matches(core, compartment, subject, text, extra=()):
    words = parts(text)
    return any(any(fingerprint(words[i:i+size]) == value for i in range(len(words)-size+1))
               for value,size in [*rules(core,compartment,subject),*extra])


def apply_erasure(core, ctx, compartment, erasure, job_id):
    subject = erasure['subject']
    for quote in erasure['values']:
        words = parts(quote)
        core.db.execute('INSERT OR IGNORE INTO source_erasures VALUES (?,?,?,?,?,?,?)',
            (compartment,subject,fingerprint(words),len(words),erasure['issuer'],encoded(erasure['evidence']),job_id))
    from .fact_candidates import erase
    erase(core,compartment,subject)
    # Delete field roots; the core cascades through notes, indexes and derived
    # artifacts. Original source audit text is not a requester-readable field.
    rows = list(core.db.execute("SELECT id,payload,attributes FROM resources WHERE compartment=? AND state='active' AND kind IN ('observation_view','memory')", (compartment,)))
    roots=[]
    from . import attribution_v19
    disclosed=attribution_v19.installed(core,compartment)
    for row in rows:
        attrs = json.loads(row['attributes'])
        if attrs.get('subject') != subject and not (disclosed and subject in attrs.get('information_subjects',[])):
            continue
        if (matches(core,compartment,subject,json.loads(row['payload']).get('text','')) or
                matches(core,compartment,subject,encoded(attrs))):
            roots.append(row['id'])
    return core.delete_many(ctx,roots) if roots else 0
