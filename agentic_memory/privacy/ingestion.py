"""Generic source envelopes and independently approved modality adapters.

Text and JSON are included. Document/media parsers are explicit plugin objects,
not a claim that a text embedder understands every modality.
"""
from __future__ import annotations
from dataclasses import dataclass
import base64
import json

from .system import digest


@dataclass(frozen=True)
class SourceEnvelope:
    observation_id: str
    policy_id: str
    compartment: str
    media_type: str
    external_id: str
    revision: str
    authenticated_issuer: str
    payload: bytes


class TextAdapter:
    processor_id='builtin:text-v1'

    def parse(self,payload):
        text=payload.decode('utf-8')
        return [{'text':text,'locator':{'byte_start':0,'byte_end':len(payload)}}]


class JsonAdapter:
    processor_id='builtin:json-v1'

    def parse(self,payload):
        value=json.loads(payload)
        rows=[]
        def visit(item,pointer,depth):
            if depth>16 or len(rows)>256:
                raise ValueError('Structured projection budget exceeded')
            if isinstance(item,dict):
                for key,child in item.items():
                    visit(child,pointer+'/'+key.replace('~','~0').replace('/','~1'),depth+1)
            elif isinstance(item,list):
                for i,child in enumerate(item):visit(child,pointer+'/'+str(i),depth+1)
            else:
                rows.append({'text':pointer+' = '+json.dumps(item,ensure_ascii=False),
                             'typed_value':item,'locator':{'json_pointer':pointer}})
        visit(value,'',0)
        return rows


class IngestionGateway:
    def __init__(self,core):
        self.core=core
        self.adapters={}

    def register(self,ctx,media_type,adapter):
        self.core.register_processor(ctx,adapter)
        self.adapters[media_type]=adapter

    def ingest(self,ctx,envelope):
        self.core._session(ctx,'ingest',envelope.compartment)
        if envelope.authenticated_issuer!=ctx.principal_id:
            raise ValueError('Envelope issuer must match authenticated source identity')
        if len(envelope.payload)>5_000_000:
            raise ValueError('Source exceeds configured ingestion budget')
        with self.core.transaction():
            source=self.core.create(ctx,envelope.observation_id,'observation',{
                'payload_base64':base64.b64encode(envelope.payload).decode(),
                'media_type':envelope.media_type,'external_id':envelope.external_id,
                'source_revision':envelope.revision,'digest':digest(envelope.payload.hex())},
                policy_id=envelope.policy_id,compartment=envelope.compartment)
            adapter=self.adapters.get(envelope.media_type)
            if adapter is None:
                self.core.db.execute("UPDATE resources SET state='quarantine' WHERE id=?",(source,))
                return {'state':'quarantine','source_id':source,'memory_ids':[]}
            self.core._processing_route(ctx,adapter)
            self.core.require(ctx,source,'derive',adapter.processor_id)
            projections=adapter.parse(envelope.payload)
            if not isinstance(projections,list) or len(projections)>256:
                raise ValueError('Adapter returned invalid projection budget')
            notes=[]
            for index,projection in enumerate(projections):
                if not isinstance(projection,dict) or not isinstance(projection.get('text'),str) or 'locator' not in projection:
                    raise ValueError('Source-addressable projection required')
                rid='note:'+source+':'+str(index)
                self.core.create(ctx,rid,'memory',{'text':projection['text'],'semantic':{},
                    'source_ref':{'observation_id':source,'source_revision':envelope.revision,
                                  'locator':projection['locator']},'typed_value':projection.get('typed_value')},
                    policy_id=envelope.policy_id,compartment=envelope.compartment,dependencies=[source])
                self.core.index(ctx,rid);notes.append(rid)
            return {'state':'active','source_id':source,'memory_ids':notes}
