"""Validate saved t008 attempts read-only, without model calls or publication."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import sqlite3
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agentic_memory.privacy.permissions import PermissionUpdater,PermissionBootstrap,PermissionProposalRejected
from agentic_memory.privacy.permission_context import context_view

folder=Path(__file__).parent
with sqlite3.connect('file:'+str(folder/'memory.sqlite')+'?mode=ro',uri=True) as db:
 db.row_factory=sqlite3.Row
 db.execute('ATTACH DATABASE ? AS privacy',('file:'+str(folder/'memory.privacy.sqlite')+'?mode=ro',))
 db.execute('PRAGMA query_only=ON')
 job=db.execute("SELECT * FROM permission_jobs WHERE status='rejected'").fetchone()
 bootstrap=json.loads(db.execute('SELECT body FROM permission_bootstrap WHERE compartment=?',(job['compartment'],)).fetchone()[0])
 updater=object.__new__(PermissionUpdater)
 updater.core=SimpleNamespace(db=db,snapshot_read=lambda key,load:load())
 updater.compartment=job['compartment'];updater.version='10';updater.bootstrap=PermissionBootstrap(**bootstrap)
 updater.processor=SimpleNamespace(require_permission_coverage=True)
 sources={sid:json.loads(db.execute('SELECT payload FROM resources WHERE id=?',(sid,)).fetchone()[0]) for sid in json.loads(job['inputs'])}
 state=updater._state();loaded,records,_=context_view(sources,state,job['source_id'])
 outcomes=[]
 for row in db.execute('SELECT * FROM permission_attempts WHERE job_id=? ORDER BY attempt',(job['id'],)):
  proposal=json.loads(row['proposal']);value=deepcopy(proposal)
  try:
   updater._validate(value,state,sources,job['revision'],new_source_id=job['source_id'],
                     available_source_ids=set(loaded),available_records=records)
  except PermissionProposalRejected as exc:
   outcomes.append({'attempt':row['attempt'],'accepted':False,'reason':str(exc),'details':exc.details})
  else:
   outcomes.append({'attempt':row['attempt'],'accepted':True,
                    'operation_types_unchanged':[o['op'] for o in value['operations']]==[o['op'] for o in proposal['operations']]})
 assert [r['accepted'] for r in outcomes]==[False,True,True]
 print(json.dumps({'source':'t008','outcomes':outcomes,'model_calls':0,'run_databases_modified':False},indent=2))
