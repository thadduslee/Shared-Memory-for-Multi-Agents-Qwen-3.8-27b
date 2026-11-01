"""Closed, benchmark-only operational interpretation. No query/scoring inputs.

Authority is the configured fixture workflow convention, NOT staff authorship.
This compiler accepts authenticated task performance in a subject-isolated source
stream as a workflow receipt. Unknown/mixed fields and ambiguous extensions stay
unresolved. See AUTHORITY_POLICY.md before using another source adapter.
"""
import json
import re

SCOPES = ('appointments', 'medications', 'contact', 'restricted_contact', 'sharing', 'transport', 'care', 'clinical', 'lab_schedule')
TASKS = {
    'scheduler': ('scheduling', ('appointments',), r'\b(booked|scheduled|confirmed|appointments?|schedule|arrival|arrive|slot|parking|calendar|check-in|rescheduled)\b'),
    'pharmacist': ('pharmacy', ('medications', 'contact'), r'\b(medication|medicine|prescription|dose|take|start|stop|hold|pharmacy|application|tablet|mg|milligram)\b'),
    'nurse': ('nursing-care', ('appointments', 'medications', 'contact', 'restricted_contact', 'sharing', 'care', 'clinical', 'lab_schedule'), r'\b(triage|documented|instructions|recap|review|call|return|go|take|hold|monitor|precautions|intake|callback plan|check completed|red flags)\b'),
    'reception': ('front-desk', ('contact', 'sharing', 'appointments'), r'\b(checked in|check in|arrive|arrival|check-in|contact|callback|documented|confirmed|family|sharing|release|number|portal|parking)\b'),
    'social_worker': ('coordination', ('contact', 'restricted_contact', 'sharing', 'transport'), r'\b(callback|contact|backup|transport|ride|bus|voucher|assistance|routing)\b'),
    'lab_tech': ('laboratory', ('lab_schedule',), r'\b(drawing|order|repeat|lab|CBC|CMP|date|scheduled)\b'),
}
SCOPE_TERMS = {
    'appointments':'appointments schedule calendar arrival parking changes cancellation preparation',
    'medications':'medications medicines doses active stopped counseling current plan',
    'contact':'contact callback routing phone mobile voicemail portal current plan',
    'restricted_contact':'contact callback backup routing temporary instructions',
    'sharing':'sharing access revocation permissions family status current',
    'transport':'transportation transport backup assistance ride arrangements routing',
    'care':'care instructions return now triggers red flags urgent earlier call precautions',
    'clinical':'clinical laboratory lab summary results recap',
    'lab_schedule':'laboratory lab orders repeat date schedule monitoring follow-up',
}

PATTERNS = {
    'appointments': r'\b(appointments?|arrival|arrive|scheduled|booked|slots?|visits?|calendar|parking|entrance|check-in|check in|cancel\w*|reschedul\w*|pickup|pick-up)\b',
    'medications': r'\b(medication|medicine|prescription|dose|tablet|mg|milligram|ibuprofen|acetaminophen|levothyroxine|vitamin|NSAID\w*|application|pump|wash hands|upper.arm|furosemide|spironolactone)\b',
    'contact': r'\b(callback|contact|mobile|cell|portal|voicemail|phone|number|routing|reach|call me|call only)\b',
    'restricted_contact': r'\b(callback|contact|mobile|cell|portal|voicemail|phone|number|routing|reach|call me|call only)\b',
    'sharing': r'\b(family access|family-access|sharing|revoked|revoke|release|permission|logistics.only|logistics only|no disclosure|no longer.*share)\b',
    'transport': r'\b(transport\w*|bus|voucher|ride|backup.*help|same.day assistance)\b',
    'care': r'\b(precautions|red.flags|triggers|go in|emergency|call.*(?:if|for)|return|fever|worsening|faint|severe|heavy bleeding|home.care|monitor|seek|call us)\b',
    'clinical': r'\b(lab recap|lab summary|clinical recap|results? recap|CBC|CMP|creatinine|potassium|hemoglobin|platelet|WBC|repeat.*posted)\b',
    'lab_schedule': r'\b(repeat|lab|CBC|CMP|blood draw|order)\b',
}
# Fields with particularly different audiences cannot ride along on an unrelated
# operational sentence. These bounded exclusions deliberately sacrifice recall.
PRIVATE = r'\b(alias|shelter|housing|recovery house|diagnos\w*|HIV|syphilis|lesion|cytology|biopsy result|pathology|sexual|pregnan\w*|hepatitis|assault)\b'


def segments(text):
    start = 0
    for match in re.finditer(r'(?<=[.!?])\s+(?=[A-Z])|;\s+(?=(?:Backup|Primary|Generic|do not mention))|\n+', text):
        # Keep titles and initials with the following source sentence.
        if re.search(r'\b(?:Dr|Ms|Mr)\.$', text[start:match.start()]):
            continue
        if text[start:match.start()].strip(): yield start, match.start()
        start = match.end()
    if text[start:].strip(): yield start, len(text)


def field_spans(text, scope, *, family=False):
    selected = []
    for a,b in segments(text):
        part = text[a:b]
        if '?' in part or not re.search(PATTERNS[scope], part, re.I): continue
        if scope in {'appointments', 'lab_schedule'}:
            if scope == 'lab_schedule' and not re.search(r'\b(order|repeat|scheduled|Monday|Tuesday|Wednesday|Thursday|Friday|AM|PM)\b|\d:\d',part,re.I): continue
            if re.search(r'\b(potassium|WBC|hemoglobin|creatinine|symptoms?|lesion|cytology|diagnosis|mg|positive|negative|blood pressure)\b',part,re.I): continue
            # Schedules do not carry result values or unrelated contact secrets.
            if re.search(r'\b(mIU|result|positive|negative|diagnosis|alias|voicemail)\b|\d{3}[- .]\d{3}[- .]\d{4}',part,re.I): continue
            if scope == 'lab_schedule' and re.search(r'\b(mg|medicine|medication|lesion|cytology|WBC|hemoglobin|creatinine|potassium|showed|shows|posted|result)\b',part,re.I): continue
        elif scope == 'medications':
            if re.search(PRIVATE,part,re.I) or re.search(PATTERNS['contact']+'|'+PATTERNS['sharing'],part,re.I): continue
        elif scope in {'contact','restricted_contact','sharing','transport'}:
            restricted=bool(re.search(r'\b(House|Lodge|Steps|shelter|lodging|housing|staying|alias|ask for|Cedar Bridge|outreach.center|backup (?:line|number|route|contact))\b',part,re.I))
            if scope == 'contact' and restricted: continue
            if scope == 'restricted_contact' and not restricted: continue
            if re.search(PATTERNS['medications']+'|'+PATTERNS['clinical']+r'|\b(lesion|cytology|positive|negative)\b',part,re.I): continue
        if family and (re.search(PRIVATE,part,re.I) or re.search(r'\b(mg|dose|results?|medication|contact|callback|phone|portal|voicemail|prep|fasting|therapy|psychiatric|behavioral|panic|potassium|high|symptom|reason|clinic type)\b',part,re.I)):
            continue
        if family and not re.search(r'\d|\b(parking|garage|entrance|canceled|cancelled|ride times|check.in instructions)\b',part,re.I): continue
        selected.append((a,b))
    return selected[:32]


class OperationalCompiler:
    def __init__(self, adapter):
        self.a = adapter

    def _aid(self, pid, subject, scope):
        return f'{self.a.eid}:{subject}:{pid}:{scope}'

    def observe(self, turn, subjects, source_id):
        a = self.a
        pid = turn.get('speaker', {}).get('principal_id')
        role = a.principals.get(pid, {}).get('role')
        text = turn.get('text', '')
        if role not in TASKS: return
        if len(subjects) != 1 or subjects[0] != a.primary:
            a.core.unresolved_assignment(a.service, source_id, {'reason':'ambiguous_or_other_subject', 'source_id':source_id})
            return
        subject = subjects[0]
        task, scopes, pattern = TASKS[role]
        # Mere greetings, questions, claimed visibility or requested access do not
        # constitute a performance receipt under this fixture convention.
        claimed = bool(re.search(r'\b(I only see|I can see|I have access|I am allowed|not assigned)\b',text,re.I))
        restriction = role == 'pharmacist' and bool(re.search(r'\b(medication.only|medicine.only|only need.*medicine)\b',text,re.I))
        if restriction:
            a.core.revoke_assignment(a.service,self._aid(pid,subject,'contact'))
            a.core.unresolved_assignment(a.service,'scope:'+self._aid(pid,subject,'contact'),
                {'reason':'explicit_medication_only_scope', 'source_id':source_id, 'required':'independent authority for contact extension'})
        if claimed or '?' in text or not re.search(pattern,text,re.I):
            if claimed:
                a.core.unresolved_assignment(a.service,source_id,{'reason':'self_assertion_is_not_authority','source_id':source_id})
            return
        for scope in scopes:
            aid = self._aid(pid,subject,scope)
            existing = a.core.db.execute('SELECT state FROM assignments WHERE id=?',(aid,)).fetchone()
            if existing: continue  # never revive a revoked scope from later speech
            if scope=='contact' and a.core.db.execute('SELECT 1 FROM assignment_diagnostics WHERE id=?',('scope:'+aid,)).fetchone(): continue
            if scope == 'transport':
                # Only a patient-accepted plan is shared; offers alone stay pending.
                continue
            a.core.install_assignment(a.service,aid,principals=[pid],subject=subject,
                compartment=a.compartment,information_scope=scope,task=task,actions=['search','read','derive','link'],
                provenance={'authority':a.config['operational_convention'],
                            'evidence':[{'source_id':source_id,'turn_id':turn['turn_id']}],
                            'basis':'documented_fixture_workflow_convention'})

    def accepted_transport(self, turn, subjects, source_id):
        a = self.a
        pid = turn.get('speaker',{}).get('principal_id')
        if subjects != [a.primary] or pid != a.primary: return
        if not re.search(r'\b(document|save|yes|please|backup|that works)\b',turn.get('text',''),re.I): return
        # Only preceding source turns already ingested; no future or checkpoint data.
        previous = a.core.db.execute("SELECT id,payload FROM resources WHERE compartment=? AND kind='observation' AND state='active' ORDER BY created DESC LIMIT 3",(a.compartment,)).fetchall()
        for row in previous:
            payload = json.loads(row['payload']); speaker = payload.get('source',{}).get('speaker_id')
            if a.principals.get(speaker,{}).get('role') != 'social_worker': continue
            if not re.search(PATTERNS['transport'],payload.get('text',''),re.I): continue
            aid = self._aid(speaker,a.primary,'transport')
            a.core.install_assignment(a.service,aid,principals=[speaker],subject=a.primary,
                compartment=a.compartment,information_scope='transport',task='accepted-transport',actions=['search','read','derive','link'],
                provenance={'authority':a.config['operational_convention'],
                    'evidence':[{'source_id':row['id']},{'source_id':source_id}], 'basis':'patient_acceptance_and_workflow_convention'})
            break

    def views(self, turn, subjects, source_id, text):
        a = self.a
        if len(subjects) != 1 or re.search(r'\b(I only see|I can see|I have access|I am allowed)\b',text,re.I): return []
        subject = subjects[0]
        notes = []
        speaker = turn.get('speaker',{}).get('principal_id')
        role = a.principals.get(speaker,{}).get('role')
        assigned = any(r.get('clinician_id') == speaker and r.get('patient_id') == subject
            for r in a.episode['entities'].get('relationships',[]))
        if speaker not in {subject,*a.config['control_principals']} and role not in TASKS and not assigned: return []
        for scope in SCOPES:
            if role in TASKS and scope not in TASKS[role][1]: continue
            spans = field_spans(text, scope)
            if (not spans and role == 'lab_tech' and scope == 'lab_schedule' and
                    re.fullmatch(r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b[^?]*\d[^?]*',text,re.I) and
                    not re.search(r'\b(result|posted|WBC|mg|g/dL|positive|negative)\b',text,re.I)):
                # An authenticated lab worker's bare date in the established
                # laboratory workflow is a scheduling field, not a result.
                spans=[(0,len(text))]
            if not spans: continue
            notes.append(a.core.ingest_authoritative_view(a.service,source_id,source_id+':'+scope,spans,
                policy_id=a.policy(subject)+':fields',
                attributes={'subject':subject,'subjects':subjects,'information_scope':scope,
                    'turn_id':turn['turn_id'],'sequence':int(turn['turn_id'][1:]),'view_authority':a.config['operational_convention'], 'retrieval_terms':SCOPE_TERMS[scope]}))
        # A complete source clause may be released to family only under a separate
        # field ACL and live family membership; no disconnected date/time fragments.
        spans,labels = family_fields(text)
        if spans:
            notes.append(a.core.ingest_authoritative_view(a.service,source_id,source_id+':logistics',spans,
                policy_id=a.policy(subject)+':logistics',field_labels=labels))
        return notes


def family_fields(text):
    """Typed exact selectors: no specialty, diagnosis or procedure labels.

    Appointment groups retain their date/time, status and approved generic
    location. Change wording determines prior/current fields when unambiguous.
    Ambiguous prose stays unavailable rather than sharing a whole clinical clause.
    """
    spans=[];labels=[]
    day=r'(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)'
    month=r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
    date_time=rf'\b(?:{day}(?:,?\s+)(?:{month}\s+\d{{1,2}}(?:,?\s+))?(?:at\s+)?)?\d{{1,2}}:\d{{2}}\s*(?:AM|PM)\b'
    count=0
    for start,end in segments(text):
        part=text[start:end]
        if '?' in part or not re.search(PATTERNS['appointments']+'|'+r'\b(ride times|check.in instructions)\b',part,re.I):continue
        if re.search(r'\b(mg|dose|medication|phone|callback|voicemail|alias|results?|diagnosis)\b',part,re.I):continue
        if re.fullmatch(r'Ride times and check.in instructions only\.?',part,re.I):
            spans.append((start,end));labels.append('permitted_assistance');continue
        times=list(re.finditer(date_time,part,re.I))
        for index,match in enumerate(times):
            count+=1;prefix=f'appointment_{count}.'
            name='time'
            if re.search(r'\bfrom\b',part[:match.start()],re.I) and len(times)>1:
                name='previous_time' if index==0 else 'current_time'
            elif re.search(r'\b(moved|rescheduled|updated|current)\b',part,re.I):name='current_time'
            spans.append((start+match.start(),start+match.end()));labels.append(prefix+name)
            # Same-clause status is retained explicitly instead of losing cancel
            # or arrival semantics when clinical service labels are withheld.
            status=re.search(r'\b(cancelled|canceled|moved|rescheduled|confirmed|tentative|held|booked|pickup|arrival|check-in|arrive)\b',part,re.I)
            if status:
                spans.append((start+status.start(),start+status.end()));labels.append(prefix+'status')
            location=re.search(r'\b(?:Suite|Room|Building|Garage)\s+[A-Z0-9][A-Z0-9 -]{0,12}\b',part)
            if location:
                spans.append((start+location.start(),start+location.end()));labels.append(prefix+'location')
        # A separate parking/access clause may have no clock but remains useful.
        if not times and re.search(r'\b(parking|garage|entrance)\b',part,re.I) and not re.search(PRIVATE,part,re.I):
            if not re.search(r'\b(clinic|therapy|biopsy|neurology|hematology|nephrology|rheumatology|infectious|EEG|MRI)\b',part,re.I):
                parking=re.search(r'\b(?:parking\s+)?garage\s+[A-Z0-9]+\b',part,re.I)
                if parking:
                    spans.append((start+parking.start(),start+parking.end()));labels.append('parking_location')
                voucher=re.search(r'\bparking voucher\b',part,re.I)
                if voucher:
                    spans.append((start+voucher.start(),start+voucher.end()));labels.append('parking_assistance')
    return spans[:32],labels[:32]
