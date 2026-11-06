"""The real agents an exam can be sent to. Each returns a function
    agent(prompt, cwd, timeout) -> core.Reply

  codex     the Orchestra codex lane argv (config.json lane_argv), prompt on stdin
  claude    the Orchestra claude lane, only with --include-claude (Chuen's allowance is scarce)
  copilot   node <@github/copilot entry> -p <prompt> -s, as agent_hub/server.mjs calls it
  gemini    node <@google/gemini-cli entry>, as the hub does; IneligibleTierError = skipped
  local:<m> every model Ollama lists at http://127.0.0.1:11434, if it answers
  don       the study tutor: python -m tutor consult --json (study tutor 5/5). An exam that ships
            a `vault/` folder is answered from that vault (and a scratch `state/`), so the canary
            runs on a fixture, never the real vault; otherwise the real one. Only exams naming it
            (rules.json agents.own_exams_only, bank "agents": ["don"]) are put to it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .core import Reply, run_proc
from .settings import load

OLLAMA = os.environ.get("OLLAMA_HOST_URL", load()["agents"]["ollama_url"])


def _lanes() -> dict:
    from orchestra import runner  # imported late: it reads config and looks for CLIs on import
    return runner.lanes()


def _env() -> dict:
    # Same stripping as runner.run_lane: never bill the API, never look like a nested claude.
    return {k: v for k, v in os.environ.items() if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")
            and k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")}


def _exe(argv0: str) -> str | None:
    return argv0 if Path(argv0).exists() else shutil.which(argv0)


def _npm_entry(pkg: str, name: str) -> str | None:
    """The CLI's JS entry, found by the runner's resolver (config.json "npm_dirs"), so the exams
    see the same installs the lanes do, including npm installs made from the Claude desktop app
    (3 Nov 2026: under Task Scheduler the exams logged "Copilot CLI not found")."""
    from orchestra import runner
    return runner.npm_bin(pkg, name)


def _refused(r: Reply) -> Reply:
    """An agent that says it can't take work at all is skipped, not failed."""
    m = re.search(load()["agents"]["not_served_pattern"], r.text or "", re.I) if r.error else None
    return Reply(unavailable=m.group(0) + ": " + (r.error or "")[:load()["agents"]["refusal_chars"]]) if m else r


def codex(prompt: str, cwd: Path, timeout: float) -> Reply:
    argv = list(_lanes().get("codex") or [])
    exe = _exe(argv[0]) if argv else None
    if not exe:
        return Reply(unavailable="codex lane not found")
    last = cwd / load()["agents"]["codex_last_file"]
    rest = argv[1:-1] if argv[-1] == "-" else argv[1:]
    argv = [exe, *rest, "-C", str(cwd), "-o", str(last), "-"]  # "-" = prompt on stdin
    r = _refused(run_proc(argv, cwd=cwd, timeout=timeout, stdin=prompt, env=_env()))
    try:
        text = last.read_text(encoding="utf-8").strip()
        last.unlink()
        if text:
            r.text = text
    except OSError:
        pass
    return r


def claude(prompt: str, cwd: Path, timeout: float) -> Reply:
    argv = list(_lanes().get("claude") or [])
    exe = _exe(argv[0]) if argv else None
    if not exe:
        return Reply(unavailable="claude lane not found")
    r = _refused(run_proc([exe, *argv[1:]], cwd=cwd, timeout=timeout, stdin=prompt, env=_env()))
    # stream-json: the last JSON line with a "result" is the answer
    for line in reversed((r.text or "").splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "result" in obj:
            r.text = str(obj["result"])
            break
    return r


def copilot(prompt: str, cwd: Path, timeout: float) -> Reply:
    entry = _npm_entry("@github/copilot", "copilot")
    node = shutil.which("node")
    if not entry or not node:
        return Reply(unavailable="Copilot CLI not found")
    return _refused(run_proc([node, entry, "-p", prompt, "-s", "--allow-all-tools"], cwd=cwd, timeout=timeout, env=_env()))


def gemini(prompt: str, cwd: Path, timeout: float) -> Reply:
    entry = _npm_entry("@google/gemini-cli", "gemini")
    node = shutil.which("node")
    if not entry or not node:
        return Reply(unavailable="Gemini CLI not found")
    argv = [node, entry, "--approval-mode", "auto_edit", "-o", "text", "-p", load()["agents"]["gemini_prompt"]]
    return _refused(run_proc(argv, cwd=cwd, timeout=timeout, stdin=prompt, env=_env()))


def ollama_models(timeout: float | None = None) -> list[str]:
    """Installed Ollama models, or [] if Ollama isn't running."""
    timeout = load()["agents"]["discovery_timeout_s"] if timeout is None else timeout
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=timeout) as resp:
            return [m["name"] for m in json.load(resp).get("models", [])]
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        return []


def local(model: str):
    def ask(prompt: str, cwd: Path, timeout: float) -> Reply:
        policy = load()["agents"]
        files = "".join(policy["local_file_template"].format(name=p.name, content=p.read_text(encoding="utf-8"))
                        for p in sorted(cwd.glob(policy["local_file_glob"])))
        body = json.dumps({"model": model, "prompt": prompt + (policy["local_files_prompt"].format(files=files) if files else ""),
                           "stream": False}).encode()
        req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return Reply(text=json.load(resp).get("response", ""))
        except TimeoutError:
            return Reply(error=f"timed out after {timeout:g}s")
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), TimeoutError):
                return Reply(error=f"timed out after {timeout:g}s")
            return Reply(error=f"ollama: {e}")
        except (OSError, ValueError) as e:
            return Reply(error=f"ollama: {e}")
    return ask


def _opinion(text: str) -> dict | None:
    """The last JSON object line with an "answer" (python -m tutor consult --json)."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and "answer" in data:
            return data
    return None


def don(prompt: str, cwd: Path, timeout: float) -> Reply:
    policy = load()["agents"]
    argv = [sys.executable] + [a.replace("{prompt}", prompt) for a in policy["don_command"]]
    vault = cwd / policy["don_vault_dir"]
    if vault.is_dir():  # the exam brought its own notes: never the real vault
        state = cwd / policy["don_state_dir"]
        argv += [a.replace("{vault}", str(vault)).replace("{state}", str(state)) for a in policy["don_vault_args"]]
    env = {**_env(), "PYTHONIOENCODING": "utf-8"}
    from .core import REPO  # run from the repo so `-m tutor` resolves whatever the exam's temp dir is
    r = run_proc(argv, cwd=REPO, timeout=timeout, env=env)
    data = _opinion(r.text)
    if data is None:
        return r if r.error else Reply(text=r.text, error=policy["don_no_json"])
    sep = ", "
    try:  # the tutor's name is its own config's (tutor.json "name"); the agent key stands in without it
        tutor = json.loads((REPO / policy["don_name_file"]).read_text(encoding="utf-8-sig"))[policy["don_name_key"]]
    except (OSError, ValueError, KeyError, TypeError):
        tutor = "don"
    text = policy["don_answer"].format(
        tutor=tutor, confidence=data.get("confidence", "?"), who=data.get("who", "?"),
        sources=sep.join(data.get("sources") or []) or policy["don_none"],
        caveats=sep.join(data.get("caveats") or []) or policy["don_none"], answer=data.get("answer", ""))
    if r.error:  # exit 1: the tutor had no grounded answer -- a scored fail with its reason, answered
        return Reply(text=text, error=policy["don_no_answer"].format(
            caveats=sep.join(data.get("caveats") or []) or policy["don_none"]))
    return Reply(text=text)


NAMED = {"codex": codex, "copilot": copilot, "gemini": gemini, "claude": claude, "don": don}


def available(include_claude: bool = False, only: str | None = None) -> dict:
    """name -> agent for every agent worth asking. Missing CLIs are reported by the agent as
    unavailable on first use, so the run logs why they were skipped."""
    out = {n: f for n, f in NAMED.items() if n != "claude" or include_claude or only == "claude"}
    if only is None or only.startswith("local"):
        for m in ollama_models():
            out[f"local:{m}"] = local(m)
    if only:
        out = {n: f for n, f in out.items() if n == only or (only == "local" and n.startswith("local:"))}
    return out
