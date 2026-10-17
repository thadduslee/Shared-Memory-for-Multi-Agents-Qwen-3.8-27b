// govmem demo front-end. Every action calls the real memory through the server; nothing here decides outcomes.
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

let S = { session: null, scenario: null, spec: null, state: null, step: 0, playing: false, busy: false,
          events: [], fresh: new Set(), feeds: {}, snapshots: [], gStep: 0, gLive: true, gPos: {}, gSel: null };

const VIEW_LABEL = { full: "full", redacted: "summary", existence: "exists only", none: "no access" };

function name(pid) {
  const p = (S.spec?.principals || []).find((x) => x.id === pid);
  return p ? p.display_name || p.id : pid;
}
function setStatus(t) { $("#status").textContent = t; }
function setBusy(b) {
  S.busy = b;
  document.querySelectorAll(".compose button, #step, #load, #play").forEach((x) => (x.disabled = b || (x.id !== "load" && !S.session)));
  $("#pause").disabled = !S.playing;
  if (!b && S.spec && S.step >= S.spec.steps.length) { $("#play").disabled = true; $("#step").disabled = true; }
}

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

// ------------------------------------------------------------------ setup
async function init() {
  const list = await api("/api/scenarios");
  $("#scenario").innerHTML = list.map((s) => `<option value="${esc(s.name)}">${esc(s.title)} (${s.steps} steps)</option>`).join("");
  $("#scenario").onchange = () => newMemory();
  $("#load").onclick = () => newMemory();
  $("#play").onclick = () => play();
  $("#step").onclick = () => runStep();
  $("#pause").onclick = () => { S.playing = false; setBusy(S.busy); setStatus("paused"); };
  document.querySelectorAll(".tabs button").forEach((b) => (b.onclick = () => {
    document.querySelectorAll(".tabs button, .tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active"); $("#tab-" + b.dataset.tab).classList.add("active");
    if (b.dataset.tab === "graph") renderGraph();
  }));
  setupGraph();
  await newMemory();
}

async function newMemory() {
  S.playing = false;
  setBusy(true);
  setStatus("starting a fresh memory…");
  const name_ = $("#scenario").value;
  S.spec = await api("/api/scenarios/" + name_);
  const r = await api("/api/session", { scenario: name_ });
  Object.assign(S, { session: r.session, scenario: name_, state: r.state, step: 0, events: [], fresh: new Set(), feeds: {},
                     snapshots: [], gStep: 0, gLive: true, gPos: {}, gSel: null });
  recordSnapshot("fresh memory", null, r.state);
  $("#scenario-desc").textContent = S.spec.description;
  renderParties();
  renderMemory();
  setStatus(`fresh memory · memory model: ${r.memory_model} · step 0/${S.spec.steps.length}`);
  setBusy(false);
}

// ------------------------------------------------------------------ actions (shared by Play and manual input)
async function send(speaker, action, text) {
  if (!text.trim()) return;
  setBusy(true);
  const before = new Set((S.state?.records || []).map((r) => r.id + ":" + r.status + ":" + JSON.stringify(r.views) + ":" + (r.note?.version || 0)));
  pushFeed(speaker, { cls: "me" + (action === "ask" ? " ask" : ""), text, meta: action === "ask" ? "asked my agent" : "shared" });
  setStatus(action === "ask" ? `${name(speaker)}'s agent is checking memory…` : `memory is reading ${name(speaker)}'s message…`);
  try {
    const r = await api(`/api/session/${S.session}/${action}`, { speaker, text });
    S.state = r.state;
    S.events.unshift(r.event);
    recordSnapshot(`${r.event.turn ? r.event.turn + " · " : ""}${name(speaker)} ${action === "ask" ? "asked" : "shared"}`, r.event, r.state);
    S.fresh = new Set(S.state.records.filter((x) => !before.has(x.id + ":" + x.status + ":" + JSON.stringify(x.views) + ":" + (x.note?.version || 0))).map((x) => x.id));
    if (action === "ask") {
      const e = r.event, c = e.counters || {};
      pushFeed(speaker, { cls: "agent", text: e.reply,
        meta: `memory suggested <b>${esc(e.suggested_action)}</b> · released ${e.evidence.length} record(s)` +
              (c.n_responsive_denied ? ` · ${c.n_responsive_denied} relevant record(s) withheld` : "") +
              (e.release_check ? ` · release check: ${esc(e.release_check)}` : "") +
              (e.evidence.length ? `<br>sources: ${e.evidence.map((x) => `<code>${esc(x.record_id)}</code>`).join(" ")}` : "") });
    } else {
      const e = r.event;
      const added = e.new_records.length, ops = e.ops.filter((o) => o.op !== "add");
      pushFeed(speaker, { cls: "agent", text: `Stored ${added} record(s)` + (ops.length ? `; ${ops.map((o) => `${o.op} ${o.outcome}`).join(", ")}` : "") + ".",
        meta: `read as <b>${esc(e.kind)}</b>${e.fallback ? " (fallback rules)" : ""} · ${e.seconds}s` });
    }
    renderMemory();
    setStatus(`step ${S.step}/${S.spec.steps.length} · ${S.state.model} memory-model calls so far`);
  } catch (err) {
    setStatus("error: " + err.message);
    S.playing = false;
  }
  setBusy(false);
}

async function runStep() {
  if (S.busy || !S.spec || S.step >= S.spec.steps.length) return;
  const st = S.spec.steps[S.step];
  S.step += 1;
  await send(st.speaker, st.action, st.text);
  if (S.step >= S.spec.steps.length) { S.playing = false; setStatus(`done · ${S.state.model} memory-model calls`); setBusy(false); }
}

async function play() {
  S.playing = true;
  setBusy(S.busy);
  while (S.playing && S.step < S.spec.steps.length) {
    await runStep();
    if (S.playing) await new Promise((r) => setTimeout(r, 900));
  }
  S.playing = false;
  setBusy(false);
}

// ------------------------------------------------------------------ parties
function renderParties() {
  const people = S.spec.principals;
  $("#parties").innerHTML = people.map((p) => `
    <div class="party" id="party-${esc(p.id)}">
      <h2>${esc(p.display_name)} <small>${esc(p.role)} · <code>${esc(p.id)}</code></small></h2>
      <div class="feed" id="feed-${esc(p.id)}"><div class="empty">Messages ${esc(p.display_name)} shares go into the shared memory. Questions go to ${esc(p.display_name)}'s own agent, which sees only what the gate releases to them.</div></div>
      <div class="compose">
        <textarea id="in-${esc(p.id)}" rows="2" placeholder="Type as ${esc(p.display_name)}…"></textarea>
        <div class="btns">
          <button data-pid="${esc(p.id)}" data-action="share">Share</button>
          <button data-pid="${esc(p.id)}" data-action="ask">Ask my agent</button>
        </div>
      </div>
    </div>`).join("");
  document.querySelectorAll(".compose button").forEach((b) => (b.onclick = async () => {
    const box = $("#in-" + b.dataset.pid);
    const text = box.value; box.value = "";
    await send(b.dataset.pid, b.dataset.action, text);
  }));
  setBusy(false);
}

function pushFeed(pid, item) {
  const feed = $("#feed-" + pid);
  if (!feed) return;
  if (feed.querySelector(".empty")) feed.innerHTML = "";
  const div = document.createElement("div");
  div.className = "bubble " + item.cls;
  div.innerHTML = `${esc(item.text)}<div class="meta">${item.meta || ""}</div>`;
  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
}

// ------------------------------------------------------------------ memory panel
function viewPill(v) { return `<span class="pill ${esc(v)}">${esc(VIEW_LABEL[v] || v)}</span>`; }

function renderMemory() {
  const st = S.state;
  if (!st) return;
  // records
  const recs = [...st.records].reverse();
  $("#tab-records").innerHTML = recs.length ? recs.map((r) => {
    const views = st.principals.map((p) => `${esc(p.display_name)} ${viewPill(r.views[p.id] || "none")}`).join(" ");
    const subj = Object.entries(r.subjects || {}).map(([k, owner]) => `${esc(name(k))}${owner ? " (owner)" : ""}`).join(", ");
    const note = r.note ? `<div class="note">note v${r.note.version}${r.note_versions > 1 ? ` (of ${r.note_versions})` : ""}:
        ${esc(r.note.context || "")} ${r.note.keywords?.length ? "· keywords: " + r.note.keywords.map(esc).join(", ") : ""}
        ${r.attributes?.length ? "<br>attributes: " + r.attributes.map((a) => `<code>${esc(a.name)}=${esc(a.value)}</code>`).join(" ") : ""}</div>` : "";
    return `<div class="rec ${esc(r.status)} ${S.fresh.has(r.id) ? "fresh" : ""}">
      <div class="top"><code>${esc(r.id)}</code><span class="pill ${esc(r.status)}">${esc(r.status)}</span>
        from ${esc(name(r.speaker))} at ${esc(r.turn)} · ${esc(r.kind)}${r.slot ? ` · slot <code>${esc(r.slot)}</code>` : ""}
        ${r.superseded_by ? ` · replaced by <code>${esc(r.superseded_by)}</code>` : ""}</div>
      <div class="text">${r.text === null ? "deleted: body and keys shredded" : esc(r.text)}</div>
      ${r.status !== "deleted" ? `<div class="aud">${views}</div>` : ""}
      ${subj ? `<div class="note">about: ${subj}</div>` : ""}${note}</div>`;
  }).join("") : `<p class="empty">No records yet. Share a message or press Play.</p>`;

  // access matrix
  const live = st.records.filter((r) => r.status !== "deleted");
  $("#tab-access").innerHTML = live.length ? `<table><tr><th>record</th><th>text</th>${st.principals.map((p) => `<th>${esc(p.display_name)}</th>`).join("")}</tr>
    ${live.map((r) => `<tr class="${S.fresh.has(r.id) ? "fresh" : ""}"><td><code>${esc(r.id)}</code><br><span class="pill ${esc(r.status)}">${esc(r.status)}</span></td>
      <td>${esc((r.text || "").slice(0, 90))}</td>${st.principals.map((p) => `<td class="cell">${viewPill(r.views[p.id] || "none")}</td>`).join("")}</tr>`).join("")}</table>
    ${st.denials.length ? `<h3>Standing denials</h3><ul>${st.denials.map((d) => `<li>${esc(name(d.principal_id))} denied on <code>${esc(d.target)}</code> since ${esc(d.source_turn)}</li>`).join("")}</ul>` : ""}
    <p class="footnote">A view is a key the record is sealed under. "summary" holds only the shareable version; "no access" holds no key at all.</p>`
    : `<p class="empty">Nothing stored yet.</p>`;

  // activity
  $("#tab-activity").innerHTML = S.events.length ? S.events.map(renderEvent).join("") : `<p class="empty">Nothing has happened yet.</p>`;

  // organisation
  const links = st.links, reorgs = st.reorganisations;
  const counts = {};
  reorgs.forEach((g) => { const k = g.op + " " + g.status; counts[k] = (counts[k] || 0) + 1; });
  $("#tab-organisation").innerHTML = `
    <p>As each message is stored, the organiser writes a note (keywords, context, typed attributes) for the new
      records, links them to related ones, and may update the notes of neighbours whose readers can all see the
      new information. Every change is logged with a status.</p>
    <h3>Links</h3>${links.length ? `<table><tr><th>from</th><th>to</th><th>kind</th><th>status</th></tr>${links.map((l) =>
      `<tr><td><code>${esc(l.a)}</code></td><td><code>${esc(l.b)}</code></td><td>${esc(l.kind)}</td><td><span class="pill ${esc(l.status)}">${esc(l.status)}</span></td></tr>`).join("")}</table>` : `<p class="empty">No links yet.</p>`}
    <h3>Clusters</h3>${Object.keys(st.clusters).length ? `<ul>${Object.entries(st.clusters).map(([c, m]) => `<li><code>${esc(c)}</code>: ${m.map((x) => `<code>${esc(x)}</code>`).join(" ")}</li>`).join("")}</ul>` : `<p class="empty">Clusters form at consolidation (every 10 turns).</p>`}
    <h3>Reorganisation ledger</h3>
    <p>${Object.entries(counts).map(([k, v]) => `<span class="pill ${esc(k.split(" ")[1])}">${esc(k)}: ${v}</span>`).join(" ") || '<span class="empty">empty</span>'}</p>
    ${reorgs.length ? `<table><tr><th>id</th><th>change</th><th>status</th><th>turn</th></tr>${reorgs.slice(0, 40).map((g) =>
      `<tr><td><code>${esc(g.id)}</code></td><td>${esc(g.op)} ${esc(JSON.stringify(g.args))}</td><td><span class="pill ${esc(g.status)}">${esc(g.status)}</span>${g.reason ? " " + esc(g.reason) : ""}</td><td>${esc(g.created_turn)}</td></tr>`).join("")}</table>` : ""}
    <p class="footnote">Deleted values remembered only as keyed fingerprints, to mask later repeats: ${st.forgotten_fingerprints}.</p>`;
  if ($("#tab-graph").classList.contains("active")) renderGraph();
}

function renderEvent(e) {
  if (e.type === "share") {
    const l = e.label || {};
    const quotes = (l.quotes || []).map((q) => `<li>“${esc(q.text)}” <span class="k">about</span> ${esc((q.about || []).map(name).join(", ") || "-")}
       ${q.slot ? ` <span class="k">slot</span> <code>${esc(q.slot)}</code>` : ""}</li>`).join("");
    const aud = l.audience || {};
    const ops = e.ops.map((o) => `<li><span class="pill ${esc(o.outcome)}">${esc(o.outcome)}</span> <b>${esc(o.op)}</b>
       ${o.reason ? `<span class="k">${esc(o.reason)}</span>` : ""} ${o.records.length ? o.records.map((x) => `<code>${esc(x)}</code>`).join(" ") : ""}
       ${o.args.principals ? ` <span class="k">for</span> ${esc(o.args.principals.map(name).join(", "))}` : ""}</li>`).join("");
    const org = e.organised.reduce((m, x) => { m[x.op + " " + x.status] = (m[x.op + " " + x.status] || 0) + 1; return m; }, {});
    return `<div class="event share"><h4>${esc(e.turn)} · ${esc(name(e.speaker))} shared · read as <b>${esc(e.kind)}</b>${e.fallback ? " (fallback)" : ""}</h4>
      <div>“${esc(e.text)}”</div>
      ${quotes ? `<div class="k">kept as verbatim quotes:</div><ul>${quotes}</ul>` : `<div class="k">nothing kept as a fact</div>`}
      <div class="k">audience the model reported: full ${esc(JSON.stringify(aud.full || []))}${(aud.redacted || []).length ? ", summary " + esc(JSON.stringify(aud.redacted)) : ""}
        ${(l.exclude || []).length ? ` · exclude ${esc(JSON.stringify(l.exclude))}` : ""}${l.rescope ? ` · re-scope ${esc(JSON.stringify(l.rescope))}` : ""}${l.remove ? ` · remove ${esc(JSON.stringify(l.remove))}` : ""}</div>
      <div class="k">what the core applied:</div><ul>${ops || "<li>nothing</li>"}</ul>
      ${Object.keys(org).length ? `<div class="k">organiser: ${Object.entries(org).map(([k, v]) => `${esc(k)} ×${v}`).join(", ")}</div>` : ""}</div>`;
  }
  if (e.type === "ask") {
    const c = e.counters || {};
    return `<div class="event ask"><h4>${esc(name(e.speaker))} asked their agent</h4><div>“${esc(e.text)}”</div>
      <div class="k">gate: ${c.n_candidates} candidate records, ${c.n_allowed} readable by ${esc(name(e.speaker))}, ${c.n_responsive} relevant
        (${c.n_responsive_full} full, ${c.n_responsive_redacted} summary, ${c.n_responsive_denied} withheld) · mode ${esc(c.mode)}</div>
      <div class="k">memory suggested <b>${esc(e.suggested_action)}</b>; released ${e.evidence.map((x) => `<code>${esc(x.record_id)}</code>`).join(" ") || "nothing"}</div>
      <div>agent: “${esc(e.reply)}”${e.release_check ? ` <span class="pill rejected">${esc(e.release_check)}</span>` : ""}</div></div>`;
  }
  return `<div class="event"><h4>consolidation</h4>${esc(JSON.stringify(e.stats))}</div>`;
}

init().catch((e) => setStatus("error: " + e.message));
