"""Bounded medical source lifecycle controls; hashes prevent value retention.

No scoring labels are accepted. Exact source-derived selectors are stored only
as normalized hashes. New source occurrences are redacted before indexing.
"""
import hashlib
import json
import re


def tokens(text):return re.findall(r'\w+',text.lower())
def fingerprint(parts):return hashlib.sha256(' '.join(parts).encode()).hexdigest()


def identifiers(text):
    found=re.findall(r'\b\d{3}[- .]\d{3}[- .]\d{4}\b',text)
    found+=re.findall(r'\b(?:alias|ask for|Manager)\s+(?:is |was )?([A-Z][a-z]+)\b',text)
    found+=re.findall(r'\b(?:[A-Z][a-z]+ ){1,3}(?:House|Steps|Lodge)(?: unit [A-Z0-9]+)?\b',text)
    found+=re.findall(r'\bgate code\s+(\d+)\b',text,re.I)
    found+=re.findall(r"['\"]([^'\"\n]{5,100})['\"]",text)
    return [re.sub(r'^(?:The|Delete|Please)\s+','',v) for v in found]


class SourceLifecycle:
    def __init__(self,adapter):
        self.a=adapter
        # Adapter-owned control data lives in the privacy store, never the source index.
        adapter.core.db.execute('''CREATE TABLE IF NOT EXISTS privacy.erasure_selectors(
          compartment TEXT,subject TEXT,hash TEXT,words INTEGER,source_id TEXT,
          PRIMARY KEY(compartment,subject,hash))''')

    def erase(self,turn,subject):
        a=self.a;text=turn.get('text','')
        values=identifiers(text)
        candidates=[]
        for row in a.core.db.execute("SELECT id,payload,attributes FROM resources WHERE compartment=? AND kind='observation' AND state='active'",(a.compartment,)):
            if json.loads(row['attributes']).get('subjects') != [subject]:continue
            candidates.append((row['id'],json.loads(row['payload']).get('text','')))
        # Explicit site/alias deletion also removes associated phone/alias fields
        # from matching prior source statements; unrelated current phones survive.
        seeds=list(values)
        for rid,body in candidates:
            from .operational import segments
            for start,end in segments(body):
                part=body[start:end]
                if not any(' '.join(tokens(v)) in ' '.join(tokens(part)) for v in seeds):continue
                associated=identifiers(part)
                phones=re.findall(r'\b\d{3}[- .]\d{3}[- .]\d{4}\b',part)
                # A mixed current/backup recap is not authority to erase both.
                if len(phones)>1:associated=[v for v in associated if v not in phones]
                values.extend(associated)
        values={tuple(tokens(v)) for v in values if tokens(v)}
        if not values:
            a.core.unresolved_assignment(a.service,'erasure:'+a.eid+':'+turn['turn_id'],
                {'reason':'unresolved_erasure_selector','source_turn':turn['turn_id']})
            return
        for parts in values:
            a.core.db.execute('INSERT OR IGNORE INTO erasure_selectors VALUES (?,?,?,?,?)',
                (a.eid,subject,fingerprint(parts),len(parts),a.observation_id(turn['turn_id'])))
        for rid,body in candidates:
            if self.matches(body,subject):a.core.delete(a.service,rid)

    def matches(self,text,subject):
        parts=tokens(text)
        for row in self.a.core.db.execute('SELECT hash,words FROM erasure_selectors WHERE compartment=? AND subject=?',(self.a.eid,subject)):
            size=row['words']
            if any(fingerprint(parts[i:i+size])==row['hash'] for i in range(len(parts)-size+1)):return True
        return False

    def redact(self,text,subjects):
        # Keep complete unaffected source sentences and an explicit redaction.
        from .operational import segments
        output=[]
        for start,end in segments(text):
            part=text[start:end]
            if any(self.matches(part,s) for s in subjects):
                output.append('[Previously deleted source values are unavailable.]')
            else:output.append(part)
        return ' '.join(output)


def is_request(turn):
    text=turn.get('text','')
    return bool('?' in text or re.search(
        r'\b(asking whether|asking for|send me|tell me whether|recover the deleted|fill in the|finish this deleted|was the deleted)\b',text,re.I))
