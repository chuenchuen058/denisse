// Atrium Live: draws what /events sends. Read-only; every word and colour comes
// from the data (atrium/config.json "live"), nothing is hardcoded here.
"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const colour = c => c && c.startsWith("#") ? c : `var(--${c})`;
let D = null, got = 0, es = null, still = true;  // still: next placement jumps, no glide
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

// -- agent cursors: one per running task, glides only when its data moves it -----
const ARROW = `<svg viewBox="0 0 10 14" aria-hidden="true"><path d="M0.5 0.5V12.5L3.8 9.4L6 13.6L8 12.7L5.8 8.6H10Z"/></svg>`;
const cur = (k, cls) => `<span class="cur ${cls}" data-seat="${esc(k.id)}" style="--c:var(--agent-${esc(k.lane)})">${ARROW}<b>${esc(k.label)}</b></span>`;

function place(listId, rowsHtml, want) {
  // want: cursor id -> true for every cursor that has a seat in rowsHtml
  const box = $(listId), layer = box.querySelector(".layer");
  box.querySelector(".rows").innerHTML = rowsHtml;
  const byId = Object.fromEntries((D.where.cursors).map(k => [k.id, k]));
  for (const el of [...layer.children]) if (!want[el.dataset.seat]) el.remove();
  if (still) layer.classList.add("still");
  for (const id of Object.keys(want)) {
    const seat = box.querySelector(`.rows .seat[data-seat="${CSS.escape(id)}"]`);
    let el = layer.querySelector(`[data-seat="${CSS.escape(id)}"]`);
    if (!seat) { if (el) el.remove(); continue; }
    if (!el) {
      layer.insertAdjacentHTML("beforeend", cur(byId[id], "fly"));
      el = layer.lastElementChild;
      layer.classList.add("still");  // a new cursor appears where it is, it does not fly in
    }
    el.querySelector("b").textContent = byId[id].label;
    el.style.transform = `translate(${seat.offsetLeft}px, ${seat.offsetTop}px)`;
  }
  void layer.offsetWidth;  // apply the jump before the transition comes back
  layer.classList.remove("still");
}

function where() {
  const W = D.where, w = D.words, r = document.documentElement.style;
  r.setProperty("--cur-arrow", W.cursor_cfg.arrow_px + "px");
  r.setProperty("--cur-tag", W.cursor_cfg.tag_px + "px");
  r.setProperty("--cur-row", W.cursor_cfg.row_px + "px");
  r.setProperty("--cur-glide", W.cursor_cfg.glide_ms + "ms");
  const byId = Object.fromEntries(W.cursors.map(k => [k.id, k]));
  const onTask = Object.fromEntries(W.cursors.map(k => [k.id, true]));
  place("where-tasks", W.cursors.length
    ? W.cursors.map(k => `<div class="row" title="${esc(k.task)}">${cur(k, "seat")}<span class="t">${esc(k.task)}</span></div>`).join("")
    : `<div class="none">${esc(w.nobody_working)}</div>`, onTask);
  const warn = Object.fromEntries(W.overlaps.map(o => [o.path, o.text]));
  const onFile = {};
  const rows = W.files.map(f => {
    const marks = f.who.map(id => {
      const k = byId[id];
      if (k.file === f.path) { onFile[id] = true; return cur(k, "seat"); }
      return cur(k, "mini");
    }).join("");
    return `<div class="row${f.shared ? " shared" : ""}" title="${esc(f.path)}">${marks}<span class="t">${esc(f.path)}</span></div>` +
           (f.shared ? `<div class="warn">${esc(W.icon)} ${esc(warn[f.path])}</div>` : "");
  }).join("");
  place("where-files", W.files.length
    ? rows + (W.more_files ? `<div class="more">${esc(W.more_files)}</div>` : "")
    : `<div class="none">${esc(W.cursors.length ? w.no_files : w.nobody_working)}</div>`, onFile);
  still = false;
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

function markets() {
  const rows = D.markets || [];
  if (!rows.length) {
    $("markets").innerHTML = `<div class="needs none">${esc(D.words.nothing_needed || "No markets yet")}</div>`;
    return;
  }
  $("markets").innerHTML = rows.map(r => {
    const pct = Number(r.day_change_pct ?? 0);
    const color = pct >= 0 ? "var(--ok)" : "var(--warn)";
    const spark = (r.sparkline || []).map(v => Math.max(0, Math.min(1, Number(v) || 0))).join(",");
    return `<div class="needs" title="${esc(r.ticker || "ticker")}"><b>${esc(r.ticker || "?")}</b><span>${esc(String(r.price ?? ""))}</span><span style="color:${color}">${esc(`${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`)}</span><span>spark:${esc(spark)}</span></div>`;
  }).join("");
}

function draw() { if (!D) return; header(); agents(); needs(); where(); timeline(); progress(); markets(); }

function live(on) {
  $("dot").classList.toggle("off", !on);
  if (D) $("state").textContent = on ? D.words.live : D.words.stale;
}

function take(d) { if (!D || d.version !== D.version || d.day !== D.day) { D = d; got = Date.now(); draw(); } else { D.now_h = d.now_h; got = Date.now(); } still = false; live(true); }

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
  if (document.hidden) { if (es) { es.close(); es = null; } } else { still = true; connect(); }
});
// A new width moves the rows: put the cursors back where they belong, without a glide.
addEventListener("resize", () => { if (D) { still = true; where(); } });
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
