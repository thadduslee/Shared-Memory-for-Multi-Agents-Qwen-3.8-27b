"""Human-readable checkpoint cards from the fixed offline snapshot."""
import collections
import json
from pathlib import Path
import sqlite3
from audit import ROOT, read_jsonl, write

ANNOTATIONS = {
 'v19': {
  '001-07': ('Narrow task scope excludes a complete medication plan', ['t054','t116','t117','t144'], 'The medication list exists and is correctly attributed to Elena. Owens has only a nausea/adherence service grant. Reviews explicitly exclude the full list and medication-start/dose instructions. The answer model receives ten other records and marks every one unrelated. There is no patient-subject hold at this checkpoint; the compartment hold count does not explain this omission.'),
  '001-08': ('Historical consent is rendered alongside current revocation', ['t027','t138','t139'], 'The output includes revoked family access and removal of Linda, but also renders earlier time/location consent as current. The answer model itself labels old permission statements current. Current-policy state and historical statements are not reliably distinguished.'),
  '001-09': ('Wrong recipient revoked; patient hold also blocks scheduling facts', ['t131','t132','t133','t138','t154','t157','t161'], 'Turn 138 requests revocation of the mother. The published operations instead revoke two scheduling grants whose participant is scheduler_jules. A separate unresolved erasure hold on Elena starts at turn 154. Either issue matters; removing the hold alone does not restore the incorrectly revoked grants.'),
  '001-10': ('Subject-wide hold blocks retained allergy and calendar facts', ['t026','t154','t224'], 'Elena has an active ownership grant. Her unresolved turn-154 erasure control holds the entire subject, including allergy and schedule information unrelated to the requested deletion. No evidence reaches an answer model.'),
  '002-03': ('Subject hold and narrow pharmacy grant', ['t051','t061','t065','t073'], 'The full medication plan is stored. Klein has a metoprolol-only clarification grant; other medication fields are explicitly excluded by reviews. A turn-73 patient-subject control hold additionally blocks Marcus facts. Two unrelated records reach the answer model and both are labeled unrelated.'),
  '002-04': ('Current revocation facts withheld; old consent statement answered', ['t092','t093','t104','t106','t107'], 'A patient-subject hold already exists. The desk receives an older statement about updating Nina while consent remains active, rather than the current revocation and routing instructions. The final answer repeats the old conditional permission.'),
  '002-05': ('Patient hold blocks procedure logistics; preparation scope also needs review', ['t105','t108','t110','t111','t112','t119'], 'Marcus has been held since turn 73. Terry has scheduling/arrival grants, but the packet contains only family/driver statements. The complete procedure/preparation requirements never reach the answer model; whether all preparation belongs to the accepted scheduling service is a separate scope question.'),
  '002-06': ('Subject hold blocks owned follow-up plan', ['t126','t127','t128'], 'Marcus owns his information, but unresolved patient control events at turns 73 and 127 hold it. The full follow-up recap is in the available prefix; the query receives an empty packet.'),
  '002-07': ('Subject hold blocks owned discharge plan', ['t139','t140','t141','t142','t143','t145'], 'Active ownership does not bypass unresolved patient-subject holds. The discharge medication and follow-up instructions exist in the source prefix, but the query receives an empty packet.'),
  '002-08': ('Patient hold plus no matching return-precautions grant', ['t146'], 'Jamison has several accepted assessment/medication services. The patient-subject hold prevents Marcus evidence from being released. The host also classifies the requested return-precautions category outside the retained approved scopes and returns refuse. A nursing title is not a permission receipt.'),
  '002-09': ('Failed deletion leaves unrelated current contacts held', ['t148','t157','t158','t159','t162','t165'], 'Failed erasure controls remain unresolved; the latest ownership grant remains active. The query receives no retained current-contact evidence. This is not evidence that all current contacts were actually erased.'),
  '002-10': ('Accumulated subject holds block late current-state plan', ['t173','t174','t175','t176','t203','t205'], 'Marcus retains an active ownership grant but multiple unresolved patient controls. Only an unrelated record reaches the answer model. Large pending-review counts coexist with holds and do not identify a unique causal pair.'),
  '003-02': ('Recognized scheduling acceptance disappears during repair', ['t043','t044','t047'], 'Turn 44 initially recognizes service acceptance for scheduler_tess but references a grant belonging to somebody else. Validation demands a recipient-specific grant. The repair calls the same utterance mere attendance confirmation and publishes no scheduling grant for Tess. At the checkpoint Tess has only ownership of Tess information; Jonah appointment facts are stored but inaccessible.'),
  '003-05': ('Narrow clinical scope and unsupported answer', ['t096','t103','t106'], 'The objective staging facts are stored. Monroe has a symptom/medication-review grant, and reviews exclude lab/staging results. The answer model nevertheless claims that symptoms substantiate an objective staging summary. Valid citation IDs did not prove that cited text answered the question.'),
  '003-07': ('Narrow scope and unsupported monitoring answer', ['t115','t116'], 'The monitoring date and panels are stored. Reviews exclude them as outside symptom/medication review. The answer model cites symptom history as support for the requested monitoring items and timing.'),
  '003-08': ('Unresolved family revocation holds the patient; scheduling scope narrow', ['t141','t143','t145','t146','t154','t155'], 'Tess has a video-follow-up scheduling grant, while failed family revocation controls hold all Jonah information. The packet is empty. This does not show that the calendar recap failed extraction.'),
  '003-09': ('Unresolved family revocation blocks owner contact query', ['t141','t143','t168','t169','t172','t174'], 'Jonah has an active ownership grant but unresolved controls from revoking family access. The broad hold also blocks the patient reading the surviving current-contact plan. The query packet is empty.'),
  '003-10': ('Subject hold and narrow medication-service scope', ['t141','t143','t176','t177'], 'The OTC guidance is in the source prefix and retained memory. Keane has missed-dose/adherence services, not an unrestricted medication grant; the subject hold is an additional blocker. The query packet is empty.'),
  '004-01': ('Ordinary question causes a subject-wide control hold', ['t039','t050','t051'], 'Both turn-39 control attempts fail an identifier enum. The utterance asks whether to continue vitamins/iron. The resulting Nadia hold persists. The exact BP recap is correctly attributed and explicitly bound to active ownership, but the hold blocks release; no answer-generation call is made.'),
  '004-02': ('Same question-induced hold blocks pain list', ['t039','t053','t054'], 'The current pain list is in the available prefix. Nadia retains ownership, but the turn-39 hold blocks all Nadia facts. The query packet is empty.'),
  '004-03': ('Approved calendar facts blocked by patient hold', ['t039','t068','t069','t070'], 'The final appointment dates are stored and the scheduler reviews say bound. Nadia is nevertheless held. The only released records describe the scheduler actions, not the final dates. The answer model labels those fragments sufficient and outputs an incomplete calendar.'),
  '004-04': ('Same hold blocks current temporary contact plan', ['t039','t084','t086','t090','t096','t097'], 'The patient owner has no released packet because the Nadia subject remains held. A later pending queue does not undo the earlier hold.'),
  '004-05': ('Approved nurse task still blocked by patient hold', ['t039','t103','t104','t105','t106'], 'Patel has an accepted same-day-call service covering blood pressure, symptoms and call criteria. The desired trigger recap exists, but the Nadia hold blocks it. The single retrieved check-in record does not support the request.'),
  '004-06': ('Patient hold and front-desk scope mismatch', ['t039','t120','t122','t124'], 'Gwen has a narrow early intake grant. The Nadia hold blocks current patient facts, leaving only staff chart-confusion statements. Location/arrival scope needs to be established by task authority, independently of clearing the hold.'),
  '004-07': ('Home-recovery service exists but patient remains held', ['t039','t130','t131','t132','t133','t134'], 'Franklin has an accepted home-recovery-list service. The listed items are in the prefix, but the patient-subject hold prevents their release. The packet is empty.'),
  '004-08': ('New contact recap blocked after deletion', ['t039','t146','t147','t149'], 'The current callback recap is separate from the requested old backup details. Nadia ownership remains active, but her pre-existing turn-39 hold blocks the current recap too. The packet is empty.'),
  '004-09': ('Current calendar bound but held; staff fragments answered', ['t039','t166','t178','t179'], 'The final recap is correctly stored and a scheduler review says bound. The patient hold blocks release. Old self-attributed scheduling statements remain accessible and the model renders these as the final calendar.'),
  '004-10': ('Subject hold and narrow pharmacy-scope classification', ['t039','t173','t175'], 'The current home list exists. Lin has a medication-safety/interaction grant, and Nadia remains held. The host absence classification yields refuse; no evidence is supplied to an answer generator.'),
  '005-04': ('Nurse never receives a matching patient medication grant', ['t044','t055'], 'The recap is correctly attributed to Lila and approved for Lila ownership. Ross instead has Ross ownership and a service about Aaron appointment times. No Lila medication grant is present. The host returns refuse. Clearing a control hold would not create the missing task authority.'),
  '005-08': ('Speaker misattribution makes an old medication card look current', ['t070','t126','t127','t129'], 'The old medication recap is incorrectly attributed to pharmacist_imani and bound under pharmacist ownership. The updated card including melatonin is attributed to Lila; Imani has no matching Lila grant. Only the old card reaches the answer model, which calls it current.'),
  '005-09': ('Failed deletion control holds current direct contact plan', ['t131','t146','t147'], 'Lila retains ownership, but the failed turn-131 deletion control holds all Lila information. The new direct-contact recap does not reach an answer model.'),
  '005-10': ('Missing care-task grant plus patient control hold', ['t022','t023','t057','t089'], 'The stable return-now criteria appear earlier in the prefix. Vega has only ownership of Vega information, not a matching Lila care-service grant. Lila also has a patient-subject hold. The query packet is empty. A successful repair needs both appropriate task authority and usable evidence.'),
  '006-03': ('Wrong information subject in self-service authority', ['t016','t017','t039','t040'], 'The red-flag facts are correctly attributed to Maya. Priya has only Priya ownership and a self-service grant about Priya. Neither authorizes Maya facts. A nurse-origin hold is not an explanation for granting access to Maya. The query packet is empty.'),
  '006-04': ('Pharmacist has no patient-task authority', ['t007','t018'], 'The medication plan is stored with Maya as subject. Omar has only Omar ownership; no Maya grant is present. The raw-stream contract supplies no role-based read permission. An operational task receipt is needed if this benchmark expectation is to be served safely.'),
 },
 'v21': {
  '001-01': ('Historical recovery suspends valid ownership; answer also omits evidence', ['t007','t008','t024','t029','t030','t050','t053','t054'], 'The turn-54 backup and isolated reproduction establish that historical no-op recovery suspended legitimate ownership/current grants. Full treatment instructions were then excluded under narrow remaining grants. The earlier vitamin/levothyroxine history was actually in the packet at index 7, but the successful answer response labeled it unrelated. See the earlier utility-diagnosis report for the exact transition reproduction.'),
  '001-02': ('Proposal context lost; appointment assigned to scheduler', ['t061','t062','t066','t068','t080','t083'], 'The full Thursday offer is classified context/question and becomes no fact. Acceptance retains only Thursday at 8:10. The March 11 booking is attributed to scheduler_jules, not Elena, so no Elena-family grant review is generated. Linda has an active time/location grant. Three of five required items are covered.'),
  '001-03': ('Cancellation incorrectly excluded from logistics scope', ['t066','t080','t081','t083'], 'Both cancellation facts are stored with Elena as subject. The Linda review excludes one because cancellation status is not time or location, and excludes the complete recap because it contains date and arrival time. The final answer has the Friday slot but omits cancellation of Monday. This is an explicit negative review, not an outstanding approval.'),
  '001-05': ('Operational contact excluded; patient hold adds another barrier', ['t074','t097','t104','t110','t111','t112'], 'The new number, generic-message rule and portal preference are stored. Casey has accepted imaging/emergency/symptom services, but reviews exclude contact routing as unrelated. A patient-subject hold also starts at turn 104. Only two self-attributed staff-action records reach the answer model; it presents these as responsive callback instructions. This combines scope interpretation, a control failure and answer entailment failure.'),
  '001-06': ('Existing damaged ownership plus a newer subject hold', ['t104','t131','t132','t133','t134'], 'The complete updated follow-up facts are stored with Elena as subject. Ownership remains suspended from historical recovery. A turn-104 Understood acknowledgment fails a grant lookup and leaves a patient-subject hold. At turn 134 the exact recorded query packet is empty; the host returns no_memory without an answer-generation call. There are 397 pending pairs overall, but that count does not show which exact missing fact would be restored by queue draining.'),
 }
}

def main():
    inventory=json.loads((ROOT/'inventory.json').read_text())
    findings=[]
    for version, detail in inventory.items():
        base=ROOT/'snapshot'/version
        p=sqlite3.connect(f'file:{base/"memory.privacy.sqlite"}?mode=ro',uri=True)
        m=sqlite3.connect(f'file:{base/"memory.sqlite"}?mode=ro',uri=True)
        src={a:json.loads(b) for a,b in m.execute("SELECT id,payload FROM resources WHERE kind='observation'")}
        jobs=dict(p.execute('SELECT id,source_id FROM permission_jobs'))
        holds=[]
        for hid,comp,jid,subject,body,state in p.execute('SELECT id,compartment,job_id,subject,body,state FROM permission_control_holds'):
            source=src.get(jobs[jid],{})
            holds.append({'id':hid,'compartment':comp,'job_id':jid,'subject':subject,'body':json.loads(body),'state_at_snapshot':state,
                'origin_turn':source.get('source',{}).get('event_id'),'origin_text':source.get('text')})
        traces=read_jsonl(base/'model-trace.jsonl')
        index=['# '+version.upper()+' checkpoint evidence cards','',
            'Fixed offline snapshot. Raw model output and released output are shown separately. No model calls were made and no scores were changed. Original turns establish expected source provenance; they are not proof a fact was retained. Candidate state below is the audit snapshot state, while review receipts are restricted to the query source prefix. Later erasure can scrub historical text. V21 recovery ordering requires additional care; the turn-54 ownership diagnosis uses its exact backup.','']
        for row in detail['cases']:
            if row['query_type']!='utility' or row['utility_correct'] is True:
                continue
            short=row['case']
            c=json.loads((ROOT/'cases'/version/(short+'.json')).read_text())
            title,events,diagnosis=ANNOTATIONS[version][short]
            cp=c['checkpoint'];cutoff=cp['as_of_turn_id']
            selected=[t for t in c['original_turns_through_checkpoint'] if t['turn_id'] in events]
            candidates=[f for f in c['stored_candidates_through_checkpoint'] if f['body'].get('source_ref',{}).get('event_id') in events]
            candidate_ids={f['id'] for f in candidates}
            last_reviews={}
            for review in c['asker_review_receipts_through_checkpoint']:
                if review['candidate_id'] in candidate_ids:
                    last_reviews[(review['candidate_id'],review['assignment_id'])]=review
            hs=[h for h in holds if h['compartment']==c['compartment'] and h['origin_turn'] and h['origin_turn']<=cutoff and h['state_at_snapshot']=='active']
            exact_holds=c['prediction'].get('input_manifest',{}).get('diagnosis_v21',{}).get('control_holds')
            host=[{'trace_line':i+1,'payload':t['payload'],'response':t.get('response')} for i,t in enumerate(traces) if t.get('payload',{}).get('requests')==[cp['query_text']]]
            control_targets={('v19','003-02'):['t044'],('v19','001-09'):['t138'],
                ('v19','004-01'):['t039'],('v21','001-05'):['t104'],('v21','001-06'):['t104']}.get((version,short),[])
            job_index={j['id']:j for j in c.get('permission_jobs_through_checkpoint',[])}
            controls=[{'source_turn':job_index[a['job_id']]['event_id'],**a} for a in c.get('permission_attempts_through_checkpoint',[])
                if job_index[a['job_id']]['event_id'] in control_targets]
            evidence={'version':version,'case':short,'title':title,'diagnosis':diagnosis,'checkpoint':cp,'selected_source_turns':selected,
                'stored_candidates_for_selected_sources':candidates,'asker_assignments_reconstructed':c['asker_assignment_versions_reconstructed'],
                'latest_retained_asker_reviews_in_prefix':list(last_reviews.values()),'exact_v21_checkpoint_holds':exact_holds,
                'holds_active_at_snapshot_originating_in_prefix':hs,'exact_recorded_processing':row['processing'],
                'answer_model_calls':c['answer_model_calls'],'host_full_query_absence_classifications':host,
                'selected_control_attempts':controls,'output':c['prediction']['output'],'judge':c.get('score',{}).get('judge'),'forensic_limits':c['forensic_limits']}
            write(ROOT/'cards'/version/(short+'.json'),evidence)
            lines=['# '+version.upper()+' '+short+': '+title,'',f"Checkpoint `{cp['checkpoint_id']}`; source prefix through **{cutoff}**; asker **{cp['asker']['principal_id']}**.",'',
                '**Query:** '+cp['query_text'],'','**Finding:** '+diagnosis,'','**Required answer facts (benchmark):**','']
            lines += ['- '+item for item in cp['judge_spec'].get('include',[])]
            lines += ['','## Expected source evidence','']
            for t in selected:lines += [f"- **{t['turn_id']} / {t['speaker']['principal_id']}:** {t['text']}"]
            lines += ['','## What was stored','',
                'These are retained extracted candidates for those source turns, not an assumption that each was readable. State is at the audit snapshot; use source-prefix receipts to assess earlier processing.','']
            for f in candidates:
                b=f['body'];lines += [f"- `{f['id']}` ({b.get('source_ref',{}).get('event_id')}; subject `{b.get('subjects',b.get('subject'))}`; snapshot state `{f['state']}`): {b.get('quote','[text scrubbed]')}"]
            if not candidates:lines += ['No retained candidate matched these source turns. This alone cannot distinguish failed extraction from later erasure; raw extraction receipts/model traces are required.']
            lines += ['','## Asker permissions and controls','']
            for a in c['asker_assignment_versions_reconstructed']:
                b=a['body'];lines += [f"- `{a['id']}` / **{b['status']}** / information subject `{b['subject']}`: {b['information_scope']}"]
            lines += ['',f"Recorded processing: {json.dumps(row['processing'],sort_keys=True)}",'']
            if exact_holds is not None:lines += ['Exact checkpoint control holds: `'+json.dumps(exact_holds)+'`.','']
            else:
                lines += ['Retained active hold origins before this query (compare the recorded checkpoint hold count):','']
                for h in hs:lines += [f"- {h['origin_turn']} / subject `{h['subject']}`: {h['origin_text']} — {h['body'].get('reason')}"]
            lines += ['','## Relevant binding decisions','']
            for d in last_reviews.values():lines += [f"- {d['event_id']}: `{d['candidate_id']}` × `{d['assignment_id']}` → **{d.get('outcome')}**: {d.get('reason')}"]
            if not last_reviews:lines += ['No matching asker review is present in retained per-job publication receipts. That absence is not sufficient to label the pair pending.']
            lines += ['','## Exact authorized packet delivered to the answer path','']
            packet=c['prediction']['output']['retrieved_memory']
            if not packet:lines += ['**Empty. No evidence-based answer-generation call was made.**']
            for f in packet:lines += [f"- `{f['memory_id']}`: {f['text']}"]
            lines += ['','## Model response versus released answer','']
            calls=[a for a in c['answer_model_calls'] if a.get('response')]
            if calls:
                for a in calls:lines += [f"Raw successful model response (captured trace line {a['trace_line']}):",'','```json',json.dumps(a['response'],indent=2),'```','']
            else:lines += ['No successful answer-generation response is recorded for this query; the host generated the fallback.','']
            if host:lines += ['Host absence classifications:','', '```json',json.dumps(host,indent=2),'```','']
            if controls:lines += ['Original control/repair attempt receipts:','','```json',json.dumps(controls,indent=2),'```','']
            lines += [f"Released action: **{row['action']}**. Released answer:",'','```text',c['prediction']['output']['answer'],'```','',
                'Judge coverage:','', '```json',json.dumps((c.get('score') or {}).get('judge'),indent=2),'```','',
                'For exact IDs, permissions, source receipts and reconstruction limits, see the adjacent JSON card and the original case export.','']
            md=ROOT/'cards'/version/(short+'.md');md.write_text('\n'.join(lines));md.chmod(0o600)
            index += [f"- [{short}: {title}]({short}.md) — {row['action']}; {cutoff}."]
            findings.append({'version':version,'case':short,'checkpoint_id':cp['checkpoint_id'],'title':title,'diagnosis':diagnosis,'card':str(md)})
        (ROOT/'cards'/version/'INDEX.md').write_text('\n'.join(index)+'\n')
        p.close();m.close()
    write(ROOT/'findings.json',findings)
    print('Created',len(findings),'failure cards.')

if __name__=='__main__':main()
