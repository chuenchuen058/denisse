"""The one path every local model (Ollama) call takes. Policy: local_llm.json.

Found 10 Nov 2026 in Ollama's server.log, when llama3.1:8b sat in "Stopping..."
and every call timed out: a tutor chunk came to 8555 tokens, the server cut it to
4098 (system prompt gone), and with no num_predict and no client timeout the
model generated ~78k tokens over 20 minutes. Another call ran for 1h3m. Ollama
gives a model one slot, so every caller queued behind it. A caller that asked
for a different num_ctx (Ollama's own 4096 default, from callers that sent
none) made the scheduler unload the model to reload it, and the unload waited
on the runaway request, so `ollama ps` showed "Stopping..." until it ended.

So for every *real* call, here:
  * the GPU lease (.review/gpu.lease), re-entrant for this process and for a
    parent that names itself in the owner env, plus one lock per process;
  * ONE num_ctx per model (a caller's own is ignored) and a num_predict cap;
  * a client timeout;
  * /api/ps before the call and after a timeout: a model "Stopping" too long,
    or /api/ps hanging, is a wedge; it is logged and, with auto_restart, Ollama
    is restarted (rate-limited, logged).

A call that has been replaced (a test's fake, eval's deadline wrapper, bench's
meter) is called as given, options normalised, nothing else: no lease, no
network. That is how the existing tests keep running without Ollama.

    import local_llm
    r = local_llm.chat(job="don_extract", model=m, messages=..., options={...})
    data = local_llm.http(base, "/api/chat", body, job="orchestra_local_team")
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "local_llm.json"
_LOCK = threading.RLock()
_log = logging.getLogger("local_llm")


class LeaseBusy(TimeoutError):
    """The GPU lease stayed held by another job past the wait (a TimeoutError, so the
    callers' error handling counts it as the server's fault, not a bug)."""


def config() -> dict:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def base_url(cfg: dict | None = None) -> str:
    cfg = cfg or config()
    url = (os.environ.get(cfg["host_env"]) or cfg["host"]).strip().rstrip("/")
    return url if "://" in url else "http://" + url


# -- one context per model, a cap on every reply ---------------------------------

def num_ctx(model: str | None = None, cfg: dict | None = None) -> int:
    """The single num_ctx `model` is ever loaded with."""
    cfg = cfg or config()
    by_model = cfg.get("num_ctx_by_model") or {}
    for name in (model, (model or "").split(":")[0]):
        if name and name in by_model:
            return int(by_model[name])
    c = cfg["context"]
    env = os.environ.get(c["env"], "").strip()
    if env.isdigit():
        return int(env)
    node = json.loads((HERE / c["file"]).read_text(encoding="utf-8-sig"))
    for key in c["key"]:
        node = node[key]
    return int(node)


def options(model: str | None, opts: dict | None = None, kind: str = "chat", cfg: dict | None = None) -> dict:
    """`opts` with num_ctx set to the model's one value and num_predict capped. Embedding
    calls are left as they are (their model keeps its own single context)."""
    cfg = cfg or config()
    out = dict(opts or {})
    if kind == "embed":
        return out
    ctx = num_ctx(model, cfg)
    if "num_ctx" in out and out["num_ctx"] != ctx:
        _log.info("local_llm: %s asked for num_ctx %s; using the model's one value %s", model, out["num_ctx"], ctx)
    out["num_ctx"] = ctx
    asked = out.get("num_predict")
    if not isinstance(asked, int) or asked < 0:
        out["num_predict"] = int(cfg["num_predict"])
    else:
        out["num_predict"] = min(asked, int(cfg["num_predict_max"]))
    return out


def prompt_chars(kw: dict) -> int:
    n = len(str(kw.get("prompt") or "")) + len(str(kw.get("system") or ""))
    for m in kw.get("messages") or []:
        n += len(str((m or {}).get("content") or "")) if isinstance(m, dict) else len(str(m))
    return n


def too_long(kw: dict, opts: dict, cfg: dict | None = None) -> str | None:
    """Why this prompt likely won't fit num_ctx less num_predict, or None."""
    cfg = cfg or config()
    if "num_ctx" not in opts:
        return None
    room = opts["num_ctx"] - opts.get("num_predict", 0)
    est = int(prompt_chars(kw) / float(cfg["chars_per_token"]))
    return None if est <= room else f"prompt ~{est} tokens > {room} (num_ctx {opts['num_ctx']} - num_predict)"


def timeout_for(kind: str, job: str | None = None, asked: float | None = None, cfg: dict | None = None) -> float:
    cfg = cfg or config()
    t = (cfg.get("timeout_s_by_job") or {}).get(job) or (cfg.get("timeout_s_by_kind") or {}).get(kind) \
        or cfg["timeout_s"]
    return float(min(t, asked) if asked else t)


# -- the lease ----------------------------------------------------------------

def _default_lease(holder: str, ttl_s: int):
    from orchestra.local_team.lease import GpuLease
    return GpuLease.for_repo(HERE, holder, ttl_s)


def owner_env(pid: int | None = None, cfg: dict | None = None) -> dict:
    """The env entry a parent holding the lease gives a child, so the child's calls count as held."""
    cfg = cfg or config()
    return {cfg["lease"]["owner_env"]: str(os.getpid() if pid is None else pid)}


@contextlib.contextmanager
def lease(job: str | None, timeout_s: float, factory=None, cfg: dict | None = None):
    """Hold the GPU lease for one call. Yields True (taken here), "held" (this process or
    the named parent already holds it), or False (busy, and the job's on_busy is run)."""
    cfg = cfg or config()
    pol = {**cfg["lease"], **(cfg["lease"].get("by_job") or {}).get(job or "", {})}
    try:
        gl = (factory or _default_lease)(f"local_llm {job or 'call'}", int(timeout_s + pol["ttl_margin_s"]))
    except ImportError:  # no orchestra beside this file: nothing to share a lease with
        yield "held"
        return
    owners = {os.getpid()}
    env = os.environ.get(pol["owner_env"], "").strip()
    if env.isdigit():
        owners.add(int(env))
    info = gl.info()
    if info is not None and info.get("pid") in owners:
        yield "held"
        return
    if not gl.acquire(wait_s=pol["wait_s"], poll_s=pol["poll_s"]):
        held = gl.info() or {}
        why = f"GPU lease held by {held.get('holder', '?')} (pid {held.get('pid', '?')}) after {pol['wait_s']}s"
        if pol["on_busy"] != "run":
            raise LeaseBusy(why)
        _log.warning("local_llm: %s; %s runs anyway (on_busy run)", why, job)
        yield False
        return
    try:
        yield True
    finally:
        gl.release()


# -- is the server wedged? -------------------------------------------------------

_FRACTION = re.compile(r"(\.\d{6})\d+")


def _when(text) -> dt.datetime | None:
    if not text or not isinstance(text, str):
        return None
    s = _FRACTION.sub(r"\1", text.strip()).replace("Z", "+00:00")
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def wedged(ps: dict | None, now: dt.datetime | None = None, cfg: dict | None = None,
           ps_error: Exception | None = None) -> str | None:
    """Why the server is wedged, or None. `ps` is /api/ps's answer; `ps_error` what reading
    it raised instead. Refused (not running) is not a wedge; hanging is."""
    cfg = cfg or config()
    if ps_error is not None:
        reason = getattr(ps_error, "reason", ps_error)
        if isinstance(reason, (TimeoutError, )) or isinstance(ps_error, TimeoutError):
            return f"/api/ps did not answer in {cfg['wedge']['ps_timeout_s']}s"
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    for m in (ps or {}).get("models") or []:
        until = _when(m.get("expires_at"))
        if until is None:
            continue
        late = (now - until).total_seconds()
        if late > cfg["wedge"]["stopping_s"]:
            return f"{m.get('name') or m.get('model')} has been Stopping for {int(late)}s"
    return None


def _text(raw) -> str:
    return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)


def read_ps(base: str | None = None, opener=None, cfg: dict | None = None) -> tuple:
    """(/api/ps answer or None, the error reading it or None)."""
    cfg = cfg or config()
    opener = opener or urllib.request.urlopen
    try:
        with opener((base or base_url(cfg)) + "/api/ps", timeout=cfg["wedge"]["ps_timeout_s"]) as r:
            return json.loads(_text(r.read())), None
    except (OSError, ValueError) as e:
        return None, e


def _main_path(rel: str) -> Path:
    try:
        from orchestra.local_team.lease import main_checkout
        return main_checkout(HERE) / rel
    except ImportError:
        return HERE / rel


def note(line: str, path: Path | None = None, cfg: dict | None = None) -> None:
    """One watchdog line: the log file in the main checkout, and the logger."""
    cfg = cfg or config()
    _log.warning("local_llm: %s", line)
    path = path or _main_path(cfg["wedge"]["log"])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def may_restart(state: dict, now: float, cfg: dict | None = None) -> bool:
    """The restart decision once a wedge is found: auto_restart on, and none within
    min_restart_interval_s."""
    cfg = cfg or config()
    w = cfg["wedge"]
    if not w["auto_restart"]:
        return False
    last = state.get("last_restart")
    return not isinstance(last, (int, float)) or now - last >= w["min_restart_interval_s"]


def restart(why: str, *, runner=None, popen=None, opener=None, clock=None, sleep=None,
            state_path: Path | None = None, log_path: Path | None = None, cfg: dict | None = None,
            platform: str | None = None) -> bool:
    """Restart Ollama if the rules allow: kill, start, wait for it to answer. True if it
    was restarted and answers. Logged either way."""
    cfg = cfg or config()
    w = cfg["wedge"]
    runner, popen = runner or subprocess.run, popen or subprocess.Popen
    clock, sleep = clock or time.time, sleep or time.sleep
    state_path = state_path or _main_path(w["state"])
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    now = clock()
    if not may_restart(state, now, cfg):
        note(f"WEDGED ({why}); not restarting (auto_restart off, or restarted under "
             f"{w['min_restart_interval_s']}s ago)", log_path, cfg)
        return False
    plan = (w.get("restart") or {}).get(platform or sys.platform)
    if not plan:
        note(f"WEDGED ({why}); no restart commands for {platform or sys.platform}: restart Ollama by hand",
             log_path, cfg)
        return False
    note(f"WEDGED ({why}); restarting Ollama", log_path, cfg)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"last_restart": now, "why": why}), encoding="utf-8")
    except OSError:
        pass
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for argv in plan.get("kill") or []:
        try:
            runner(argv, capture_output=True, timeout=w["ps_timeout_s"] * 6, creationflags=flags)
        except (OSError, subprocess.SubprocessError):
            pass
    start = [os.path.expandvars(a) for a in plan.get("start") or []]
    if start:
        try:
            popen(start, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | flags)
        except OSError as e:
            note(f"restart: could not start {start[0]}: {e}", log_path, cfg)
            return False
    deadline = clock() + w["up_wait_s"]
    while True:
        ps, err = read_ps(opener=opener, cfg=cfg)
        if err is None:
            note("restart: Ollama answers again", log_path, cfg)
            return True
        if clock() >= deadline:
            note(f"restart: Ollama still not answering after {w['up_wait_s']}s ({err})", log_path, cfg)
            return False
        sleep(w["up_poll_s"])


def check(base: str | None = None, *, opener=None, cfg: dict | None = None, **restart_kw) -> str | None:
    """Read /api/ps; a wedge is logged and (rules allowing) recovered. The wedge, or None."""
    cfg = cfg or config()
    ps, err = read_ps(base, opener, cfg)
    why = wedged(ps, cfg=cfg, ps_error=err)
    if why:
        restart(why, opener=opener, cfg=cfg, **restart_kw)
    return why


def _dropped(exc: Exception) -> bool:
    """A timeout or a lost connection: the kind of failure a wedged server gives."""
    reason = getattr(exc, "reason", None)
    if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
        return True
    name = type(exc).__name__.lower()
    return "timeout" in name or isinstance(exc, (ConnectionResetError, ConnectionAbortedError)) \
        or "remoteprotocol" in name or "readerror" in name


# -- the calls ----------------------------------------------------------------------

def _guarded(job, kind, timeout, send, base=None, lease_factory=None, opener=None, cfg=None):
    cfg = cfg or config()
    with _LOCK, lease(job, timeout, lease_factory, cfg):
        check(base, opener=opener, cfg=cfg)
        try:
            return send()
        except Exception as exc:
            if not isinstance(exc, LeaseBusy) and _dropped(exc):
                why = check(base, opener=opener, cfg=cfg)
                if not why:
                    note(f"{job or kind}: {kind} failed ({type(exc).__name__}: {exc}); server not wedged", cfg=cfg)
            raise


def _real_ollama(fn) -> bool:
    try:
        import ollama
    except ImportError:
        return False
    return getattr(fn, "__self__", None) is getattr(ollama, "_client", object())


def _call(kind: str, job: str | None, kw: dict, timeout: float | None, api=None):
    import ollama
    cfg = config()
    fn = getattr(api if api is not None else ollama, kind)
    if kind != "embed" or "options" in kw:
        kw["options"] = options(kw.get("model"), kw.get("options"), kind, cfg)
    if kind != "embed":
        long = too_long(kw, kw["options"], cfg)
        if long:
            _log.warning("local_llm: %s: %s", job or kind, long)
    if not _real_ollama(fn):  # a fake, eval's deadline, bench's meter: as given
        return fn(**kw)
    t = timeout_for(kind, job, timeout, cfg)
    client = ollama.Client(host=base_url(cfg), timeout=t)
    if ollama.Client is not getattr(sys.modules.get("ollama._client"), "Client", None):
        return getattr(client, kind)(**kw)  # a test's Client: as given
    if kw.get("stream"):  # the lease would end before the stream did: timeout only
        return getattr(client, kind)(**kw)
    return _guarded(job, kind, t, lambda: getattr(client, kind)(**kw), cfg=cfg)


def chat(*, job: str | None = None, timeout: float | None = None, api=None, **kw):
    """ollama.chat, through the shared path. `api`: the caller's own ollama (module or
    stand-in), looked up at call time so a test that swaps it is still obeyed."""
    return _call("chat", job, kw, timeout, api)


def generate(*, job: str | None = None, timeout: float | None = None, api=None, **kw):
    return _call("generate", job, kw, timeout, api)


def embed(*, job: str | None = None, timeout: float | None = None, api=None, **kw):
    return _call("embed", job, kw, timeout, api)


def _real_urlopen(fn) -> bool:
    return getattr(fn, "__module__", None) == "urllib.request" and getattr(fn, "__name__", "") == "urlopen"


def http(base: str | None, path: str, body: dict, *, job: str | None = None, timeout: float | None = None,
         opener=None, lease_factory=None) -> dict:
    """POST `body` to Ollama's REST `path` (/api/chat, /api/generate) through the shared path:
    options normalised, and for the real urlopen the lease, a timeout and the wedge check."""
    cfg = config()
    kind = path.rstrip("/").rsplit("/", 1)[-1]
    body = dict(body)
    body["options"] = options(body.get("model"), body.get("options"), kind, cfg)
    long = too_long(body, body["options"], cfg)
    if long:
        _log.warning("local_llm: %s: %s", job or kind, long)
    opener = opener or urllib.request.urlopen
    t = timeout_for(kind, job, timeout, cfg)
    base = (base or base_url(cfg)).rstrip("/")

    def send():
        req = urllib.request.Request(base + path, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with opener(req, timeout=t) as r:
            return json.loads(_text(r.read()))

    if not _real_urlopen(opener):
        return send()
    return _guarded(job, kind, t, send, base, lease_factory, opener, cfg=cfg)


def unload(model: str, *, job: str | None = None, opener=None, lease_factory=None) -> None:
    """Unload `model` (keep_alive 0) only while holding the lease, so never under a request
    that another caller has in flight. The way to free the GPU; not `ollama stop`."""
    cfg = config()
    opener = opener or urllib.request.urlopen
    t = timeout_for("generate", job, None, cfg)
    base = base_url(cfg)

    def send():
        req = urllib.request.Request(base + "/api/generate", data=json.dumps(
            {"model": model, "keep_alive": 0}).encode("utf-8"), headers={"Content-Type": "application/json"})
        with opener(req, timeout=t) as r:
            r.read()

    if not _real_urlopen(opener):
        return send()
    with _LOCK, lease(job, t, lease_factory, cfg):
        send()
