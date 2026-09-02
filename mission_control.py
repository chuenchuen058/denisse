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
    --bg: #0b0f14; --panel: #121822; --panel2: #0e141d; --border: #223044;
    --text: #d8e2ee; --muted: #7f92a8; --accent: #4fd1c5; --danger: #e2555a;
  }
  * { box-sizing: border-box; }
  body { margin:0; font-family: -apple-system, Segoe UI, sans-serif; background: var(--bg); color: var(--text); }
  header { display:flex; align-items:center; justify-content:space-between; padding: 14px 20px; border-bottom: 1px solid var(--border); }
  header h1 { font-size: 18px; margin:0; letter-spacing: .5px; }
  header h1 span { color: var(--accent); }
  .status { display:flex; align-items:center; gap:8px; font-size: 13px; color: var(--muted); }
  .dot { width:9px; height:9px; border-radius:50%; background:#555; }
  .dot.ok { background:#3ddc84; }
  .dot.warn { background:#e8b93b; }
  .dot.bad { background:var(--danger); }
  .layout { display:flex; min-height: calc(100vh - 53px); }
  aside { width: 260px; border-right: 1px solid var(--border); padding: 16px; background: var(--panel2); }
  aside h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin: 0 0 10px; }
  .shortcut { display:flex; align-items:center; justify-content:space-between; padding: 9px 10px; margin-bottom:6px; background: var(--panel); border:1px solid var(--border); border-radius:8px; cursor:pointer; }
  .shortcut:hover { border-color: var(--accent); }
  .shortcut .label { display:flex; align-items:center; gap:8px; font-size:13px; }
  .shortcut .arrow { color: var(--accent); }
  .shortcut .remove { color: var(--muted); font-size:12px; opacity:0; }
  .shortcut:hover .remove { opacity:1; }
  .shortcut .remove:hover { color: var(--danger); }
  #addForm { margin-top: 14px; display:none; flex-direction:column; gap:6px; }
  #addForm.open { display:flex; }
  #addForm input, #addForm select { background: var(--panel); border:1px solid var(--border); color: var(--text); border-radius:6px; padding:6px 8px; font-size:12px; }
  .btn { background: var(--accent); color:#04211d; border:none; border-radius:6px; padding:7px 10px; font-size:12px; cursor:pointer; font-weight:600; }
  .btn.ghost { background: transparent; border:1px solid var(--border); color: var(--muted); width:100%; }
  main { flex:1; padding: 20px; overflow-y:auto; }
  nav.tabs { display:flex; gap:6px; margin-bottom: 16px; }
  nav.tabs button { background: var(--panel2); border:1px solid var(--border); color: var(--muted); padding:7px 14px; border-radius:7px; font-size:13px; cursor:pointer; }
  nav.tabs button.active { color: var(--bg); background: var(--accent); border-color: var(--accent); font-weight:600; }
  .card { background: var(--panel); border:1px solid var(--border); border-radius:10px; padding:14px 16px; margin-bottom:10px; }
  .msg { margin-bottom:10px; }
  .msg .who { font-size:11px; text-transform:uppercase; letter-spacing:.5px; color: var(--muted); margin-bottom:3px; }
  .msg.user .who { color: var(--accent); }
  .msg .body { font-size:13px; white-space:pre-wrap; }
  table { width:100%; border-collapse: collapse; font-size:13px; }
  td, th { text-align:left; padding:6px 8px; border-bottom:1px solid var(--border); }
  th { color: var(--muted); font-weight:500; }
  .name { color: var(--accent); font-family: Consolas, monospace; }
  .desc { color: var(--muted); font-size:12px; }
  .empty { color: var(--muted); font-size:13px; padding: 10px 0; }
  #toast { position:fixed; bottom:18px; right:18px; background: var(--panel); border:1px solid var(--accent); padding:10px 16px; border-radius:8px; font-size:13px; display:none; }
</style>
</head>
<body>
<header>
  <h1>Denisse <span>// Mission Control</span></h1>
  <div class="status"><span class="dot" id="statusDot"></span><span id="statusText">checking...</span></div>
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
  if (!s.connected) { dot.className = 'dot bad'; text.textContent = 'Ollama unreachable'; }
  else if (!s.model_ready) { dot.className = 'dot warn'; text.textContent = s.model + ' not pulled yet'; }
  else { dot.className = 'dot ok'; text.textContent = s.model + ' ready'; }
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
