// Memory graph tab: draws the store's real state at any turn. Nodes are records (and the people and things they are
// about); edges are the organiser's links and supersessions; soft outlines are clusters. Nothing is laid out by script.
"use strict";

const LINK_FAMILY = { same_topic: "topic", part_of: "topic", refers_to: "topic",
                      follow_up: "update", same_value_slot: "update", contradicts: "conflict" };
const FAMILY_LABEL = { topic: "same thread (topic, part of, refers to)", update: "update or follow-up", conflict: "contradiction" };
const G = { W: 900, H: 520 };

function setupGraph() {
  $("#g-step").oninput = (e) => { S.gLive = false; S.gStep = +e.target.value; renderGraph(); };
  $("#g-live").onclick = () => { S.gLive = true; renderGraph(); };
  $("#g-people").onchange = () => renderGraph();
  $("#g-retracted").onchange = () => renderGraph();
  renderLegend();
}

function renderLegend() {
  const line = (cls) => `<svg width="30" height="10"><line class="edge ${cls}" x1="2" y1="5" x2="28" y2="5" style="stroke-width:2"></line></svg>`;
  $("#g-legend").innerHTML = `
    <span><svg width="14" height="14"><circle cx="7" cy="7" r="6" fill="var(--node)"></circle></svg>current record</span>
    <span><svg width="14" height="14"><circle cx="7" cy="7" r="5" fill="none" stroke="var(--node)" stroke-width="2"></circle></svg>superseded</span>
    <span><svg width="14" height="14"><path d="M3 3 L11 11 M11 3 L3 11" class="del"></path></svg>deleted (shredded)</span>
    <span><svg width="18" height="18"><circle cx="9" cy="9" r="5" fill="var(--node)"></circle><circle cx="9" cy="9" r="8" fill="none" stroke="var(--node)" stroke-width="1.5" opacity=".55"></circle></svg>note rewritten by a later turn</span>
    <span><svg width="18" height="18"><circle cx="9" cy="9" r="5" fill="var(--node)"></circle><circle cx="9" cy="9" r="8" fill="none" stroke="var(--halo)" stroke-width="3"></circle></svg>changed at this turn</span>
    <span>${line("k-topic")}${esc(FAMILY_LABEL.topic)}</span>
    <span>${line("k-update")}${esc(FAMILY_LABEL.update)}</span>
    <span>${line("k-conflict")}${esc(FAMILY_LABEL.conflict)}</span>
    <span><svg width="30" height="10"><line class="sup" x1="2" y1="5" x2="28" y2="5"></line></svg>replaced by (supersession)</span>
    <span><svg width="22" height="14"><rect x="1" y="1" width="20" height="12" rx="3" fill="none" stroke="var(--hub)" stroke-width="1.5"></rect></svg>person (dashed: declared thing)</span>
    <span><svg width="22" height="14"><rect x="1" y="1" width="20" height="12" rx="6" class="hull"></rect></svg>cluster</span>
    <span>faded edge: pending, not yet in effect</span>`;
}

function recordSnapshot(label, event, state) {
  S.snapshots.push({ label, event, state });
}

// ------------------------------------------------------------------ layout (persistent positions, short force runs)
function layout(nodes, edges) {
  const pos = S.gPos;
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
  let fresh = 0;
  for (const n of nodes) {
    if (pos[n.id]) continue;
    fresh++;
    const nb = edges.filter((e) => (e.s === n.id && pos[e.t]) || (e.t === n.id && pos[e.s])).map((e) => pos[e.s === n.id ? e.t : e.s]);
    const base = nb.length ? { x: nb.reduce((a, p) => a + p.x, 0) / nb.length, y: nb.reduce((a, p) => a + p.y, 0) / nb.length }
                           : { x: G.W / 2, y: G.H / 2 };
    pos[n.id] = { x: base.x + (Math.random() - 0.5) * 80, y: base.y + (Math.random() - 0.5) * 80 };
  }
  const ids = nodes.map((n) => n.id);
  const iters = fresh ? 220 : 40;
  for (let it = 0; it < iters; it++) {
    const cool = 1 - it / iters;
    const f = Object.fromEntries(ids.map((i) => [i, { x: 0, y: 0 }]));
    for (let a = 0; a < ids.length; a++) {
      for (let b = a + 1; b < ids.length; b++) {
        const p = pos[ids[a]], q = pos[ids[b]];
        let dx = p.x - q.x, dy = p.y - q.y, d2 = dx * dx + dy * dy + 0.01;
        const ha = byId[ids[a]].hub, hb = byId[ids[b]].hub;
        const k = (ha && hb ? 30000 : ha || hb ? 3000 : 1400) / d2;
        const d = Math.sqrt(d2);
        f[ids[a]].x += (dx / d) * k; f[ids[a]].y += (dy / d) * k;
        f[ids[b]].x -= (dx / d) * k; f[ids[b]].y -= (dy / d) * k;
      }
    }
    for (const e of edges) {
      const p = pos[e.s], q = pos[e.t];
      if (!p || !q) continue;
      const dx = q.x - p.x, dy = q.y - p.y, d = Math.sqrt(dx * dx + dy * dy) + 0.01;
      const k = e.w * (d - e.len) / d;
      f[e.s].x += dx * k; f[e.s].y += dy * k; f[e.t].x -= dx * k; f[e.t].y -= dy * k;
    }
    for (const i of ids) {
      const p = pos[i];
      f[i].x += (G.W / 2 - p.x) * 0.004; f[i].y += (G.H / 2 - p.y) * 0.006;
      const step = Math.min(18 * cool + 1, Math.hypot(f[i].x, f[i].y));
      const m = Math.hypot(f[i].x, f[i].y) || 1;
      const half = byId[i].hub ? byId[i].half : 16;
      p.x = Math.max(half + 6, Math.min(G.W - half - 6, p.x + (f[i].x / m) * step));
      p.y = Math.max(20, Math.min(G.H - 20, p.y + (f[i].y / m) * step));
    }
  }
}

function hullPath(points, pad) {
  const pts = [];
  for (const p of points) for (let a = 0; a < 12; a++) pts.push([p.x + pad * Math.cos(a * Math.PI / 6), p.y + pad * Math.sin(a * Math.PI / 6)]);
  pts.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower = [], upper = [];
  for (const p of pts) { while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop(); lower.push(p); }
  for (const p of pts.slice().reverse()) { while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop(); upper.push(p); }
  const h = lower.slice(0, -1).concat(upper.slice(0, -1));
  return "M" + h.map((p) => p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" L") + " Z";
}

// ------------------------------------------------------------------ diff between two snapshots
function diff(prev, cur) {
  const pr = Object.fromEntries((prev?.records || []).map((r) => [r.id, r]));
  const changed = new Set(), notes = [], status = [], added = [];
  for (const r of cur.records) {
    const p = pr[r.id];
    if (!p) { added.push(r.id); changed.add(r.id); continue; }
    if ((p.note?.version || 0) !== (r.note?.version || 0)) { notes.push(r.id); changed.add(r.id); }
    if (p.status !== r.status) { status.push([r.id, p.status, r.status]); changed.add(r.id); }
    if (JSON.stringify(p.views) !== JSON.stringify(r.views)) changed.add(r.id);
  }
  const key = (l) => `${l.a}|${l.b}|${l.kind}`;
  const pl = Object.fromEntries((prev?.links || []).map((l) => [key(l), l.status]));
  const newLinks = cur.links.filter((l) => !(key(l) in pl));
  const linkStatus = cur.links.filter((l) => key(l) in pl && pl[key(l)] !== l.status).map((l) => [l, pl[key(l)]]);
  const freshLinks = new Set(newLinks.concat(linkStatus.map((x) => x[0])).map(key));
  return { changed, notes, status, added, newLinks, linkStatus, freshLinks,
           clusters: [Object.keys(prev?.clusters || {}).length, Object.keys(cur.clusters).length] };
}

// ------------------------------------------------------------------ render
function renderGraph() {
  if (!S.snapshots?.length) return;
  const max = S.snapshots.length - 1;
  if (S.gLive) S.gStep = max;
  S.gStep = Math.min(S.gStep, max);
  const slider = $("#g-step"); slider.max = max; slider.value = S.gStep;
  const snap = S.snapshots[S.gStep], prev = S.snapshots[S.gStep - 1];
  const st = snap.state;
  $("#g-step-label").textContent = `${S.gStep}/${max} · ${snap.label}${S.gLive ? " · live" : ""}`;
  const d = diff(prev?.state, st);
  const showPeople = $("#g-people").checked, showRetracted = $("#g-retracted").checked;

  const nodes = st.records.map((r) => ({ id: r.id, rec: r }));
  const recIds = new Set(nodes.map((n) => n.id));
  const edges = [];
  const hubs = {};
  if (showPeople) {
    for (const r of st.records) for (const sid of Object.keys(r.subjects || {})) {
      if (!hubs[sid]) hubs[sid] = { id: "hub:" + sid, hub: true, sid, entity: !st.principals.some((p) => p.id === sid),
                                    half: Math.max(44, name(sid).length * 7 + 14) / 2 };
      edges.push({ s: r.id, t: "hub:" + sid, len: 120, w: 0.012, type: "about" });
    }
  }
  nodes.push(...Object.values(hubs));
  const links = st.links.filter((l) => recIds.has(l.a) && recIds.has(l.b) && (l.status !== "retracted" || showRetracted));
  for (const l of links) edges.push({ s: l.a, t: l.b, len: 70, w: l.status === "active" ? 0.03 : 0.01, type: "link", l });
  for (const r of st.records) if (r.superseded_by && recIds.has(r.superseded_by))
    edges.push({ s: r.id, t: r.superseded_by, len: 55, w: 0.04, type: "sup" });
  G.W = $("#g-svg").clientWidth || 900;
  layout(nodes, edges);
  const P = S.gPos;

  const svg = $("#g-svg");
  svg.setAttribute("viewBox", `0 0 ${G.W} ${G.H}`);
  const parts = [`<defs><marker id="g-arrow" viewBox="0 0 10 10" refX="16" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M0 0 L10 5 L0 10 z" fill="var(--node)"></path></marker></defs>`];
  for (const [cid, members] of Object.entries(st.clusters)) {
    const pts = members.filter((m) => P[m]).map((m) => P[m]);
    if (pts.length >= 2) parts.push(`<path class="hull" d="${hullPath(pts, 18)}"><title>${esc(cid)}: ${members.length} records</title></path>`);
  }
  for (const e of edges.filter((x) => x.type === "about")) {
    const p = P[e.s], q = P[e.t];
    parts.push(`<line class="about" x1="${p.x}" y1="${p.y}" x2="${q.x}" y2="${q.y}"></line>`);
  }
  for (const e of edges.filter((x) => x.type === "sup")) {
    const p = P[e.s], q = P[e.t];
    parts.push(`<line class="sup" x1="${p.x}" y1="${p.y}" x2="${q.x}" y2="${q.y}" marker-end="url(#g-arrow)"></line>`);
  }
  for (const e of edges.filter((x) => x.type === "link")) {
    const p = P[e.s], q = P[e.t], l = e.l;
    const fam = LINK_FAMILY[l.kind] || "topic";
    const fresh = d.freshLinks.has(`${l.a}|${l.b}|${l.kind}`) ? " fresh" : "";
    parts.push(`<line class="edge k-${fam} ${esc(l.status)}${fresh}" x1="${p.x}" y1="${p.y}" x2="${q.x}" y2="${q.y}"
      data-tip="${esc(`${l.a} ↔ ${l.b}: ${l.kind.replace(/_/g, " ")} (${l.status})`)}"></line>`);
    parts.push(`<line x1="${p.x}" y1="${p.y}" x2="${q.x}" y2="${q.y}" stroke="transparent" stroke-width="12"
      data-tip="${esc(`${l.a} ↔ ${l.b}: ${l.kind.replace(/_/g, " ")} (${l.status})`)}"></line>`);
  }
  for (const h of Object.values(hubs)) {
    const p = P[h.id], label = name(h.sid), w = Math.max(44, label.length * 7 + 14);
    parts.push(`<g class="hub ${h.entity ? "entity" : ""}" transform="translate(${p.x - w / 2},${p.y - 12})"
      data-tip="${esc((h.entity ? "declared thing: " : "person: ") + label)}"><rect width="${w}" height="24" rx="6"></rect>
      <text x="${w / 2}" y="16" text-anchor="middle">${esc(label)}</text></g>`);
  }
  for (const n of nodes.filter((x) => !x.hub)) {
    const r = n.rec, p = P[r.id];
    const tip = `${r.id} · ${r.status} · from ${name(r.speaker)} at ${r.turn}` + (r.text ? `\n${r.text.slice(0, 140)}` : "") +
                (r.note?.context ? `\nnote v${r.note.version}: ${r.note.context.slice(0, 120)}` : "");
    if (r.status === "deleted") {
      parts.push(`<g class="rec deleted" data-id="${esc(r.id)}" data-tip="${esc(tip)}"><path class="del" d="M${p.x - 5} ${p.y - 5} L${p.x + 5} ${p.y + 5} M${p.x + 5} ${p.y - 5} L${p.x - 5} ${p.y + 5}"></path>
        <circle cx="${p.x}" cy="${p.y}" r="10" fill="transparent"></circle></g>`);
      continue;
    }
    const sel = S.gSel === r.id ? " selected" : "";
    parts.push(`<g class="rec ${esc(r.status)}${sel}" data-id="${esc(r.id)}" data-tip="${esc(tip)}">
      ${d.changed.has(r.id) ? `<circle class="halo" cx="${p.x}" cy="${p.y}" r="12"></circle>` : ""}
      ${(r.note_versions || 0) > 1 ? `<circle class="evolved" cx="${p.x}" cy="${p.y}" r="10"></circle>` : ""}
      <circle class="dot" cx="${p.x}" cy="${p.y}" r="${r.status === "current" ? 7 : 6}"></circle>
      <circle cx="${p.x}" cy="${p.y}" r="13" fill="transparent"></circle>
      ${st.records.length <= 100 || d.changed.has(r.id) || sel ? `<text class="rid" x="${p.x + 10}" y="${p.y - 9}">${esc(r.id.replace(/^r0*/, "r"))}</text>` : ""}</g>`);
  }
  svg.innerHTML = parts.join("");
  bindTips(svg);
  svg.querySelectorAll(".rec").forEach((g) => (g.onclick = () => { S.gSel = g.dataset.id; renderGraph(); }));
  renderChanges(snap, d);
  renderDetail(st);
  renderGrowth();
}

function bindTips(svg) {
  const tip = $("#g-tip"), wrap = svg.parentElement;
  svg.querySelectorAll("[data-tip]").forEach((el) => {
    el.onmousemove = (ev) => {
      const b = wrap.getBoundingClientRect();
      tip.hidden = false;
      tip.innerHTML = esc(el.dataset.tip).replace(/\n/g, "<br>");
      tip.style.left = Math.min(ev.clientX - b.left + 14, b.width - 310) + "px";
      tip.style.top = ev.clientY - b.top + 14 + "px";
    };
    el.onmouseleave = () => (tip.hidden = true);
  });
}

function renderChanges(snap, d) {
  const e = snap.event, items = [];
  if (!e) { $("#g-changes").innerHTML = `<p class="empty">Fresh memory: only the declared people and things exist.</p>`; return; }
  if (e.type === "share") items.push(`<b>${esc(name(e.speaker))}</b> shared “${esc(e.text.slice(0, 120))}”, read as <b>${esc(e.kind)}</b>`);
  else if (e.type === "ask") items.push(`<b>${esc(name(e.speaker))}</b> asked “${esc(e.text.slice(0, 120))}”. A question changes no structure.`);
  else items.push("consolidation ran");
  if (d.added.length) items.push(`new records ${d.added.map((x) => `<code>${esc(x)}</code>`).join(" ")}`);
  for (const [id, a, b] of d.status) items.push(`<code>${esc(id)}</code> went from ${esc(a)} to <b>${esc(b)}</b>`);
  if (d.notes.length) items.push(`notes written or rewritten: ${d.notes.map((x) => `<code>${esc(x)}</code>`).join(" ")}`);
  if (d.newLinks.length) items.push(`links added: ${d.newLinks.map((l) => `<code>${esc(l.a)}</code>–<code>${esc(l.b)}</code> ${esc(l.kind.replace(/_/g, " "))} (${esc(l.status)})`).join("; ")}`);
  for (const [l, was] of d.linkStatus) items.push(`link <code>${esc(l.a)}</code>–<code>${esc(l.b)}</code> ${esc(was)} → <b>${esc(l.status)}</b>`);
  if (d.clusters[0] !== d.clusters[1]) items.push(`clusters: ${d.clusters[0]} → ${d.clusters[1]}`);
  if (e.ops) for (const o of e.ops.filter((o) => o.op !== "add" && o.op !== "annotate" && o.op !== "link"))
    items.push(`core <b>${esc(o.op)}</b> ${esc(o.outcome)}${o.reason ? " (" + esc(o.reason) + ")" : ""}`);
  $("#g-changes").innerHTML = `<ul>${items.map((x) => `<li>${x}</li>`).join("")}</ul>`;
}

function renderDetail(st) {
  const r = st.records.find((x) => x.id === S.gSel);
  if (!r) { $("#g-detail").innerHTML = `<p class="empty">Click a record in the graph.</p>`; return; }
  $("#g-detail-title").textContent = `Record ${r.id}`;
  const links = st.links.filter((l) => l.a === r.id || l.b === r.id);
  const views = st.principals.map((p) => `${esc(p.display_name)} ${viewPill(r.views[p.id] || "none")}`).join(" ");
  $("#g-detail").innerHTML = `<p><span class="pill ${esc(r.status)}">${esc(r.status)}</span> from ${esc(name(r.speaker))} at ${esc(r.turn)}
      ${r.slot ? ` · slot <code>${esc(r.slot)}</code>` : ""}${r.superseded_by ? ` · replaced by <code>${esc(r.superseded_by)}</code>` : ""}</p>
    <p>${r.text === null ? "<i>deleted: body and keys shredded</i>" : esc(r.text)}</p>
    ${r.note ? `<p class="k">note v${r.note.version} of ${r.note_versions}: ${esc(r.note.context || "")}<br>keywords: ${(r.note.keywords || []).map(esc).join(", ")}
      ${r.note.tags?.length ? `<br>tags: ${r.note.tags.map(esc).join(", ")}` : ""}</p>` : ""}
    ${r.attributes?.length ? `<p class="k">attributes: ${r.attributes.map((a) => `<code>${esc(a.name)}=${esc(a.value)}</code>`).join(" ")}</p>` : ""}
    ${links.length ? `<p class="k">links:</p><ul>${links.map((l) => `<li><code>${esc(l.a === r.id ? l.b : l.a)}</code> ${esc(l.kind.replace(/_/g, " "))} <span class="pill ${esc(l.status)}">${esc(l.status)}</span></li>`).join("")}</ul>` : ""}
    ${r.status !== "deleted" ? `<div class="aud">${views}</div>` : ""}`;
}

function renderGrowth() {
  const snaps = S.snapshots;
  const series = [
    ["current records", (s) => s.records.filter((r) => r.status === "current").length],
    ["active links", (s) => s.links.filter((l) => l.status === "active").length],
    ["clusters", (s) => Object.keys(s.clusters).length],
    ["note rewrites by later turns", (s) => s.records.reduce((a, r) => a + Math.max(0, (r.note_versions || 0) - 1), 0)],
    ["superseded or deleted", (s) => s.records.filter((r) => r.status !== "current").length],
  ];
  $("#g-growth").innerHTML = series.map(([label, fn], i) => {
    const ys = snaps.map((s) => fn(s.state));
    const W = 200, H = 60, maxY = Math.max(1, ...ys), n = Math.max(1, ys.length - 1);
    const X = (k) => 4 + (k / n) * (W - 8), Y = (v) => H - 6 - (v / maxY) * (H - 14);
    const path = ys.map((v, k) => `${k ? "L" : "M"}${X(k).toFixed(1)} ${Y(v).toFixed(1)}`).join(" ");
    const sel = S.gStep;
    return `<div class="g-mini" data-i="${i}"><div class="t">${esc(label)}</div><div class="v">${ys[sel] ?? 0}</div>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" data-ys="${ys.join(",")}">
        <line class="base" x1="4" y1="${H - 6}" x2="${W - 4}" y2="${H - 6}"></line>
        <path class="ln" d="${path}"></path>
        <line class="cur" x1="${X(sel)}" y1="4" x2="${X(sel)}" y2="${H - 6}"></line>
        <circle class="mk" cx="${X(sel)}" cy="${Y(ys[sel] ?? 0)}" r="4"></circle></svg></div>`;
  }).join("");
  document.querySelectorAll(".g-mini svg").forEach((svg) => {
    const ys = svg.dataset.ys.split(",").map(Number);
    svg.onmousemove = (ev) => {
      const b = svg.getBoundingClientRect(), k = Math.round(((ev.clientX - b.left) / b.width) * (ys.length - 1));
      svg.parentElement.querySelector(".v").textContent = `${ys[Math.max(0, Math.min(ys.length - 1, k))]}  (turn ${Math.max(0, Math.min(ys.length - 1, k))})`;
    };
    svg.onmouseleave = () => (svg.parentElement.querySelector(".v").textContent = ys[S.gStep] ?? 0);
    svg.onclick = (ev) => {
      const b = svg.getBoundingClientRect();
      S.gLive = false; S.gStep = Math.max(0, Math.min(ys.length - 1, Math.round(((ev.clientX - b.left) / b.width) * (ys.length - 1))));
      renderGraph();
    };
  });
}
