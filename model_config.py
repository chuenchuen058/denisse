"""Which models Denisse uses, read from config.json (AGENTS.md Rule 0).

    model_config.model("chat")     # the Ollama model, DENISSE_MODEL if set
    model_config.model("whisper")  # the faster-whisper size, DENISSE_WHISPER_MODEL if set

Each entry in config.json's "models" names its own env override; a set,
non-empty env var wins, as it did before this file existed. DENISSE_CONFIG
points at another config file (tests, a private tweak).
"""
import functools
import json
import os
from pathlib import Path


def config_path() -> Path:
    return Path(os.environ.get("DENISSE_CONFIG") or Path(__file__).with_name("config.json"))


@functools.lru_cache(maxsize=None)
def _models(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as f:
        return {k: v for k, v in json.load(f).get("models", {}).items() if not k.startswith("_")}


def entry(name: str) -> dict:
    """The raw config entry for one model: {"why", "env", "value"}."""
    return _models(str(config_path()))[name]


def model(name: str) -> str:
    """The model to use for `name`: its env override if set, else config.json's value."""
    e = entry(name)
    env = e.get("env")
    return (os.environ.get(env) if env else None) or e["value"]
