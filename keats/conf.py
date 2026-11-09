"""Fresh config and runtime paths. Importing this package never opens the vault."""
import json
import os
from pathlib import Path


def load():
    source = Path(os.environ.get("KEATS_CONFIG") or Path(__file__).with_name("config.json"))
    return json.loads(source.read_text(encoding="utf-8-sig"))


def profile(config):
    value = os.path.expandvars(config["paths"]["profile"])
    if "%" in value:
        raise ValueError("Profile environment variable is unset: " + value)
    return Path(value).expanduser().resolve()


def vault():
    import settings
    return Path(settings.vault()).resolve()


def inside(root, relative, config):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    parts = path.relative_to(root).parts
    if not parts or set(parts) & set(config["paths"]["never_write"]):
        raise ValueError("Unsafe vault path: " + str(relative))
    return path
