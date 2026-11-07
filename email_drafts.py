"""
Email drafts: "draft a reply to this email", "send my CV and cover letter
to my personal tutor". Denisse prepares the draft; Chuen presses Send.

Hard rule: nothing here sends. Every backend only SAVES a draft -- classic
Outlook's Drafts folder when its COM object exists, else a .eml file that
Outlook opens as an unsent message, plus the text on the clipboard. No
password, token or sign-in is entered anywhere. (On 2026-11-07 this
machine had only the new Outlook app: no COM, no local automation; a
real mailbox draft there needs a Microsoft Graph sign-in, which is his.)

What the draft holds is never guessed:
  - recipients come from what he said (an address, or a saved contact's
    name) or from the email being replied to; otherwise she asks;
  - attachments named in plain words ("my latest CV") resolve to the newest
    file of that kind in the folder email_drafts.json sets; otherwise she
    asks;
  - the body is written by the local model in his voice (writing.voice
    plus his saved reply preferences), from his gist and nothing else.

Every folder, pattern, word and sentence is email_drafts.json (AGENTS.md
Rule 0). Tests swap the model, the clipboard and the backends for fakes
(`writer=`, `clipboard=`, `backends=`), so his real mailbox is never
touched. DENISSE_EMAIL_DRAFTS_CONFIG points at another config file.
"""
from __future__ import annotations

import datetime
import email.utils
import fnmatch
import json
import logging
import mimetypes
import os
import re
import subprocess
from email.message import EmailMessage

import json_config

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(ROOT, "email_drafts.json")
DEFAULTS_FILE = os.path.join(ROOT, "email_drafts.defaults.json")
CONFIG_ENV_VAR = "DENISSE_EMAIL_DRAFTS_CONFIG"
CONFIG: dict = {}
_WORD = re.compile(r"[\w'À-ɏ]+")


def configure(path=None) -> dict:
    global CONFIG
    path = path or os.environ.get(CONFIG_ENV_VAR) or CONFIG_FILE
    CONFIG = json_config.load("email_drafts", path, DEFAULTS_FILE)
    return CONFIG


configure()


def _w(key: str, **fields) -> str:
    return CONFIG["wording"][key].format(**fields)


def _join(items) -> str:
    return CONFIG["wording"]["list_joiner"].join(items)


def _path(p: str) -> str:
    """A config path: ~ is his home folder, a relative one is under the repo."""
    p = os.path.expanduser(p or "")
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def _words(text: str) -> list:
    return _WORD.findall((text or "").lower())


# -- recipients ---------------------------------------------------------------

def _contacts() -> list:
    try:
        with open(_path(CONFIG["recipients"]["contacts_file"]), encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return [c for c in data if isinstance(c, dict) and "@" in str(c.get("address", ""))] \
        if isinstance(data, list) else []


def _addresses(text: str) -> list:
    """Every real address in `text`, as 'Name <addr>' or 'addr'."""
    out = []
    for name, addr in email.utils.getaddresses([text or ""]):
        if re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+", addr or ""):
            out.append(email.utils.formataddr((name, addr)) if name else addr)
    return out


def _header(original: str, name: str) -> str:
    m = re.search(rf"^\s*{re.escape(name)}\s*:\s*(.+)$", original or "", re.I | re.M)
    return m.group(1).strip() if m else ""


def resolve_recipients(to: str, replying_to: str = "") -> tuple[list, str | None]:
    """(addresses, None), or ([], the question to ask him)."""
    to = (to or "").strip()
    if not to:
        for h in CONFIG["recipients"]["reply_headers"]:
            found = _addresses(_header(replying_to, h))
            if found:
                return found[:1], None
        return [], _w("ask_recipient", detail="")
    # Whole entries with an address ("Prof X <x@kcl.ac.uk>", "x@kcl.ac.uk")
    # are taken as they are; what is left is names, looked up in contacts.
    entry = re.compile(r'(?:"[^"]*"|[^,;<>"]*)<[^<>\s]+@[^<>\s]+>|[^\s<>,;"]+@[^\s<>,;"]+')
    found = [a for e in entry.findall(to) for a in _addresses(e)]
    rest = entry.sub(" ", to)
    filler = {w.lower() for w in CONFIG["recipients"]["filler_words"]}
    parts = [p for p in re.split(CONFIG["recipients"]["separators"], rest, flags=re.I)
             if set(_words(p)) - filler]
    for part in parts:
        want = [w for w in _words(part) if w not in filler]
        hits = [c for c in _contacts()
                if any([w for w in _words(n) if w not in filler] == want
                       for n in [c.get("name", "")] + list(c.get("aliases") or []))]
        if not hits:
            return [], _w("ask_recipient", detail=_w("ask_recipient_detail", value=part.strip()))
        if len({c["address"].lower() for c in hits}) > 1:
            return [], _w("ask_recipient_ambiguous", value=part.strip(),
                          options=_join(email.utils.formataddr((c.get("name", ""), c["address"]))
                                        for c in hits))
        c = hits[0]
        found.append(email.utils.formataddr((c.get("name", ""), c["address"])))
    return found, None


# -- attachments --------------------------------------------------------------

def _newest(kind: dict) -> str | None:
    folder = _path(kind["folder"])
    for pattern in kind["patterns"]:
        hits = []
        for dirpath, dirnames, files in os.walk(folder):
            hits += [os.path.join(dirpath, f) for f in files if fnmatch.fnmatch(f.lower(), pattern.lower())]
            if not kind.get("recursive"):
                break
        if hits:
            return max(hits, key=os.path.getmtime)
    return None


def _kinds_in(phrase: str) -> list:
    text = " " + " ".join(_words(phrase)) + " "
    return [k for k in CONFIG["attachments"]["kinds"]
            if any(" " + " ".join(_words(w)) + " " in text for w in k["words"])]


def resolve_attachments(named) -> tuple[list, str | None]:
    """(full paths, None), or ([], the question to ask him)."""
    if isinstance(named, str):
        named = [named] if named.strip() else []
    paths = []
    for phrase in named or []:
        phrase = str(phrase).strip().strip('"')
        if not phrase:
            continue
        if os.path.isabs(phrase) and os.path.isfile(phrase):
            paths.append(phrase)
            continue
        kinds = _kinds_in(phrase)
        if not kinds:
            return [], _w("ask_attachment", value=phrase)
        for kind in kinds:
            if not (kind.get("folder") or "").strip():
                return [], _w("folder_not_set", kind=kind["name"])
            got = _newest(kind)
            if got is None:
                return [], _w("missing_attachment", kind=kind["name"], folder=_path(kind["folder"]),
                              patterns=_join(kind["patterns"]))
            if got not in paths:
                paths.append(got)
    return paths, None


# -- wording ------------------------------------------------------------------

def _preferences() -> str:
    try:
        import reply_preferences
        return reply_preferences.prompt_block()
    except Exception:
        return ""


def _ollama_writer(system: str, user: str) -> str:
    import ollama
    import model_config
    r = ollama.chat(model=model_config.model("chat"),
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}])
    return r["message"]["content"]


def compose(to: list, gist: str, replying_to: str, attachments: list, writer=None,
            preferences=None) -> str:
    wr = CONFIG["writing"]
    system = "\n".join(wr["voice"])
    prefs = _preferences() if preferences is None else preferences
    if prefs:
        system += "\n\n" + prefs
    original = (replying_to or "").strip()[:wr["max_reply_chars"]]
    user = wr["request"].format(
        to=_join(to), gist=(gist or "").strip(),
        attachments=_join(os.path.basename(p) for p in attachments) or wr["no_attachments"],
        reply_block=wr["reply_block"].format(original=original) if original else "")
    body = (writer or _ollama_writer)(system, user.strip()).strip()
    last = body.splitlines()[-1].strip() if body else ""
    if wr["name"].lower() not in last.lower():
        body = body + "\n\n" + wr["sign_off"]
    return body


# -- saving (never sending) ---------------------------------------------------

def _save_outlook(draft: dict) -> str:
    """Classic Outlook: CreateItem + Save puts it in Drafts. Send is never called."""
    import win32com.client  # pywin32; absent or unregistered -> the next backend
    b = CONFIG["backends"]
    app = win32com.client.Dispatch(b["outlook_progid"])
    item = app.CreateItem(b["outlook_mail_item"])
    item.To = "; ".join(draft["to"])
    item.Subject = draft["subject"]
    item.Body = draft["body"]
    for p in draft["attachments"]:
        item.Attachments.Add(p)
    item.Save()
    return b["outlook_where"]


def _file_name(subject: str) -> str:
    e = CONFIG["eml"]
    bad = set(e["unsafe_name_chars"])
    clean = "".join(ch for ch in subject if ch not in bad and ch.isprintable()).strip()[:e["max_name_chars"]]
    return e["file_name"].format(date=datetime.datetime.now().strftime(e["date_format"]),
                                 subject=clean.strip(" .") or "draft")


def build_eml(draft: dict) -> EmailMessage:
    msg = EmailMessage()
    for k, v in CONFIG["eml"]["extra_headers"].items():
        msg[k] = v
    msg["To"] = ", ".join(draft["to"])
    msg["Subject"] = draft["subject"]
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg.set_content(draft["body"])
    for p in draft["attachments"]:
        ctype, _ = mimetypes.guess_type(p)
        main, sub = (ctype or "application/octet-stream").split("/", 1)
        with open(p, "rb") as f:
            msg.add_attachment(f.read(), maintype=main, subtype=sub, filename=os.path.basename(p))
    return msg


def _save_eml(draft: dict) -> str:
    folder = _path(CONFIG["eml"]["drafts_folder"])
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, _file_name(draft["subject"]))
    with open(path, "wb") as f:
        f.write(bytes(build_eml(draft)))
    return CONFIG["eml"]["where"].format(path=path)


BACKENDS = {"outlook_desktop": _save_outlook, "eml_file": _save_eml}


def _clipboard(text: str) -> None:
    c = CONFIG["clipboard"]
    subprocess.run(c["command"], input=text.encode(c["encoding"]), check=True,
                   timeout=c["timeout_seconds"])


# -- the tool -----------------------------------------------------------------

def draft_email(to: str = "", subject: str = "", gist: str = "", replying_to: str = "",
                attachments=None, *, writer=None, clipboard=None, backends=None,
                preferences=None) -> str:
    """Prepare a draft and say where it is. Never sends."""
    recipients, ask = resolve_recipients(to, replying_to)
    if ask:
        return ask
    subject = (subject or "").strip()
    if not subject and replying_to:
        orig = _header(replying_to, CONFIG["writing"]["subject_header"])
        prefix = CONFIG["writing"]["reply_prefix"]
        if orig:
            subject = orig if orig.lower().startswith(prefix.strip().lower()) else prefix + orig
    if not subject:
        return _w("ask_subject")
    if not (gist or "").strip() and not (replying_to or "").strip():
        return _w("ask_gist")
    files, ask = resolve_attachments(attachments)
    if ask:
        return ask
    try:
        body = compose(recipients, gist, replying_to, files, writer=writer, preferences=preferences)
    except Exception as exc:
        return _w("writer_failed", error=exc)
    draft = {"to": recipients, "subject": subject, "body": body, "attachments": files}
    table = BACKENDS if backends is None else backends
    errors, where = [], None
    for name in CONFIG["backends"]["order"]:
        save = table.get(name)
        if save is None:
            continue
        try:
            where = save(draft)
            break
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            logging.info("email draft backend %s unavailable: %s", name, exc)
    if where is None:
        return _w("save_failed", error="; ".join(errors) or "no backend")
    copied = ""
    if CONFIG["clipboard"]["enabled"]:
        try:
            (clipboard or _clipboard)(CONFIG["clipboard"]["template"].format(
                to=", ".join(recipients), subject=subject, body=body))
            copied = _w("on_clipboard")
        except Exception as exc:
            logging.info("email draft: clipboard copy failed: %s", exc)
    return _w("ready", where=where, clipboard=copied, to=", ".join(recipients), subject=subject,
              attachments=_join(os.path.basename(p) for p in files) or CONFIG["writing"]["no_attachments"])
