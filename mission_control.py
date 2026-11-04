"""
Denisse Mission Control -- a local status dashboard + quick-launch shortcuts.

Run:
    python mission_control.py

Then open http://127.0.0.1:8765 in your browser (host and port: the
"mission_control" section of housekeeping.json, which also holds the
default shortcuts, limits and wording; the page itself is
mission_control.html -- AGENTS.md Rule 0). Everything here reads
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

import housekeeping

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON_EXE = sys.executable
CONFIG = {}


def configure(path=None) -> dict:
    """(Re)load housekeeping.json's "mission_control" section into the module values."""
    global CONFIG, PORT, SHORTCUTS_FILE, CONVERSATION_FILE, PAGE_FILE, DEFAULT_SHORTCUTS
    CONFIG = cfg = housekeeping.load(path)["mission_control"]
    try:
        PORT = int(os.environ["DENISSE_DASHBOARD_PORT"])
    except (KeyError, ValueError):
        PORT = cfg["port"]
    SHORTCUTS_FILE = housekeeping.path(cfg["shortcuts_file"])
    CONVERSATION_FILE = housekeeping.path(cfg["conversation_file"])
    PAGE_FILE = housekeeping.path(cfg["page"])
    places = {"repo": BASE_DIR, "skills": tools.SKILLS_DIR}
    DEFAULT_SHORTCUTS = [{**s, "target": s.get("target", "").format(**places)} for s in cfg["default_shortcuts"]]
    return CONFIG


def _text(key, **fields) -> str:
    return CONFIG["text"][key].format(**fields)


configure()


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


def get_history(limit=None):
    limit = CONFIG["history_limit"] if limit is None else limit
    if not os.path.exists(CONVERSATION_FILE):
        return []
    try:
        with open(CONVERSATION_FILE, "r", encoding="utf-8") as f:
            messages = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    visible = [
        m for m in messages
        if m.get("role") in CONFIG["history_roles"] and m.get("content")
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
            [PYTHON_EXE, os.path.join(BASE_DIR, CONFIG["voice_script"])],
            cwd=BASE_DIR,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        return _text("launching_voice")
    if kind == "app":
        return tools.open_application(target)
    if kind == "path":
        os.startfile(target)
        return _text("opened", target=target)
    return _text("unknown_type", kind=kind)


def page() -> str:
    """The dashboard's HTML, read on each request so an edit shows on reload."""
    with open(PAGE_FILE, encoding="utf-8") as f:
        return f.read()


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
            self._html(page())
        elif path in routes:
            self._json(routes[path]())
        else:
            self._json({"error": _text("not_found")}, status=404)

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
            if not label or kind not in housekeeping.SHORTCUT_TYPES:
                self._json({"error": _text("bad_shortcut")}, status=400)
                return
            shortcuts.append({"label": label, "type": kind, "target": target})
            save_shortcuts(shortcuts)
            self._json(shortcuts)
        elif path == "/api/launch":
            shortcuts = load_shortcuts()
            index = body.get("index")
            if index is None or not (0 <= index < len(shortcuts)):
                self._json({"error": _text("bad_launch_index")}, status=400)
                return
            try:
                message = run_shortcut(shortcuts[index])
                self._json({"ok": True, "message": message})
            except Exception as e:
                self._json({"ok": False, "message": str(e)}, status=500)
        else:
            self._json({"error": _text("not_found")}, status=404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/shortcuts/"):
            try:
                index = int(path.rsplit("/", 1)[-1])
            except ValueError:
                self._json({"error": _text("bad_index")}, status=400)
                return
            shortcuts = load_shortcuts()
            if 0 <= index < len(shortcuts):
                shortcuts.pop(index)
                save_shortcuts(shortcuts)
                self._json(shortcuts)
            else:
                self._json({"error": _text("bad_index")}, status=400)
        else:
            self._json({"error": _text("not_found")}, status=404)


def main():
    server = ThreadingHTTPServer((CONFIG["host"], PORT), Handler)
    url = _text("url", host=CONFIG["host"], port=PORT)
    print(_text("running", url=url))
    print(_text("stop_hint"))
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(_text("stopping"))
        server.shutdown()


if __name__ == "__main__":
    main()
