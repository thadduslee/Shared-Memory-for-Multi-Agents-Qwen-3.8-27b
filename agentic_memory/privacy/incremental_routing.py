"""Bounded semantic routing of protected facts; never an access decision.

Vectors live for one source update only. Existing source-dependent indexed vectors
are reused; unindexed protected facts use the approved local embedding route.
Only the permission model and canonical validator may make a fact readable.
"""
import json
import math


def cosine(a,b):
    norm=math.sqrt(sum(x*x for x in a)*sum(x*x for x in b))
    return sum(x*y for x,y in zip(a,b))/norm if norm else 0.0


class Routing:
    def __init__(self,updater,text):
        self.updater=updater;self.text=text;self.vectors={};self.scope_vectors={};self.event_vector=None

    def prepare(self,candidates,state):
        core=self.updater.core
        core._processing_route(self.updater.ctx,core.embedder)
        wanted={c['id']:c for c in candidates if c['id'] not in self.vectors}
        if wanted:
            for row in core.db.execute('''SELECT r.attributes,r.payload,v.vector FROM resources r
                JOIN representations v ON v.id=r.id WHERE r.state='active' AND v.space=?
                AND r.compartment=? AND r.kind IN ('memory','observation_view')''',
                (core.embedder.space_id,self.updater.compartment)):
                cid=json.loads(row['attributes']).get('fact_candidate_id')
                if cid in wanted and json.loads(row['payload']).get('text')==wanted[cid]['quote']:
                    self.vectors[cid]=json.loads(row['vector'])
        texts=[];keys=[]
        for candidate in candidates:
            if candidate['id'] in self.vectors:continue
            core._resource(self.updater.ctx,candidate['source_id'],'derive',core.embedder.processor_id)
            texts.append(candidate['quote']);keys.append(('fact',candidate['id']))
        for scope in dict.fromkeys(a['information_scope'] for a in state['assignment'].values()):
            if scope not in self.scope_vectors:
                texts.append(scope);keys.append(('scope',scope))
        if self.event_vector is None:texts.append(self.text);keys.append(('event',None))
        for (kind,key),vector in zip(keys,core.embedder.encode(texts) if texts else []):
            if kind=='fact':self.vectors[key]=vector
            elif kind=='scope':self.scope_vectors[key]=vector
            else:self.event_vector=vector

    def history(self,candidates,state,limit=12):
        self.prepare(candidates,state)
        if len(candidates)<=limit:return candidates
        ranked=sorted(candidates,key=lambda c:cosine(self.vectors[c['id']],self.event_vector),reverse=True)
        # Retain an age lane and recent context as well as semantic matches.
        ids={c['id'] for c in candidates[:1]+candidates[-2:]}
        for candidate in ranked:
            if len(ids)>=limit:break
            ids.add(candidate['id'])
        return [c for c in candidates if c['id'] in ids]

    def select(self,required,candidates,state,new_source_id=None,limit=12,visible=None):
        if limit<=0:return []
        self.prepare(candidates,state)
        byid={c['id']:c for c in candidates};order={c['id']:i for i,c in enumerate(candidates)}
        required=[r for r in required if visible is None or r['candidate_id'] in visible]
        def score(pair):
            vector=self.vectors[pair['candidate_id']]
            scope=state['assignment'][pair['assignment_id']]['information_scope']
            priority=0.
            requested=getattr(self.updater,'_review_priority',None)
            if requested and requested[0]==state['assignment'][pair['assignment_id']]['participant']:
                priority+=1.
            if self.updater is not None and 17<=int(self.updater.version.split('-',1)[0])<20:
                a=state['assignment'][pair['assignment_id']]
                # Own-subject reviews have the main call. Reserve the focused
                # call's first slots for independent actual recipient checks.
                priority+=.4 if a['participant']!=a['subject'] else 0.
            return .7*cosine(vector,self.scope_vectors[scope])+.3*cosine(vector,self.event_vector)+priority
        fresh=[r for r in required if byid[r['candidate_id']]['source_id']==new_source_id]
        old=[r for r in required if byid[r['candidate_id']]['source_id']!=new_source_id]
        def diverse(pairs,budget):
            ranked=sorted(pairs,key=lambda p:(-score(p),order[p['candidate_id']],p['assignment_id']))
            result=[];seen=set();recipients=set()
            for pair in ranked:
                if pair['candidate_id'] in seen:continue
                result.append(pair);seen.add(pair['candidate_id'])
                recipients.add((pair['candidate_id'],state['assignment'][pair['assignment_id']]['participant']))
                if len(result)>=budget:return result
            # Several overlapping grants for one recipient must not consume the
            # batch before another recipient gets any assessment of that fact.
            # This prioritizes reviews only; every selected pair still requires
            # the same independent semantic scope and authority validation.
            for pair in ranked:
                key=(pair['candidate_id'],state['assignment'][pair['assignment_id']]['participant'])
                if key in recipients:continue
                result.append(pair);recipients.add(key)
                if len(result)>=budget:return result
            for pair in ranked:
                if pair not in result:result.append(pair)
                if len(result)>=budget:break
            return result
        chosen=diverse(fresh,min(8,limit) if old else limit)
        if old and len(chosen)<limit:
            oldest=min(old,key=lambda p:(order[p['candidate_id']],p['assignment_id']))
            chosen.append(oldest);old=[p for p in old if p!=oldest]
        if len(chosen)<limit:chosen.extend(diverse(old,limit-len(chosen)))
        if len(chosen)<limit:chosen.extend(p for p in diverse(fresh,limit) if p not in chosen)
        return chosen[:limit]


def router(updater,candidates,state,text=None):
    current=getattr(updater,'_routing_scratch',None)
    if current is None:
        requested=getattr(updater,'_review_priority',None)
        if text is None and requested:text=requested[1]
        current=Routing(updater,text or '')
        updater._routing_scratch=current
    current.prepare(candidates,state)
    return current
