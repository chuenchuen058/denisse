"""
Denisse Mission Control -- a local status dashboard + quick-launch shortcuts.

Run:
    python mission_control.py

Then open http://localhost:8765 in your browser. Everything here reads
local files and talks to your local Ollama server -- nothing leaves this
machine.
"""

import json
import os
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import ollama
from dotenv import load_dotenv

import tools
from brain import MODEL, TOOL_DEFINITIONS, _get

load_dotenv()

PORT = int(os.environ.get("DENISSE_DASHBOARD_PORT", "8765"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SHORTCUTS_FILE = os.path.join(BASE_DIR, "memory", "shortcuts.json")
CONVERSATION_FILE = os.path.join(BASE_DIR, "memory", "conversation.json")
PYTHON_EXE = sys.executable

DEFAULT_SHORTCUTS = [
    {"label": "Talk to Denisse", "type": "talk", "target": ""},
    {"label": "Outlook", "type": "app", "target": "outlook"},
    {"label": "File Explorer", "type": "app", "target": "explorer"},
    {"label": "Project folder", "type": "path", "target": BASE_DIR},
    {"label": "Skills folder", "type": "path", "target": tools.SKILLS_DIR},
]


def load_shortcuts():
    if not os.path.exists(SHORTCUTS_FILE):
        save_shortcuts(DEFAULT_SHORTCUTS)
        return DEFAULT_SHORTCUTS
    try:
        with open(SHORTCUTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return DEFAULT_SHORTCUTS


def save_shortcuts(shortcuts):
    os.makedirs(os.path.dirname(SHORTCUTS_FILE), exist_ok=True)
    with open(SHORTCUTS_FILE, "w", encoding="utf-8") as f:
        json.dump(shortcuts, f, indent=2)


def get_status():
    try:
        resp = ollama.list()
        raw_models = _get(resp, "models", []) or []
        names = [(_get(m, "model") or _get(m, "name") or "?") for m in raw_models]
        return {
            "connected": True,
            "model": MODEL,
            "model_ready": any(MODEL in n for n in names),
            "available_models": names,
        }
    except Exception as e:
        return {"connected": False, "model": MODEL, "error": str(e)}


def get_facts():
    return tools._load_facts()


def get_history(limit=12):
    if not os.path.exists(CONVERSATION_FILE):
        return []
    try:
        with open(CONVERSATION_FILE, "r", encoding="utf-8") as f:
            messages = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    visible = [
        m for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    return visible[-limit:]


def get_skills():
    if not os.path.isdir(tools.SKILLS_DIR):
        return []
    names = sorted(f[:-3] for f in os.listdir(tools.SKILLS_DIR) if f.endswith(".md"))
    return [
        {
            "name": n,
            "description": tools._skill_description(os.path.join(tools.SKILLS_DIR, f"{n}.md")),
        }
        for n in names
    ]


def get_tools():
    out = []
    for t in TOOL_DEFINITIONS:
        fn = t.get("function", {})
        out.append({"name": fn.get("name"), "description": fn.get("description")})
    return out


def run_shortcut(shortcut):
    kind = shortcut.get("type")
    target = shortcut.get("target", "")
    if kind == "talk":
        subprocess.Popen(
            [PYTHON_EXE, os.path.join(BASE_DIR, "voice_mode.py")],
            cwd=BASE_DIR,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return "Launching voice mode in a new window..."
    if kind == "app":
        return tools.open_application(target)
    if kind == "path":
        os.startfile(target)
        return f"Opened {target}"
    return f"Unknown shortcut type '{kind}'"


HTML_PAGE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Denisse -- Mission Control</title>
<style>
  :root {
    /* Tron-Legacy-inspired: near-pure-black void, dark-glass panels (barely
       tinted, not solid color blocks) defined by glowing purple edges
       rather than fill contrast, and chamfered (cut-corner) shapes instead
       of rounded ones. */
    --bg: #050308; --panel: rgba(168,85,247,0.045); --panel2: rgba(255,255,255,0.02);
    --border: #4c3575; --border-soft: #2a1f42; --text: #ffffff; --muted: #a99bc4; --muted-dim: #6b5c85;
    --accent: #a855f7; --accent-dim: #7c3aed; --danger: #e2555a;
    --shadow: 0 8px 24px -8px rgba(0,0,0,0.6);
    --chamfer: 10px;
    --cut: polygon(
      var(--chamfer) 0, calc(100% - var(--chamfer)) 0, 100% var(--chamfer),
      100% calc(100% - var(--chamfer)), calc(100% - var(--chamfer)) 100%, var(--chamfer) 100%,
      0 calc(100% - var(--chamfer)), 0 var(--chamfer)
    );
  }
  * { box-sizing: border-box; }
  ::selection { background: var(--accent-dim); color: #fff; }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 10px; border: 2px solid var(--bg); }
  ::-webkit-scrollbar-thumb:hover { background: var(--muted-dim); }
  body {
    margin:0; font-family: "Segoe UI", -apple-system, system-ui, sans-serif;
    background:
      radial-gradient(1000px 460px at 15% -10%, rgba(168,85,247,0.09), transparent 60%),
      radial-gradient(900px 500px at 100% 100%, rgba(124,58,237,0.07), transparent 55%),
      var(--bg);
    color: var(--text); -webkit-font-smoothing: antialiased;
  }
  /* Purple "light worm" -- a soft glow segment traveling up and down each
     edge of the window on a loop, like Tron's grid boundary lines. Pure
     CSS via body::before/::after so no markup changes are needed. */
  @keyframes worm-travel {
    0% { transform: translateY(-160px); }
    100% { transform: translateY(100vh); }
  }
  body::before, body::after {
    content: ""; position: fixed; top: 0; width: 2px; height: 160px; z-index: 40; pointer-events: none;
    background: linear-gradient(to bottom, transparent, rgba(168,85,247,0.9), transparent);
    box-shadow: 0 0 12px 2px rgba(168,85,247,0.55);
    animation: worm-travel 3.6s ease-in-out infinite;
  }
  body::before { left: 0; }
  body::after { right: 0; animation-delay: 1.8s; }
  header {
    display:flex; align-items:center; justify-content:space-between;
    padding: 13px 20px; border-bottom: 1px solid var(--accent);
    background: rgba(5,3,8,0.75); backdrop-filter: blur(8px);
    box-shadow: 0 4px 18px -6px rgba(168,85,247,0.5);
    position: sticky; top: 0; z-index: 10;
  }
  header h1 { font-size: 17px; margin:0; letter-spacing: 2px; font-weight: 600; text-transform: uppercase; }
  header h1 span { color: var(--accent); font-weight: 500; text-shadow: 0 0 12px rgba(168,85,247,0.45); }
  .status { display:flex; align-items:center; gap:10px; font-size: 12.5px; color: var(--muted); }

  /* Presence orb -- Siri's soft glowing sphere crossed with a JARVIS
     targeting ring, standing in for the old flat status dot. Color and
     animation state follow .ok/.warn/.bad, same as the dot did. */
  .orb { position:relative; display:inline-block; width:18px; height:18px; flex:none; }
  .orb-ring {
    position:absolute; inset:-4px; border-radius:50%; border:1px solid transparent;
    border-top-color: rgba(168,85,247,0.85); border-right-color: rgba(168,85,247,0.22);
    animation: orb-spin 2.6s linear infinite;
  }
  .orb-core {
    position:absolute; inset:3px; border-radius:50%;
    background: radial-gradient(circle at 35% 30%, #f3e8ff, var(--accent) 45%, #4c1d95 100%);
    box-shadow: 0 0 8px 2px rgba(168,85,247,0.75), 0 0 18px 5px rgba(168,85,247,0.32);
    animation: orb-breathe 2.3s ease-in-out infinite;
  }
  .orb.warn .orb-ring { border-top-color: rgba(232,185,59,0.85); border-right-color: rgba(232,185,59,0.22); }
  .orb.warn .orb-core {
    background: radial-gradient(circle at 35% 30%, #fff3d6, #e8b93b 45%, #8a6212 100%);
    box-shadow: 0 0 8px 2px rgba(232,185,59,0.75), 0 0 18px 5px rgba(232,185,59,0.32);
  }
  .orb.bad .orb-ring { border-top-color: rgba(226,85,90,0.85); border-right-color: rgba(226,85,90,0.22); animation-play-state: paused; }
  .orb.bad .orb-core {
    background: radial-gradient(circle at 35% 30%, #ffd9d9, var(--danger) 45%, #6b1a1c 100%);
    box-shadow: 0 0 8px 2px rgba(226,85,90,0.75), 0 0 18px 5px rgba(226,85,90,0.32);
    animation-play-state: paused;
  }
  @keyframes orb-spin { to { transform: rotate(360deg); } }
  @keyframes orb-breathe { 0%, 100% { transform: scale(0.88); filter: brightness(1); } 50% { transform: scale(1.08); filter: brightness(1.2); } }

  /* JARVIS-style corner brackets framing the whole window. */
  .hud-frame span {
    position: fixed; width: 20px; height: 20px; pointer-events: none; z-index: 45;
    border: 1.5px solid rgba(168,85,247,0.5);
  }
  .hud-frame .tl { top: 7px; left: 7px; border-right: none; border-bottom: none; }
  .hud-frame .tr { top: 7px; right: 7px; border-left: none; border-bottom: none; }
  .hud-frame .bl { bottom: 7px; left: 7px; border-right: none; border-top: none; }
  .hud-frame .br { bottom: 7px; right: 7px; border-left: none; border-top: none; }
  .layout { display:flex; min-height: calc(100vh - 51px); }
  aside { width: 226px; border-right: 1px solid var(--border); padding: 15px; background: var(--panel2); }
  aside h2 { font-size: 11px; text-transform: uppercase; letter-spacing: 1.2px; color: var(--muted-dim); margin: 0 0 9px; font-weight: 600; }
  .shortcut {
    display:flex; align-items:center; justify-content:space-between;
    padding: 8px 10px; margin-bottom:5px; background: var(--panel);
    border:1px solid var(--border-soft); clip-path: var(--cut); cursor:pointer;
    transition: border-color .15s ease, transform .15s ease, background .15s ease;
  }
  .shortcut:hover { border-color: var(--accent); background: rgba(168,85,247,0.09); transform: translateX(1px); box-shadow: 0 0 0 1px rgba(168,85,247,0.15), 0 0 16px rgba(168,85,247,0.12); }
  .shortcut:active { transform: translateX(1px) scale(.98); }
  .shortcut .label { display:flex; align-items:center; gap:9px; font-size:13px; }
  .shortcut .arrow { color: var(--accent); transition: transform .15s ease; }
  .shortcut:hover .arrow { transform: translateX(2px); }
  .shortcut .remove { color: var(--muted-dim); font-size:12px; opacity:0; transition: opacity .15s ease, color .15s ease; padding: 2px 4px; }
  .shortcut:hover .remove { opacity:1; }
  .shortcut .remove:hover { color: var(--danger); }
  #addForm { margin-top: 14px; display:none; flex-direction:column; gap:7px; }
  #addForm.open { display:flex; animation: fade-in .15s ease; }
  #addForm input, #addForm select {
    background: var(--panel); border:1px solid var(--border); color: var(--text);
    clip-path: var(--cut); padding:7px 9px; font-size:12.5px; outline: none;
    transition: border-color .15s ease;
  }
  #addForm input:focus, #addForm select:focus { border-color: var(--accent-dim); box-shadow: 0 0 0 1px var(--accent-dim), 0 0 12px rgba(168,85,247,0.25); }
  .btn {
    background: var(--accent); color:#1a0a2e; border:none; clip-path: var(--cut);
    padding:8px 12px; font-size:12.5px; cursor:pointer; font-weight:600;
    transition: filter .15s ease, transform .1s ease, box-shadow .15s ease;
    box-shadow: 0 0 0 rgba(168,85,247,0);
  }
  .btn:hover { filter: brightness(1.08); box-shadow: 0 0 18px rgba(168,85,247,0.4); }
  .btn:active { transform: scale(.97); }
  .btn.ghost { background: transparent; border:1px solid var(--border); color: var(--muted); width:100%; }
  .btn.ghost:hover { border-color: var(--muted-dim); color: var(--text); filter: none; }
  main { flex:1; padding: 16px 20px; overflow-y:auto; }
  nav.tabs { display:flex; gap:5px; margin-bottom: 14px; }
  nav.tabs button {
    background: var(--panel2); border:1px solid var(--border); color: var(--muted);
    padding:6px 13px; clip-path: var(--cut); font-size:13px; cursor:pointer;
    transition: color .15s ease, border-color .15s ease, background .15s ease;
  }
  nav.tabs button:hover:not(.active) { color: var(--text); border-color: var(--border-soft); }
  nav.tabs button.active { color: #1a0a2e; background: var(--accent); border-color: var(--accent); font-weight:600; box-shadow: 0 0 14px rgba(168,85,247,0.35); }
  .card {
    background: var(--panel); border:1px solid var(--border-soft); clip-path: var(--cut);
    padding:13px 15px; margin-bottom:8px; box-shadow: var(--shadow);
    animation: fade-in .2s ease;
  }
  @keyframes fade-in { from { opacity: 0; transform: translateY(2px); } to { opacity: 1; transform: none; } }
  .msg { margin-bottom:10px; padding-bottom: 10px; border-bottom: 1px solid var(--border-soft); }
  .msg:last-child { border-bottom: none; margin-bottom: 0; padding-bottom: 0; }
  .msg .who { font-size:10.5px; text-transform:uppercase; letter-spacing:.6px; color: var(--muted-dim); margin-bottom:4px; font-weight: 600; }
  .msg.user .who { color: var(--accent); }
  .msg .body { font-size:13.5px; line-height: 1.5; white-space:pre-wrap; }
  table { width:100%; border-collapse: collapse; font-size:13px; }
  td, th { text-align:left; padding:8px 10px; border-bottom:1px solid var(--border-soft); }
  th { color: var(--muted-dim); font-weight:600; font-size: 11.5px; text-transform: uppercase; letter-spacing: .5px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(168,85,247,0.05); }
  .name { color: var(--accent); font-family: "Cascadia Code", Consolas, monospace; font-size: 12.5px; }
  .desc { color: var(--muted); font-size:12.5px; line-height: 1.4; }
  .empty { color: var(--muted-dim); font-size:13px; padding: 16px 0; text-align: center; }
  #toast { position:fixed; bottom:18px; right:18px; background: #0a0710; border:1px solid var(--accent); padding:10px 16px; clip-path: var(--cut); font-size:13px; display:none; box-shadow: 0 0 20px rgba(168,85,247,0.3); }
</style>
</head>
<body>
<div class="hud-frame"><span class="tl"></span><span class="tr"></span><span class="bl"></span><span class="br"></span></div>
<header>
  <h1>Denisse <span>// Mission Control</span></h1>
  <div class="status">
    <span class="orb" id="statusDot"><span class="orb-ring"></span><span class="orb-core"></span></span>
    <span id="statusText">checking...</span>
  </div>
</header>
<div class="layout">
  <aside>
    <h2>Quick Launch</h2>
    <div id="shortcuts"></div>
    <button class="btn ghost" onclick="toggleAddForm()">+ Add shortcut</button>
    <div id="addForm">
      <input id="newLabel" placeholder="Label, e.g. Spotify">
      <select id="newType">
        <option value="app">Open app</option>
        <option value="path">Open folder</option>
        <option value="talk">Talk to Denisse</option>
      </select>
      <input id="newTarget" placeholder="App name or folder path">
      <button class="btn" onclick="addShortcut()">Save</button>
    </div>
  </aside>
  <main>
    <nav class="tabs">
      <button class="active" data-tab="overview" onclick="showTab('overview')">Overview</button>
      <button data-tab="memory" onclick="showTab('memory')">Memory</button>
      <button data-tab="skills" onclick="showTab('skills')">Skills</button>
      <button data-tab="tools" onclick="showTab('tools')">Tools</button>
    </nav>
    <section id="tab-overview"><div id="history" class="card"></div></section>
    <section id="tab-memory" style="display:none"><div id="facts" class="card"></div></section>
    <section id="tab-skills" style="display:none"><div id="skills" class="card"></div></section>
    <section id="tab-tools" style="display:none"><div id="tools" class="card"></div></section>
  </main>
</div>
<div id="toast"></div>
<script>
function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
async function j(url, opts) { const r = await fetch(url, opts); return r.json(); }
function toast(msg) { const t = document.getElementById('toast'); t.textContent = msg; t.style.display = 'block'; clearTimeout(window._t); window._t = setTimeout(() => t.style.display = 'none', 3500); }

function showTab(name) {
  document.querySelectorAll('nav.tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  ['overview','memory','skills','tools'].forEach(t => document.getElementById('tab-' + t).style.display = (t === name ? 'block' : 'none'));
}

function toggleAddForm() { document.getElementById('addForm').classList.toggle('open'); }

async function refreshStatus() {
  const s = await j('/api/status');
  const dot = document.getElementById('statusDot');
  const text = document.getElementById('statusText');
  if (!s.connected) { dot.className = 'orb bad'; text.textContent = 'Offline'; }
  else if (!s.model_ready) { dot.className = 'orb warn'; text.textContent = 'Online'; }
  else { dot.className = 'orb ok'; text.textContent = 'Online'; }
}

async function refreshShortcuts() {
  const items = await j('/api/shortcuts');
  const el = document.getElementById('shortcuts');
  el.innerHTML = '';
  items.forEach((s, i) => {
    const row = document.createElement('div');
    row.className = 'shortcut';
    row.innerHTML = '<span class="label"><span class="arrow">-&gt;</span>' + esc(s.label) + '</span><span class="remove">x</span>';
    row.querySelector('.label').onclick = () => launch(i);
    row.querySelector('.remove').onclick = (e) => { e.stopPropagation(); removeShortcut(i); };
    el.appendChild(row);
  });
}

async function launch(i) {
  const res = await j('/api/launch', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({index: i}) });
  toast(res.message || (res.ok ? 'Done' : 'Failed'));
}

async function removeShortcut(i) {
  await j('/api/shortcuts/' + i, { method: 'DELETE' });
  refreshShortcuts();
}

async function addShortcut() {
  const label = document.getElementById('newLabel').value.trim();
  const type = document.getElementById('newType').value;
  const target = document.getElementById('newTarget').value.trim();
  if (!label) { toast('Give it a label first'); return; }
  await j('/api/shortcuts', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({label, type, target}) });
  document.getElementById('newLabel').value = '';
  document.getElementById('newTarget').value = '';
  document.getElementById('addForm').classList.remove('open');
  refreshShortcuts();
}

async function refreshHistory() {
  const msgs = await j('/api/history');
  const el = document.getElementById('history');
  el.innerHTML = msgs.length ? '' : '<div class="empty">No conversation yet.</div>';
  msgs.forEach(m => {
    const d = document.createElement('div');
    d.className = 'msg ' + m.role;
    d.innerHTML = '<div class="who">' + esc(m.role) + '</div><div class="body"></div>';
    d.querySelector('.body').textContent = m.content;
    el.appendChild(d);
  });
}

async function refreshFacts() {
  const facts = await j('/api/facts');
  const el = document.getElementById('facts');
  const keys = Object.keys(facts);
  if (!keys.length) { el.innerHTML = '<div class="empty">Nothing remembered yet.</div>'; return; }
  let html = '<table><tr><th>Key</th><th>Value</th></tr>';
  keys.forEach(k => html += '<tr><td class="name">' + esc(k) + '</td><td>' + esc(facts[k]) + '</td></tr>');
  el.innerHTML = html + '</table>';
}

async function refreshSkills() {
  const skills = await j('/api/skills');
  const el = document.getElementById('skills');
  el.innerHTML = skills.length ? '' : '<div class="empty">No skills installed.</div>';
  skills.forEach(s => {
    const d = document.createElement('div');
    d.style.marginBottom = '10px';
    d.innerHTML = '<div class="name">' + esc(s.name) + '</div><div class="desc"></div>';
    d.querySelector('.desc').textContent = s.description || '';
    el.appendChild(d);
  });
}

async function refreshTools() {
  const items = await j('/api/tools');
  const el = document.getElementById('tools');
  let html = '<table><tr><th>Tool</th><th>Description</th></tr>';
  items.forEach(t => html += '<tr><td class="name">' + esc(t.name) + '</td><td class="desc">' + esc(t.description) + '</td></tr>');
  el.innerHTML = html + '</table>';
}

function refreshAll() { refreshStatus(); refreshShortcuts(); refreshHistory(); refreshFacts(); refreshSkills(); refreshTools(); }
refreshAll();
setInterval(refreshStatus, 5000);
setInterval(() => { refreshHistory(); refreshFacts(); }, 8000);
</script>
</body>
</html>
'''


class Handler(BaseHTTPRequestHandler):
    def _json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def do_GET(self):
        path = urlparse(self.path).path
        routes = {
            "/api/status": get_status,
            "/api/facts": get_facts,
            "/api/history": get_history,
            "/api/skills": get_skills,
            "/api/tools": get_tools,
            "/api/shortcuts": load_shortcuts,
        }
        if path == "/":
            self._html(HTML_PAGE)
        elif path in routes:
            self._json(routes[path]())
        else:
            self._json({"error": "not found"}, status=404)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_json_body()
        if path == "/api/shortcuts":
            shortcuts = load_shortcuts()
            label = (body.get("label") or "").strip()
            kind = body.get("type")
            target = (body.get("target") or "").strip()
            if not label or kind not in ("talk", "app", "path"):
                self._json({"error": "label and a valid type are required"}, status=400)
                return
            shortcuts.append({"label": label, "type": kind, "target": target})
            save_shortcuts(shortcuts)
            self._json(shortcuts)
        elif path == "/api/launch":
            shortcuts = load_shortcuts()
            index = body.get("index")
            if index is None or not (0 <= index < len(shortcuts)):
                self._json({"error": "invalid shortcut index"}, status=400)
                return
            try:
                message = run_shortcut(shortcuts[index])
                self._json({"ok": True, "message": message})
            except Exception as e:
                self._json({"ok": False, "message": str(e)}, status=500)
        else:
            self._json({"error": "not found"}, status=404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/shortcuts/"):
            try:
                index = int(path.rsplit("/", 1)[-1])
            except ValueError:
                self._json({"error": "invalid index"}, status=400)
                return
            shortcuts = load_shortcuts()
            if 0 <= index < len(shortcuts):
                shortcuts.pop(index)
                save_shortcuts(shortcuts)
                self._json(shortcuts)
            else:
                self._json({"error": "invalid index"}, status=400)
        else:
            self._json({"error": "not found"}, status=404)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Denisse Mission Control running at {url}")
    print("Press Ctrl+C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
