"""Proposal generation is separate from approval and runtime enforcement.

This component performs inference and exposes protected authority feedback. It
does not train a model, infer issuer legitimacy from text, or activate grants.
"""
import json


class AuthorizationLearner:
    def __init__(self, core, context, processor):
        self.core,self.context,self.processor=core,context,processor

    def propose(self, source_ids, instruction, payload, schema):
        return self.core.process(self.context,self.processor,source_ids,instruction,payload,schema=schema)

    def feedback(self, namespace):
        self.core._session(self.context,'audit',namespace)
        return [{'candidate_id':row['candidate_id'],'version':row['version'],
                 'decision':row['decision'],'candidate':json.loads(row['candidate_body']),
                 'receipt':json.loads(row['receipt'])}
                for row in self.core.db.execute('''SELECT d.*,c.body AS candidate_body FROM capability_decisions d
                    JOIN capability_candidates c ON c.id=d.candidate_id AND c.version=d.version
                    WHERE c.compartment=? AND d.issuer=? ORDER BY d.rowid''',
                    (namespace,self.context.principal_id))]
