// Atrium hub (v3 mission board): shows what /events sends. Read-only. atrium/board.py renders
// every section from real data; this file only puts that HTML on screen and adds what moves
// with the clock: the date and time, the lane timers, the gliding agent pointers, the favicon
// and the theme. Every word and colour comes from the data (atrium/config.json "live").
"use strict";
const $ = id => document.getElementById(id);
const root = document.documentElement;
let D = null, got = 0, es = null, still = true, online = false;  // still: next placement jumps, no glide
const ONCE = new URLSearchParams(location.search).has("once");  // one fetch, no stream (screenshots)

function nowH() { return D ? D.now_h + (Date.now() - got) / 3600000 : 0; }
const fill = (s, f) => s.replace(/\{(\w+)\}/g, (m, k) => k in f ? f[k] : m);

// -- theme: the saved choice, else the system's; colours are the config's -------------
function scheme() {
  const saved = D && localStorage.getItem(D.board.theme_storage_key);
  return saved || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
function theme() {
  const name = scheme(), vars = D.board.theme[name] || {};
  root.dataset.theme = name;
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
    } else if (el.dataset.html !== c.html) {
      el.innerHTML = c.html;
      el.dataset.html = c.html;
    }
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
  $("top").innerHTML = b.header;
  $("rings").innerHTML = b.rings;
  $("foot").textContent = D.words.foot;
  cards();
  favicon(); clock(); timers(); place(); live(online);
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
addEventListener("resize", () => { if (D) { still = true; place(); still = false; } });
// The clock and the lane timers tick; only while the tab is visible.
function tick() { if (!document.hidden) { clock(); timers(); } setTimeout(tick, D.board.clock.tick_ms); }
// The header is redrawn with every update, so its button is caught here.
document.addEventListener("click", e => {
  if (!D || !e.target.closest("#theme")) return;
  localStorage.setItem(D.board.theme_storage_key, scheme() === "light" ? "dark" : "light");
  theme();
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (D) theme(); });
connect();
