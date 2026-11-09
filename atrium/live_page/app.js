// Atrium Live: draws what /events sends. Read-only; every word and colour comes
// from the data (atrium/config.json "live"), nothing is hardcoded here.
"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const colour = c => c && c.startsWith("#") ? c : `var(--${c})`;
let D = null, got = 0, es = null;
const ONCE = new URLSearchParams(location.search).has("once");  // one fetch, no stream (screenshots)

function nowH() { return D ? D.now_h + (Date.now() - got) / 3600000 : 0; }

function header() {
  const w = D.words, p = D.power;
  document.title = D.title;
  $("title").textContent = D.title;
  $("subtitle").textContent = D.subtitle;
  $("theme").textContent = w.theme;
  $("foot").textContent = w.foot;
  for (const h of document.querySelectorAll("[data-heading]")) h.textContent = D.headings[h.dataset.heading] ?? "";
  const icon = p.ac === true ? w.ac : p.ac === false ? w.battery : w.power_unknown;
  $("power").textContent = p.battery_percent == null ? icon : `· ${icon} ${p.battery_percent}%`;
  for (const a of D.agents) document.documentElement.style.setProperty(`--agent-${a.lane}`, a.colour);
  for (const el of document.querySelectorAll("[data-panel]"))
    el.hidden = !el.dataset.panel.split(" ").some(k => D.panels.includes(k));
}

function agents() {
  $("capacity").innerHTML = D.agents.map(a =>
    `<div class="cap" title="${esc(a.why)}"><span>${esc(a.label)}</span>` +
    `<div class="track"><i style="width:${a.fill}%;background:var(--agent-${esc(a.lane)})"></i></div>` +
    `<span class="${a.state === "bad" ? "bad" : ""}" title="${esc(a.status)}">${esc(a.status)}</span>` +
    `<span class="q">${esc(a.held || a.queued)}</span></div>`).join("");
}

function needs() {
  const n = D.needs;
  $("needs").innerHTML = n.length
    ? n.map(x => `<div class="needs" title="${esc(x.item)}"><b>${esc(x.icon)}</b><span>${esc(x.item)}</span></div>`).join("")
    : `<div class="needs none">${esc(D.words.nothing_needed)}</div>`;
  document.querySelector(".needs-card").classList.toggle("on", n.length > 0);
}

function timeline() {
  const c = D.timeline_cfg, now = nowH(), lanes = D.agents;
  const starts = D.timeline.map(b => b.start);
  const t0 = Math.max(0, Math.min(c.start_hour, Math.floor(Math.min(...starts, c.start_hour))));
  const t1 = Math.min(24, Math.max(c.end_hour, Math.ceil(now + c.pad_hours)));
  const X = h => 110 + (Math.min(Math.max(h, t0), t1) - t0) / (t1 - t0) * 870;
  const bottom = 18 + lanes.length * 38 - 6, H = bottom + 25;
  $("gantt").setAttribute("viewBox", `0 0 1000 ${H}`);
  let g = "";
  for (let h = t0; h <= t1; h += c.tick_hours)
    g += `<line x1="${X(h)}" x2="${X(h)}" y1="12" y2="${bottom}" stroke="var(--line)"/>` +
         `<text x="${X(h)}" y="${H - 6}" text-anchor="middle" font-size="9.5" fill="var(--faint)">${h}:00</text>`;
  lanes.forEach((a, i) => {
    const y = 18 + i * 38;
    g += `<text x="0" y="${y + 17}" font-size="10.5" fill="var(--muted)">${esc(a.label)}</text>` +
         `<rect x="110" y="${y}" width="870" height="26" rx="6" fill="var(--panel2)"/>`;
  });
  const row = Object.fromEntries(lanes.map((a, i) => [a.lane, i]));
  for (const b of D.timeline) {
    if (!(b.lane in row)) continue;
    const y = 18 + row[b.lane] * 38, end = b.end ?? now, w = Math.max(6, X(end) - X(b.start));
    const fill = b.cls === "run" ? `var(--agent-${esc(b.lane)})` : colour(b.cls);
    const mins = Math.round((end - b.start) * 60);
    const tip = `${b.task}${b.result ? " · " + b.result : ""} · ${D.words.elapsed.replace("{m}", mins)}`;
    g += `<rect class="${b.cls === "run" ? "run" : ""}" x="${X(b.start)}" y="${y + 3}" width="${w}" height="20" rx="5" fill="${fill}"><title>${esc(tip)}</title></rect>`;
  }
  g += `<line x1="${X(now)}" x2="${X(now)}" y1="12" y2="${bottom}" stroke="var(--text)" stroke-dasharray="3 3"/>` +
       `<text x="${X(now)}" y="9" text-anchor="middle" font-size="10" fill="var(--text)">${esc(D.words.now)}</text>`;
  $("gantt").innerHTML = g;
}

function progress() {
  $("progress").innerHTML = `<div class="prog">` + D.progress.map(p =>
    `<div class="cap"><span>${esc(p.label)}</span><div class="track"><i style="width:${p.pct}%;background:${colour(p.colour)}"></i></div>` +
    `<span>${esc(p.text)}</span></div>`).join("") + `</div>`;
}

function draw() { if (!D) return; header(); agents(); needs(); timeline(); progress(); }

function live(on) {
  $("dot").classList.toggle("off", !on);
  if (D) $("state").textContent = on ? D.words.live : D.words.stale;
}

function take(d) { if (!D || d.version !== D.version || d.day !== D.day) { D = d; got = Date.now(); draw(); } else { D.now_h = d.now_h; got = Date.now(); } live(true); }

function connect() {
  if (es || document.hidden) return;
  fetch("data").then(r => r.json()).then(take).catch(() => live(false));  // first paint without waiting on the stream
  if (ONCE) return;
  es = new EventSource("events");
  es.addEventListener("update", e => take(JSON.parse(e.data)));
  es.onopen = () => live(true);
  es.onerror = () => live(false);
}

// No tab visible: close the stream, so the laptop does no work for a page nobody sees.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { if (es) { es.close(); es = null; } } else connect();
});
// The now line and running blocks move with the clock; only while the tab is visible.
setInterval(() => { if (!document.hidden && D) timeline(); }, 60000);

const saved = localStorage.getItem("atrium-theme");
if (saved) document.documentElement.dataset.theme = saved;
$("theme").addEventListener("click", () => {
  const r = document.documentElement;
  r.dataset.theme = r.dataset.theme === "light" ? "dark" : "light";
  localStorage.setItem("atrium-theme", r.dataset.theme);
});
connect();
