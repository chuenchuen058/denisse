"""Local page OCR, with a content-addressed sidecar in the tutor's data area.

The engine is lazy: normal PDFs never start it. A session logs unavailability
once, keeps native text, and leaves failures retryable on the next ingest.
Tests inject an engine factory; neither a vault nor Windows is required.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from .conf import CONFIG


def _signature(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


class WindowsEngine:
    """Windows.Media.Ocr via the installed Windows PowerShell WinRT bridge.

    PNG bytes travel over stdin, not command arguments or temporary images.
    The helper returns the actual language and supported image size.
    """

    def __init__(self, config):
        self.config = config
        info = self._invoke({})
        self.language = info["language"]
        self.max_dimension = info["max_dimension"]

    def _invoke(self, payload):
        bridge = self.config["windows"]
        script = str(Path(__file__).with_name(bridge["script"]))
        command = [part.format(script=script) for part in bridge["command"]]
        payload.update(language=self.config["language"], errors=self.config["errors"])
        result = subprocess.run(
            command, input=json.dumps(payload), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=bridge["timeout_seconds"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        result.check_returncode()
        data = json.loads(result.stdout)
        if data.get("error"):
            raise RuntimeError(data["error"])
        return data

    def recognize(self, png: bytes) -> str:
        return self._invoke({"image": base64.b64encode(png).decode("ascii")})["text"]


def create_engine(config):
    return {"windows": WindowsEngine}[config["engine"]](config)


class Session:
    def __init__(self, cache_dir=None, *, config=None, say=print, write=True, factory=None):
        self.config = CONFIG["extract"]["ocr"] if config is None else config
        self.settings = _signature(self.config)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.say, self.write = say, write
        self.factory = create_engine if factory is None else factory
        self.engine = None
        self.disabled = False
        self.records = {}
        self.touched = []
        self._cache_warned = False

    def unavailable(self, exc):
        if not self.disabled and self.config["enabled"]:
            self.say(self.config["say"]["unavailable"].format(
                engine=self.config["engine"], why=" ".join(str(exc).splitlines())))
        self.disabled = True

    def _cache_path(self, digest):
        if self.cache_dir is not None:
            return self.cache_dir / self.config["cache_file"].format(digest=digest, settings=self.settings)
        return None

    def _load(self, path, digest):
        if path is None:
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data["sha256"] != digest or data["settings"] != self.settings:
                return {}
            pages = data["pages"]
            if not isinstance(pages, dict) or any(
                not isinstance(p, dict) or not isinstance(p.get("text"), str)
                or not isinstance(p.get("language"), str) for p in pages.values()
            ):
                return {}
            return pages
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    def _save(self, path, digest, pages):
        if path is None or not self.write:
            return
        tmp = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
                tmp = Path(f.name)
                json.dump({"sha256": digest, "settings": self.settings, "ocr": self.config,
                           "pages": pages}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
            if str(path) not in self.touched:
                self.touched.append(str(path))
        except OSError as exc:
            if not self._cache_warned:
                self.say(self.config["say"]["cache_error"].format(why=exc))
                self._cache_warned = True
        finally:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass  # a cache cleanup failure must not discard page text

    def pdf(self, doc, digest):
        """Return (page number, marked text) pairs; cache successful pages,
        including blank OCR results. Failed pages never become cache hits.
        The cap counts eligible pages, so native-text pages do not consume it.
        """
        cfg = self.config
        path = self._cache_path(digest)
        cached = self._load(path, digest)
        pages, used, capped, pending = [], [], [], []
        eligible = 0
        for n, page in enumerate(doc, 1):
            native = page.get_text().strip()
            text = native
            if cfg["enabled"] and len(native) < cfg["min_text_chars"]:
                eligible += 1
                if eligible > cfg["page_cap"]:
                    capped.append(n)
                else:
                    key = str(n)
                    if key not in cached and not self.disabled:
                        try:
                            if self.engine is None:
                                self.engine = self.factory(cfg)
                            # PDF coordinates are points (72 per inch). Respect
                            # the engine's runtime limit before allocating pixels.
                            dpi = min(cfg["dpi"], int(72 * self.engine.max_dimension /
                                                     max(page.rect.width, page.rect.height)))
                            started = time.perf_counter()
                            png = page.get_pixmap(dpi=dpi, alpha=False).tobytes("png")
                            recognized = self.engine.recognize(png).strip()
                            cached[key] = {"source": "ocr", "engine": cfg["engine"],
                                           "language": self.engine.language, "dpi": dpi,
                                           "text": recognized, "native_text": native,
                                           "seconds": time.perf_counter() - started}
                            self._save(path, digest, cached)  # survive an interrupted PDF
                        except Exception as exc:  # optional OCR must not lose native text
                            self.unavailable(exc)
                    result = cached.get(key)
                    if result is None:
                        pending.append(n)
                    elif result["text"]:
                        used.append(n)
                        text = "\n".join(part for part in (
                            native, cfg["page_marker"].format(engine=cfg["engine"], language=result["language"]),
                            result["text"],
                        ) if part)
            pages.append((n, text))
        if capped:
            self.say(cfg["say"]["capped"].format(n=len(capped), cap=cfg["page_cap"]))
        self.records[digest] = {
            "settings": self.settings, "signature": _signature([pages, used, capped, pending]),
            "ocr_pages": used, "capped_pages": capped, "pending_pages": pending,
            "retry": bool(pending),
        }
        return pages
