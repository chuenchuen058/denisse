// Atrium hub (v3 mission board): shows what /events sends. Read-only. atrium/board.py renders
// every section from real data; this file only puts that HTML on screen and adds what moves
// with the clock: the date and time, the lane timers, the gliding agent pointers, the favicon
// and the theme; and what answers the reader: a lane card opens in place, a task row opens the
// drawer, the filter's pill slides, changed numbers run to their new value, a few keys work.
// Every word, colour, selector, key and duration comes from the data (atrium/config.json "live").
"use strict";
const $ = id => document.getElementById(id);
const root = document.documentElement;
let D = null, got = 0, es = null, still = true, online = false;  // still: next placement jumps, no glide
const ONCE = new URLSearchParams(location.search).has("once");  // one fetch, no stream (screenshots)
const calm = matchMedia("(prefers-reduced-motion: reduce)");  // then nothing animates (the CSS agrees)
const opened = new Set();  // lane cards the reader opened, by card key: kept across updates
let seg = null, query = "", shown = null, back = null;  // the filter's segment and text; the drawer's task

function nowH() { return D ? D.now_h + (Date.now() - got) / 3600000 : 0; }
const fill = (s, f) => s.replace(/\{(\w+)\}/g, (m, k) => k in f ? f[k] : m);

// -- theme: the saved choice, else the system's; colours are the config's -------------
function scheme() {
  let saved = null;
  try { saved = D && localStorage.getItem(D.board.theme_storage_key); } catch (e) {}
  if (saved && D.board.theme[saved]) return saved;
  if (D && D.board.theme_default && D.board.theme[D.board.theme_default]) return D.board.theme_default;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function theme() {
  const name = scheme(), vars = D.board.theme[name] || {};
  root.dataset.theme = name;
  // a variable only one theme sets (bg-image, blur...) must not linger after switching away
  for (const t of Object.values(D.board.theme)) for (const k of Object.keys(t)) root.style.removeProperty(`--${k}`);
  for (const [k, v] of Object.entries(vars)) root.style.setProperty(`--${k}`, v);
}

// -- favicon: one arc per agent in its colour, the centre dot in the verdict's --------
function favicon() {
  const f = D.board.favicon, n = f.arcs.length, c = f.size / 2;
  const at = i => { const a = 2 * Math.PI * i / n - Math.PI / 2; return `${c + f.radius * Math.cos(a)} ${c + f.radius * Math.sin(a)}`; };
  const arcs = f.arcs.map((col, i) =>
    `<path d="M${at(i)}A${f.radius} ${f.radius} 0 0 1 ${at(i + 1)}" stroke="${col}"/>`).join("");
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${f.size} ${f.size}"><defs>` +
    `<linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${f.bg_top}"/>` +
    `<stop offset="1" stop-color="${f.bg_bottom}"/></linearGradient></defs>` +
    `<rect width="${f.size}" height="${f.size}" rx="${f.corner}" fill="url(#g)"/>` +
    `<g fill="none" stroke-width="${f.stroke}" stroke-linecap="round">${arcs}</g>` +
    `<circle cx="${c}" cy="${c}" r="${f.dot_r}" fill="${f.dot}"/></svg>`;
  $("favicon").href = "data:image/svg+xml," + encodeURIComponent(svg);
}

// -- what moves with the clock -----------------------------------------------------
function clock() {
  const el = $("clock"), c = D.board.clock, d = new Date();
  if (el) el.textContent = d.toLocaleDateString(c.locale, c.date) + c.join + d.toLocaleTimeString(c.locale, c.time);
}
function timers() {
  const w = D.board.words, now = nowH();
  for (const el of document.querySelectorAll("[data-timer]")) {
    const s = Math.max(0, Math.round((now - Number(el.dataset.startH)) * 3600)), limit = Number(el.dataset.timeoutS);
    const elapsed = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    el.textContent = limit ? fill(w.timer_limit, {elapsed, limit: Math.round(limit / 60)}) : fill(w.timer, {elapsed});
    const bar = el.previousElementSibling;
    if (bar && limit) {
      bar.firstElementChild.style.width = Math.min(D.board.bar_full_pct, s / limit * 100) + "%";
      bar.classList.toggle("over", s > limit);
    }
  }
}

// -- agent cursors: one per running task, glides only when its data moves it -----------
function place() {
  const box = document.querySelector(".where");
  if (!box) return;
  const layer = box.querySelector(".layer"), want = {};
  for (const seat of box.querySelectorAll(".rows .cur.seat")) want[seat.dataset.seat] = seat;
  for (const el of [...layer.children]) if (!want[el.dataset.seat]) el.remove();
  if (still) layer.classList.add("still");
  for (const [id, seat] of Object.entries(want)) {
    let el = layer.querySelector(`[data-seat="${CSS.escape(id)}"]`);
    if (!el) {
      el = seat.cloneNode(true);
      el.className = "cur fly";
      layer.appendChild(el);
      layer.classList.add("still");  // a new cursor appears where it is, it does not fly in
    }
    el.querySelector("b").textContent = seat.querySelector("b").textContent;
    el.style.transform = `translate(${seat.offsetLeft}px, ${seat.offsetTop}px)`;
  }
  void layer.offsetWidth;  // apply the jump before the transition comes back
  layer.classList.remove("still");
}

// -- numbers run to their new value; rings sweep to theirs ------------------------------
const NUM = /\d+(?:\.\d+)?/g;
function tween(el, from, to) {
  const a = from.match(NUM), b = to.match(NUM), ms = D.board.ix.count.ms;
  // only when nothing but the numbers changed ("1 of 3" -> "2 of 3"), and motion is wanted
  if (ONCE || calm.matches || !a || !b || a.length !== b.length || from.replace(NUM, "0") !== to.replace(NUM, "0")) return;
  const t0 = performance.now();
  const step = t => {
    const p = Math.min(1, (t - t0) / ms), k = 1 - Math.pow(1 - p, 3);
    let i = 0;
    el.textContent = p === 1 ? to : to.replace(NUM, m => {
      const x = Number(a[i++]);
      return (x + (Number(m) - x) * k).toFixed((m.split(".")[1] || "").length);
    });
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
// New HTML into el; a counted number that changed runs from its old value. Nothing runs per tick.
function swap(el, html) {
  const sel = D.board.ix.count.selector, old = [...el.querySelectorAll(sel)].map(n => n.textContent);
  el.innerHTML = html;
  el.dataset.html = html;
  el.querySelectorAll(sel).forEach((n, i) => { if (old[i] !== undefined && old[i] !== n.textContent) tween(n, old[i], n.textContent); });
}
function rings(html) {
  const box = $("rings"), arc = r => r.querySelectorAll("circle")[1], was = {};
  if (box.dataset.html === html) return;
  for (const r of box.querySelectorAll("[data-ring]")) was[r.dataset.ring] = arc(r) ? getComputedStyle(arc(r)).strokeDasharray : "";
  swap(box, html);
  if (ONCE || calm.matches) return;
  for (const r of box.querySelectorAll("[data-ring]")) {
    const a = arc(r);
    if (!a) continue;
    const to = a.getAttribute("stroke-dasharray");
    a.style.strokeDasharray = was[r.dataset.ring] || `0 ${to.split(" ")[1]}`;  // from where it was, or empty
    void a.getBoundingClientRect();
    a.style.strokeDasharray = to;
  }
}

// -- the board -----------------------------------------------------------------------
function cards() {
  const grid = $("grid"), order = [];
  for (const c of D.board.cards) {
    let el = grid.querySelector(`:scope > [data-key="${CSS.escape(c.key)}"]`);
    if (!el) {
      el = document.createElement("div");
      el.dataset.key = c.key;
      el.dataset.section = c.section;
    }
    order.push(el);
    el.className = `w ${c.cls}`;
    el.style.cssText = c.style;
    if (c.rows !== undefined) {  // who's editing what: the pointer layer outlives every update
      if (!el.querySelector(".layer")) { el.innerHTML = c.html; el.dataset.html = c.rows; }
      el.querySelector("h3").textContent = c.heading;
      if (el.dataset.html !== c.rows) { el.querySelector(".rows").innerHTML = c.rows; el.dataset.html = c.rows; }
    } else if (el.dataset.html !== c.html) swap(el, c.html);
  }
  for (const el of [...grid.children]) if (!order.includes(el)) el.remove();
  // only touch the document order when it changed: moving a card would restart its pointers
  if (order.some((el, i) => grid.children[i] !== el)) grid.append(...order);
}

function draw() {
  if (!D) return;
  const b = D.board, k = b.cursor_cfg;
  document.title = b.title;
  theme();
  root.style.setProperty("--cur-arrow", k.arrow_px + "px");
  root.style.setProperty("--cur-tag", k.tag_px + "px");
  root.style.setProperty("--cur-row", k.row_px + "px");
  root.style.setProperty("--cur-glide", k.glide_ms + "ms");
  for (const [name, v] of Object.entries(b.ix.vars)) root.style.setProperty(`--ix-${name}`, v);
  $("top").innerHTML = b.header;
  rings(b.rings);
  $("foot").textContent = D.words.foot;
  const typing = document.activeElement && document.activeElement.id === "q";
  cards();
  dress(typing);
  favicon(); clock(); timers(); place(); live(online);
}

// -- what answers the reader -------------------------------------------------------------
// After every redraw: the opened cards stay open, the tooltips, the filter and its text are put back.
function dress(typing) {
  const ix = D.board.ix;
  for (const el of document.querySelectorAll(ix.expand)) {
    const on = opened.has(el.dataset.key);
    el.classList.toggle("open", on);
    el.tabIndex = 0;
    el.setAttribute("role", "button");
    el.setAttribute("aria-expanded", on);
  }
  for (const t of ix.tips) for (const el of document.querySelectorAll(t.selector)) el.dataset.tip = t.text;
  if (seg === null) { try { seg = localStorage.getItem(ix.filter.storage_key); } catch (e) {} seg = seg || ix.filter.default; }
  const q = $("q");
  if (q) { q.value = query; if (typing) q.focus(); }
  sift(false);
}
function toggle(card) {
  const key = card.dataset.key;
  if (opened.has(key)) opened.delete(key); else opened.add(key);
  card.classList.toggle("open", opened.has(key));
  card.setAttribute("aria-expanded", opened.has(key));
}
// The filter: one pill slides under the chosen segment; the other groups and, with text in the
// quick filter, the rows without it are hidden.
function sift(glide) {
  const bar = document.querySelector("[data-filter]"), f = D.board.ix.filter;
  const text = query.trim().toLowerCase();
  for (const r of document.querySelectorAll(f.rows)) r.toggleAttribute("data-off", !!text && !r.textContent.toLowerCase().includes(text));
  if (!bar) return;
  const all = [...bar.querySelectorAll("[data-seg]")];
  const on = all.find(b => b.dataset.seg === seg) || all.find(b => b.dataset.seg === f.default) || all[0];
  if (!on) return;
  const groups = JSON.parse(on.dataset.groups), pill = bar.querySelector(".slide"), card = bar.closest(".w");
  for (const b of all) { b.classList.toggle("on", b === on); b.setAttribute("aria-pressed", b === on); }
  pill.classList.toggle("still", !glide);
  pill.style.width = on.offsetWidth + "px";
  pill.style.transform = `translateX(${on.offsetLeft}px)`;
  let left = 0;
  for (const g of card.querySelectorAll("[data-group]")) {
    const off = groups.length > 0 && !groups.includes(g.dataset.group);
    g.toggleAttribute("data-off", off);
    left += !off;
  }
  const none = card.querySelector("[data-none]");
  if (none) none.hidden = left > 0;
}
// The drawer: one task's run, report and reviewer, read on demand from the page's own server.
function show(id) {
  const d = $("drawer"), w = D.board.ix.words, x = document.createElement("button"), body = document.createElement("div");
  if (!d.classList.contains("on")) back = document.activeElement;
  shown = id;
  x.type = "button"; x.className = "x"; x.dataset.close = ""; x.textContent = w.close;
  body.className = "body none"; body.textContent = w.loading;
  d.replaceChildren(x, body);
  const leaf = document.querySelector(`li[data-task="${CSS.escape(id)}"]`);
  if (D.run && leaf) {  // run this task as a prompt (atrium/run_claude.py)
    const r = document.createElement("button");
    r.type = "button"; r.className = "run"; r.textContent = D.run.words.button;
    r.dataset.runTask = id; r.dataset.status = leaf.dataset.status || "";
    d.insertBefore(r, body);
  }
  d.classList.add("on"); $("scrim").classList.add("on");
  d.setAttribute("aria-hidden", "false");
  x.focus();
  fetch(D.board.ix.path.replace(/^\//, "") + "?id=" + encodeURIComponent(id)).then(r => r.json())
    .then(j => { if (shown === id) { body.className = "body"; body.innerHTML = j.html; } })
    .catch(() => { if (shown === id) body.textContent = w.failed; });
}
// Run as a prompt: the server opens the coding agent in a new window on the laptop (atrium/run_claude.py).
function runPrompt(what, btn) {
  const r = D && D.run;
  if (!r) return;
  if (btn) btn.disabled = true;
  fetch(r.http_path.replace(/^\//, ""), {method: "POST", headers: {"Content-Type": "application/json"},
                                         body: JSON.stringify(Object.assign({token: r.token}, what))})
    .then(res => res.ok ? res.json() : res.text().then(t => { throw new Error(t || res.status); }))
    .then(() => { if (btn) btn.textContent = r.words.started; })
    .catch(err => { alert(fill(r.words.failed, {why: err.message})); if (btn) btn.disabled = false; });
}
function shut() {
  if (shown === null) return;
  shown = null;
  $("drawer").classList.remove("on"); $("scrim").classList.remove("on");
  $("drawer").setAttribute("aria-hidden", "true");
  if (back && back.isConnected) back.focus();
}
function nextTheme() {
  const cycle = D.board.theme_cycle || Object.keys(D.board.theme);
  const next = cycle[(cycle.indexOf(scheme()) + 1) % cycle.length];
  try { localStorage.setItem(D.board.theme_storage_key, next); } catch (e) {}
  theme();
}

// Game mode (orchestra/gamemode.py): one POST turns it on or off. Before ON, and only while tasks
// are running, ask first: they will be stopped, parked and requeued when it goes off again.
let switching = false;
function gameMode() {
  const g = D && D.gamemode, b = $("gamemode");
  if (!g || !b || switching) return;
  const turnOn = b.dataset.on !== "1", n = Number(b.dataset.running) || 0;
  if (turnOn && n > 0 && !confirm(n === 1 ? g.confirm_one : fill(g.confirm_many, {n}))) return;
  switching = true;
  b.disabled = true;
  b.textContent = g.busy;
  fetch(g.http_path.replace(/^\//, ""), {method: "POST", headers: {"Content-Type": "application/json"},
                                         body: JSON.stringify({on: turnOn})})
    .then(r => r.ok ? r.json() : r.text().then(t => { throw new Error(t || r.status); }))
    .then(() => fetch("data").then(r => r.json()).then(take))
    .catch(err => alert(fill(g.failed, {why: err.message})))
    .finally(() => { switching = false; if (D) draw(); });
}

function live(on) {
  online = on;
  const dot = $("dot"), state = $("state");
  if (dot) dot.classList.toggle("off", !on);
  if (D && state) state.textContent = on ? D.words.live : D.words.stale;
}

function take(d) { const first = !D; if (!D || d.version !== D.version || d.day !== D.day) { D = d; got = Date.now(); draw(); } else { D.now_h = d.now_h; got = Date.now(); } still = false; live(true); if (first) tick(); }

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
addEventListener("resize", () => { if (D) { still = true; place(); still = false; sift(false); } });
// The clock and the lane timers tick; only while the tab is visible.
function tick() { if (!document.hidden) { clock(); timers(); } setTimeout(tick, D.board.clock.tick_ms); }
// The board is redrawn with every update, so every click is caught here: the theme button, the
// drawer's close and the veil behind it, a filter segment, a task row, a card that opens.
document.addEventListener("click", e => {
  if (!D) return;
  const t = e.target, ix = D.board.ix;
  if (t.closest("#theme")) return nextTheme();
  if (t.closest("#gamemode")) return gameMode();
  if (t.closest("[data-close]") || t.id === "scrim") return shut();
  const rb = t.closest("button[data-run-task]");
  if (rb) return runPrompt({kind: "task", id: rb.dataset.runTask, status: rb.dataset.status}, rb);
  if (t.closest("#drawer")) return;
  const s = t.closest("[data-seg]");
  if (s) {
    seg = s.dataset.seg;
    try { localStorage.setItem(ix.filter.storage_key, seg); } catch (err) {}
    return sift(true);
  }
  if (t.closest("button, a, input, summary") || String(getSelection())) return;  // its own job, or text being selected
  const hot = t.closest("li[data-run]");
  if (hot && D.run) {  // an open hot.md bullet: ask, then run it as a prompt
    if (!confirm(fill(D.run.words.confirm, {text: hot.dataset.text}))) return;
    return runPrompt({kind: hot.dataset.run, group: hot.dataset.group, text: hot.dataset.text}, null);
  }
  const row = t.closest(ix.rows);
  if (row) return show(row.dataset.task);
  const card = t.closest(ix.expand);
  if (card) toggle(card);
});
document.addEventListener("input", e => { if (D && e.target.id === "q") { query = e.target.value; sift(false); } });
// Keys (which ones: the config): the theme, game mode, the quick filter, closing the drawer; Enter or
// Space on a focused card opens it like a click.
document.addEventListener("keydown", e => {
  if (!D || e.ctrlKey || e.metaKey || e.altKey) return;
  const k = D.board.ix.keys, t = e.target, typing = /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName);
  if (e.key === k.close) {
    if (shown !== null) shut();
    else if (t.id === "q") { query = t.value = ""; t.blur(); sift(false); }
    return;
  }
  if (typing) return;
  if (e.key === k.theme) nextTheme();
  else if (D.gamemode && e.key === D.gamemode.key) gameMode();
  else if (e.key === k.filter && $("q")) { e.preventDefault(); $("q").focus(); $("q").select(); }
  else if ((e.key === "Enter" || e.key === " ") && t.matches(D.board.ix.expand)) { e.preventDefault(); toggle(t); }
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (D) theme(); });
// The suggestion card's two buttons: the page's one write (atrium/suggest.py); the update that
// follows (its state file is watched) redraws the card without the item.
document.addEventListener("click", e => {
  const b = e.target.closest("button[data-suggest]");
  if (!b) return;
  b.disabled = true;
  fetch(b.dataset.url.replace(/^\//, ""), {method: "POST", headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({action: b.dataset.suggest, id: b.dataset.id})})
    .then(r => { if (r.ok) b.closest("li").classList.add("gone"); else b.disabled = false; })
    .catch(() => { b.disabled = false; });
});
connect();
