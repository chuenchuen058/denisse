"""Shared plumbing for the local team: config, log, state, role files, schema checks and
the one Ollama call. Every role's model output is validated against its schema here, and
again by the deterministic code that uses it; the model never writes a file itself.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .lease import GpuLease
from .settings import load

HERE = Path(__file__).resolve().parent
ROLES = HERE / "roles"
DEFAULT_REPO = HERE.parent.parent

# Model choices are in orchestra/models.json, not here (AGENTS.md Rule 0).
DEFAULTS = load()["defaults"]


class Skip(Exception):
    """Not an error: the GPU or Ollama isn't available now. Logged, exit 0."""


class BadOutput(Exception):
    """The model's answer didn't fit the role's schema or referred to things it wasn't given."""


def now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def load_config(repo: Path) -> dict:
    """DEFAULTS, then config.json, then models.json: each read from `repo`'s orchestra/ if
    present, else this checkout's. models.json wins for the keys it holds."""
    cfg = load()["defaults"]
    for name in ("config.json", "models.json"):
        for path in (Path(repo) / "orchestra" / name, HERE.parent / name):
            if path.exists():
                cfg.update({k: v for k, v in json.loads(path.read_text(encoding="utf-8-sig")).items()
                            if not k.startswith("_")})
                break
    return cfg


@dataclass
class Ctx:
    repo: Path
    cfg: dict = field(default_factory=dict)
    ollama: str = ""

    def __post_init__(self):
        self.repo = Path(self.repo).resolve()
        self.cfg = {**load_config(self.repo), **self.cfg}
        self.ollama = (self.ollama or os.environ.get("OLLAMA_HOST_URL") or load()["ollama"]["url"]).rstrip("/")

    @property
    def review(self) -> Path:
        return self.repo / ".review"

    @property
    def home(self) -> Path:
        """Everything the local team writes lives under here (git-ignored)."""
        return self.review / "local_team"

    def log(self, msg: str) -> None:
        line = f"{now().isoformat()} {msg}"
        print(line)
        self.home.mkdir(parents=True, exist_ok=True)
        with (self.home / "producer.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def state(self) -> dict:
        try:
            return json.loads((self.home / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_state(self, st: dict) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.home / "state.json.tmp"
        tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
        os.replace(tmp, self.home / "state.json")

    def rel(self, p: Path) -> str:
        return Path(p).resolve().relative_to(self.repo).as_posix()


# ---------------------------------------------------------------- role files

@dataclass
class Role:
    name: str
    version: str
    meta: dict
    prompt: str
    schema: dict


def load_role(name: str) -> Role:
    text = (ROLES / f"{name}.md").read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n(.*)", text.replace("\r\n", "\n"), re.S)
    if not m:
        raise ValueError(f"roles/{name}.md has no front matter")
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    schema = json.loads((ROLES / f"{name}.schema.json").read_text(encoding="utf-8"))
    return Role(name, meta.get("version", "0"), meta, load()["role_prompts"][name], schema)


# ---------------------------------------------------------------- schema

def validate(value, schema: dict, where: str = "$") -> list[str]:
    """The small part of JSON Schema the role schemas use: type, required, properties,
    additionalProperties, items, enum, min/maxItems, min/maxLength, pattern."""
    errs: list[str] = []
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    if t and not isinstance(value, types[t]) or (t == "integer" and isinstance(value, bool)):
        return [f"{where}: expected {t}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{where}: {value!r} not one of {schema['enum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errs.append(f"{where}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{where}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errs.append(f"{where}: does not match {schema['pattern']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errs.append(f"{where}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{where}: more than {schema['maxItems']} items")
        for i, v in enumerate(value):
            errs += validate(v, schema.get("items", {}), f"{where}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                errs.append(f"{where}: missing {k}")
        for k, v in value.items():
            if k in props:
                errs += validate(v, props[k], f"{where}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{where}: unexpected {k}")
    return errs


# ---------------------------------------------------------------- Ollama

def _get(ctx: Ctx, path: str, timeout: float | None = None) -> dict:
    timeout = load()["ollama"]["probe_timeout_s"] if timeout is None else timeout
    with urllib.request.urlopen(ctx.ollama + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


_last_call_end = float("-inf")  # time.monotonic() when this process's last model call ended


def gpu_utilisation() -> int | None:
    """GPU busy percent from nvidia-smi, or None if it can't be read."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=load()["gpu"]["probe_timeout_s"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return max(int(x) for x in out.split() if x.strip().isdigit())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def model_name(ctx: Ctx) -> str:
    """local_team_model, unless model_routing.json routes orchestra_local_team elsewhere."""
    try:
        import model_routing
    except ImportError:  # run from outside the repo root: today's model
        return ctx.cfg["local_team_model"]
    return model_routing.model_for(load()["routing_job"], ctx.cfg["local_team_model"])


def ready(ctx: Ctx) -> str:
    """Raise Skip unless Ollama is up, the model is installed (never pulled), no other model
    is loaded (ours would evict it), and the GPU isn't busy. Returns the model name."""
    model = model_name(ctx)
    try:
        tags = _get(ctx, "/api/tags")
    except (OSError, ValueError) as e:
        raise Skip(f"Ollama not reachable at {ctx.ollama}: {e}")
    names = {m.get("name") or m.get("model") for m in tags.get("models", [])}
    if model not in names and f"{model}:latest" not in names:
        raise Skip(f"model {model} is not installed (have: {', '.join(sorted(n for n in names if n)) or 'none'}); "
                   f"not pulling it")
    try:
        loaded = {m.get("name") or m.get("model") for m in _get(ctx, "/api/ps").get("models", [])}
    except (OSError, ValueError):
        loaded = set()
    others = sorted(n for n in loaded if n and n not in (model, f"{model}:latest"))
    if others:
        raise Skip(f"another model is loaded ({', '.join(others)}); loading {model} would evict it")
    busy = gpu_utilisation()
    limit = ctx.cfg["local_team_gpu_busy_pct"]
    if busy is not None and busy >= limit and time.monotonic() - _last_call_end < load()["gpu"]["settle_window_s"]:
        # Our own call just finished and nvidia-smi still shows it (found 2 Nov 2026: 97% right
        # after the librarian's call). Let it settle once before deciding someone else is busy.
        time.sleep(load()["gpu"]["settle_delay_s"])
        busy = gpu_utilisation()
    if busy is not None and busy >= limit:
        raise Skip(f"GPU {busy}% busy (limit {ctx.cfg['local_team_gpu_busy_pct']}%): Denisse or another job is using it")
    return model


def ask(ctx: Ctx, role: Role, payload: dict) -> dict:
    """One inference under the GPU lease. Skip if the lease is held; BadOutput if the answer
    isn't valid JSON for the role's schema."""
    from ollama_output import call_with_schema

    model = ready(ctx)
    lease = GpuLease.for_repo(ctx.repo, f"orchestra.local_team {role.name}", ctx.cfg["gpu_lease_ttl_s"])
    if not lease.try_acquire():
        held = lease.info() or {}
        raise Skip(f"GPU lease held by {held.get('holder', '?')} (pid {held.get('pid', '?')})")
    try:
        body = {"model": model, "stream": False,
                # Same num_ctx as Denisse, so the loaded model is reused rather than reloaded.
                "options": {"temperature": load()["ollama"]["temperature"], "num_ctx": ctx.cfg["local_team_num_ctx"]},
                "messages": [{"role": "system", "content": role.prompt},
                             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]}
        if ctx.cfg["local_team_think"] is not None:
            body["think"] = ctx.cfg["local_team_think"]

        def send(**request_body):
            req = urllib.request.Request(ctx.ollama + "/api/chat", data=json.dumps(request_body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=ctx.cfg["local_team_timeout_s"]) as r:
                return json.loads(r.read().decode("utf-8"))

        try:
            data = call_with_schema(send, schema=role.schema,
                                    enabled=ctx.cfg["local_team_structured_output"], **body)
        except (OSError, ValueError) as e:
            raise Skip(f"Ollama call failed: {e}")
    finally:
        lease.release()
        global _last_call_end
        _last_call_end = time.monotonic()
    content = (data.get("message") or {}).get("content", "")
    try:
        out = json.loads(content)
    except ValueError:
        raise BadOutput(f"{role.name}: answer is not JSON: {content[:load()["errors"]["preview_chars"]]!r}")
    errs = validate(out, role.schema)
    if errs:
        raise BadOutput(f"{role.name}: " + "; ".join(errs[:load()["errors"]["schema_count"]]))
    return out
