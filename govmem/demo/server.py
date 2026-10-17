"""govmem demo: two people talk to their agents over one governed shared memory.

Every message goes through the real library: the interpreter labels it, the core authorises and applies the
operations, the organiser reorganises, the retriever gates what each agent may see, and the answerer replies
from released evidence only. Nothing about outcomes is scripted: the example conversations are just messages.

    cd govmem && source scripts/env_soclaas.sh
    ~/miniconda3/envs/gsm/bin/python -m uvicorn demo.server:app --port 8765
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from govmem.api import Memory, Relationship
from govmem.config import Settings
from govmem.interpret.interpreter import Turn
from govmem.llm import ChatClient
from govmem.release.check import check_release

HERE = Path(__file__).parent
SCENARIOS = HERE / "scenarios"
STATIC = HERE / "static"

AGENT_SYSTEM = """You are the personal assistant of {name} in a shared workspace with a governed memory.
Below are the only memories released to {name}; each shows who said it and when. Answer {name}'s message
using only these memories. If they do not contain what is asked, say plainly that you do not have that
information available to {name}. Never guess or invent details. Reply in one or two short sentences."""

app = FastAPI(title="govmem demo")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class Session:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.id = uuid.uuid4().hex[:10]
        self.dir = Path(tempfile.mkdtemp(prefix="govmem_demo_"))
        self.spec = spec
        settings = Settings.from_env()
        self.mem = Memory(str(self.dir / "memory.db"), settings, cache_path=self.dir / "cache.sqlite")
        self.mem.declare(spec["principals"], entities=spec.get("entities", []),
                         relationships=[Relationship(r["type"], r["endpoints"], r.get("attributes", {}))
                                        for r in spec.get("relationships", [])])
        answer_ep = settings.answer
        answer_ep.thinking = os.environ.get("GOVMEM_DEMO_THINKING", "0") == "1"
        answer_ep.max_tokens = 1024 if not answer_ep.thinking else 8192
        self.agent = ChatClient(answer_ep) if answer_ep.configured else None
        self.names = {p["id"]: p.get("display_name") or p["id"] for p in spec["principals"]}
        self.n = 0
        self.clock = datetime(2026, 3, 2, 9, 0)
        self.lock = threading.Lock()
        self.history: list[dict[str, Any]] = []
        self.created = time.time()

    def next_turn(self) -> tuple[str, str]:
        self.n += 1
        self.clock += timedelta(minutes=7)
        return f"t{self.n:03d}", self.clock.strftime("%Y-%m-%d %H:%M")


SESSIONS: dict[str, Session] = {}


def _scenario(name: str) -> dict[str, Any]:
    p = SCENARIOS / f"{name}.json"
    if not p.exists():
        raise HTTPException(404, f"no scenario {name}")
    return json.loads(p.read_text())


def _session(sid: str) -> Session:
    s = SESSIONS.get(sid)
    if s is None:
        raise HTTPException(404, "session expired; reload")
    return s


# ------------------------------------------------------------------ state, read as the store's owner (demo view)
def state(s: Session) -> dict[str, Any]:
    st = s.mem.store
    recs = []
    for r in st.records(current_only=False, alive_only=False):
        status = "deleted" if r.tombstoned_at else "superseded" if r.superseded_by else "current"
        note = st.active_note(r.id) if not r.tombstoned_at else None
        recs.append({
            "id": r.id, "speaker": r.speaker_id, "turn": r.source_turn, "kind": r.kind, "slot": r.slot,
            "status": status, "superseded_by": r.superseded_by,
            "text": None if r.tombstoned_at else st.text_owner(r.id),
            "views": st.views(r.id), "subjects": st.subjects(r.id),
            "redacted": bool(r.has_redacted),
            "note": note and {k: note[k] for k in ("version", "keywords", "tags", "context")},
            "note_versions": st.q("SELECT COUNT(*) FROM notes WHERE record_id=?", (r.id,))[0][0],
            "attributes": [] if r.tombstoned_at else [{k: a[k] for k in ("name", "type", "value")}
                                                      for a in st.attributes(r.id)],
        })
    links = [dict(x) for x in st.q("SELECT a, b, kind, status FROM links ORDER BY id")]
    clusters: dict[str, list[str]] = {}
    for x in st.q("SELECT cluster_id, record_id FROM cluster_members WHERE status='active' ORDER BY cluster_id"):
        clusters.setdefault(x["cluster_id"], []).append(x["record_id"])
    reorgs = [dict(x) | {"args": json.loads(x["args"] or "{}")} for x in
              st.q("SELECT id, op, args, status, created_turn, reason FROM _reorgs ORDER BY id DESC LIMIT 80")]
    denials = [dict(x) for x in st.q("SELECT principal_id, target, source_turn FROM _denials")]
    forgotten = st.q("SELECT COUNT(*) FROM _forgotten")[0][0]
    return {"records": recs, "links": links, "clusters": clusters, "reorganisations": reorgs, "denials": denials,
            "forgotten_fingerprints": forgotten, "principals": s.spec["principals"],
            "model": s.mem.model.calls, "tokens": s.mem.model.tokens}


def _ops(results: list[Any]) -> list[dict[str, Any]]:
    out = []
    for r in results:
        args = {k: v for k, v in r.op.args.items() if k in ("records", "principals", "view", "old", "new", "a", "b",
                                                              "kind", "record", "scope", "slot")}
        out.append({"op": r.op.op, "outcome": r.outcome, "reason": r.reason, "authority": r.op.authority,
                    "origin": r.op.origin, "records": r.record_ids, "args": args})
    return out


# ------------------------------------------------------------------ API
class NewSession(BaseModel):
    scenario: str = "consent"


class Message(BaseModel):
    speaker: str
    text: str


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/scenarios")
def scenarios() -> list[dict[str, Any]]:
    out = []
    for p in sorted(SCENARIOS.glob("*.json")):
        d = json.loads(p.read_text())
        out.append({"name": p.stem, "title": d["title"], "description": d["description"], "steps": len(d["steps"])})
    return out


@app.get("/api/scenarios/{name}")
def scenario(name: str) -> dict[str, Any]:
    return _scenario(name)


@app.post("/api/session")
def new_session(req: NewSession) -> dict[str, Any]:
    for sid in [k for k, v in SESSIONS.items() if time.time() - v.created > 3600]:
        shutil.rmtree(SESSIONS.pop(sid).dir, ignore_errors=True)
    s = Session(_scenario(req.scenario))
    SESSIONS[s.id] = s
    return {"session": s.id, "scenario": req.scenario, "state": state(s),
            "memory_model": s.mem.cfg.memory.model if s.mem.model.enabled else "disabled (fallback rules)"}


@app.post("/api/session/{sid}/share")
def share(sid: str, msg: Message) -> dict[str, Any]:
    s = _session(sid)
    with s.lock:
        tid, ts = s.next_turn()
        t0 = time.time()
        out = s.mem.ingest(Turn(tid, ts, msg.speaker, msg.text))
        organise = [x for x in s.mem.store.q("SELECT op, status, args FROM _reorgs WHERE created_turn=? AND op IN "
                                             "('annotate','link','cluster')", (tid,))]
        event = {"type": "share", "turn": tid, "time": ts, "speaker": msg.speaker, "text": msg.text,
                 "kind": out.kind, "fallback": out.fallback, "label": out.label, "ops": _ops(out.results),
                 "new_records": out.new_records, "seconds": round(time.time() - t0, 1),
                 "organised": [{"op": x["op"], "status": x["status"], "args": json.loads(x["args"])} for x in organise]}
        s.history.append(event)
        return {"event": event, "state": state(s)}


@app.post("/api/session/{sid}/ask")
def ask(sid: str, msg: Message) -> dict[str, Any]:
    s = _session(sid)
    with s.lock:
        t0 = time.time()
        ev = s.mem.query(msg.speaker, msg.text)
        evidence = [{"record_id": i["record_id"], "speaker": i["principal_id"], "text": i["text"], "view": i["view"]}
                    for i in ev.items]
        name = s.names.get(msg.speaker, msg.speaker)
        reply, reason = "", ""
        if s.agent is None:
            reply = ("No answer model is configured, so here is what the gate released to " + name + ": "
                     + (" | ".join(e["text"] for e in evidence) if evidence else "nothing") + ".")
        elif not evidence:
            reply = {"refuse": f"I can't share that with {name}: it isn't released to you.",
                     "no_memory": "I don't have that information in memory."}.get(ev.action, "I don't have that.")
        else:
            block = "\n".join(f"- ({s.names.get(e['speaker'], e['speaker'])}) {e['text']}" for e in evidence)
            res = s.agent.chat([{"role": "system", "content": AGENT_SYSTEM.format(name=name)},
                                {"role": "user", "content": f"MEMORIES RELEASED TO {name}\n{block}\n\n"
                                                            f"{name.upper()} ASKS: {msg.text}"}],
                               temperature=0.0, tag="demo_agent")
            reply = res.text.strip() if res.ok else f"(answerer error: {res.error})"
            checked, reason = check_release(s.mem.store, {"action": "answer", "answer": reply},
                                            [e["record_id"] for e in evidence], [e["text"] for e in evidence])
            reply = checked["answer"]
        event = {"type": "ask", "speaker": msg.speaker, "text": msg.text, "suggested_action": ev.action,
                 "counters": ev.counters, "evidence": evidence, "reply": reply, "release_check": reason,
                 "seconds": round(time.time() - t0, 1)}
        s.history.append(event)
        return {"event": event, "state": state(s)}


@app.post("/api/session/{sid}/consolidate")
def consolidate(sid: str) -> dict[str, Any]:
    s = _session(sid)
    with s.lock:
        stats = s.mem.organiser.consolidate(f"t{s.n:03d}", s.mem.store.last_seq())
        event = {"type": "consolidate", "stats": stats}
        s.history.append(event)
        return {"event": event, "state": state(s)}


@app.get("/api/session/{sid}/state")
def get_state(sid: str) -> dict[str, Any]:
    return state(_session(sid))
