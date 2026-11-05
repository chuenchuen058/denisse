"""Text out of a KEATS file, with nothing installed for it.

PDF through the installed PyMuPDF (fitz), falling back to pypdf; PPTX and
DOCX by reading their XML straight out of the zip (no python-pptx or
python-docx here, and no pip); TXT and MD as they are. Audio only when
tutor.json says audio.enabled, through the installed faster_whisper.

    extract.read(path)  -> (text, why)   why is '' on success, else the
                                          configured reason it couldn't be read
    extract.sha256(path)
"""
from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
import zipfile

from .conf import CONFIG

_E = CONFIG["extract"]
_REASONS = CONFIG["match"]["unmatched_reasons"]


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def kind(path: str) -> str:
    """'pdf' / 'pptx' / 'docx' / 'text' / 'audio' / ''."""
    ext = os.path.splitext(path)[1].lower()
    for name in ("pdf", "pptx", "docx", "text"):
        if ext in _E[name]:
            return name
    if ext in _E["audio"]["extensions"]:
        return "audio"
    return ""


def wanted(name: str) -> bool:
    return not name.startswith(tuple(_E["skip_prefixes"]))


# -- readers -----------------------------------------------------------------

def _pdf(path: str) -> str:
    pages = []
    try:
        import fitz  # PyMuPDF
        with fitz.open(path) as doc:
            for n, page in enumerate(doc, 1):
                pages.append((n, page.get_text()))
    except Exception:  # noqa: BLE001 -- fitz missing or choked: pypdf has a go
        pages = []
    if not any(t.strip() for _, t in pages):
        from pypdf import PdfReader
        pages = [(n, page.extract_text() or "") for n, page in enumerate(PdfReader(path).pages, 1)]
    return "\n".join(_E["page_heading"].format(n=n) + "\n" + t.strip() for n, t in pages if t.strip())


def _xml_text(data: bytes) -> str:
    """Paragraph-per-line text from an Office XML part: every <a:t>/<w:t>
    run, joined inside each <a:p>/<w:p>."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ""
    # ElementTree gives '{namespace}t'; the local name is enough to tell a
    # text run from a paragraph in both the DrawingML (a:) and WordprocessingML (w:) families.
    text_tags = {t.split(":")[-1] for t in _E["xml_text_tags"]}
    para_tags = {t.split(":")[-1] for t in _E["xml_paragraph_tags"]}

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    lines = []
    for para in root.iter():
        if local(para.tag) in para_tags:
            line = "".join(el.text or "" for el in para.iter() if local(el.tag) in text_tags).strip()
            if line:
                lines.append(line)
    return "\n".join(lines)


def _numbered(names: list, prefix: str) -> list:
    """Zip members 'ppt/slides/slide12.xml' sorted by their number."""
    out = []
    for name in names:
        if name.startswith(prefix) and name.endswith(".xml"):
            m = re.search(r"(\d+)\.xml$", name)
            if m:
                out.append((int(m.group(1)), name))
    return [name for _, name in sorted(out)]


def _pptx(path: str) -> str:
    parts = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        notes = {}
        for name in _numbered(names, _E["pptx_notes_glob"]):
            n = int(re.search(r"(\d+)\.xml$", name).group(1))
            notes[n] = _xml_text(z.read(name))
        for name in _numbered(names, _E["pptx_slide_glob"]):
            n = int(re.search(r"(\d+)\.xml$", name).group(1))
            body = _xml_text(z.read(name))
            if notes.get(n):
                body += "\n" + notes[n]
            if body.strip():
                parts.append(_E["slide_heading"].format(n=n) + "\n" + body.strip())
    return "\n".join(parts)


def _docx(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        return _xml_text(z.read(_E["docx_document"]))


def _text(path: str) -> str:
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        return f.read()


def _audio(path: str) -> str:
    import model_config
    from faster_whisper import WhisperModel
    model = WhisperModel(model_config.model(_E["audio"]["config_entry"]))
    segments, _info = model.transcribe(path)
    return "\n".join(s.text.strip() for s in segments if s.text.strip())


def read(path: str) -> tuple:
    """(text, why). why is '' when text was read, else the reason from
    tutor.json match.unmatched_reasons."""
    k = kind(path)
    if not k:
        return "", _REASONS["unknown_type"]
    if k == "audio" and not _E["audio"]["enabled"]:
        return "", _REASONS["audio_off"]
    reader = {"pdf": _pdf, "pptx": _pptx, "docx": _docx, "text": _text, "audio": _audio}[k]
    try:
        text = reader(path)
    except Exception as exc:  # noqa: BLE001 -- a broken file is recorded, never fatal
        return "", f"{_REASONS['empty']} ({type(exc).__name__}: {exc})"
    text = re.sub(r"[ \t]+\n", "\n", text or "").strip()
    if len(text) < _E["min_chars"]:
        return "", _REASONS["empty"]
    return text, ""
