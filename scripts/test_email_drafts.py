"""draft_email (email_drafts.py): a draft for Chuen to send, never a send.

Fakes only: the model is a function, the clipboard is a list, Outlook is a
fake COM object, and every folder (contacts, CV, cover letters, drafts) is
a temp folder named in a temp config. His real mailbox, CV folder and
memory/ are never touched.
"""
import base64
import email
import email.policy
import json
import os
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import email_drafts  # noqa: E402

ORIGINAL = """From: Terry Example <terry@example.ac.uk>
Sent: 06 November 2026 09:12
To: Chuen
Subject: CV and cover letter meeting

Hi Chuen, could you send me your CV and cover letter before we meet on Thursday?
"""


class FakeWriter:
    def __init__(self, body="Dear Terry,\n\nThanks for your email. Please find them attached."):
        self.body, self.calls = body, []

    def __call__(self, system, user):
        self.calls.append((system, user))
        return self.body


class DraftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="denisse-email-")
        j = lambda *p: os.path.join(self.tmp, *p)  # noqa: E731
        for d in ("cv", "out", "out/letters", "drafts"):
            os.makedirs(j(d), exist_ok=True)
        self._file(j("cv", "CV_old.pdf"), age=100)
        self._file(j("cv", "CV_new.pdf"), age=10)
        self._file(j("cv", "CV_newest.docx"), age=1)   # a later pattern: pdf wins
        self._file(j("out", "letters", "Cover_Letter_UROP.pdf"), age=5)
        with open(j("contacts.json"), "w", encoding="utf-8") as f:
            json.dump([{"name": "Terry Example", "address": "terry@example.ac.uk",
                        "aliases": ["personal tutor", "Prof Terry"]},
                       {"name": "Sam One", "address": "sam1@example.com", "aliases": ["Sam"]},
                       {"name": "Sam Two", "address": "sam2@example.com", "aliases": ["Sam"]}], f)
        with open(email_drafts.DEFAULTS_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["recipients"]["contacts_file"] = j("contacts.json")
        cfg["eml"]["drafts_folder"] = j("drafts")
        cfg["attachments"]["kinds"][0]["folder"] = j("cv")
        cfg["attachments"]["kinds"][1]["folder"] = j("out")
        cfg["graph"]["token_cache"] = j("graph_cache.bin")   # never his real sign-in
        with open(j("cfg.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        email_drafts.configure(j("cfg.json"))
        self.clip = []
        self.j = j

    def tearDown(self):
        email_drafts.configure()

    def _file(self, path, age):
        with open(path, "wb") as f:
            f.write(b"%PDF fake")
        t = time.time() - age
        os.utime(path, (t, t))

    def draft(self, writer=None, backends=None, **kw):
        writer = writer or FakeWriter()
        out = email_drafts.draft_email(writer=writer, clipboard=self.clip.append,
                                       backends=backends or {"eml_file": email_drafts._save_eml},
                                       preferences="", **kw)
        return out, writer

    # -- recipients: what he said, or the email replied to; never guessed --
    def test_recipient_address_and_contact(self):
        got, ask = email_drafts.resolve_recipients("my personal tutor")
        self.assertIsNone(ask)
        self.assertEqual(got, ["Terry Example <terry@example.ac.uk>"])
        got, ask = email_drafts.resolve_recipients("Prof X <x@example.org>, prof terry")
        self.assertIsNone(ask)
        self.assertEqual(got, ["Prof X <x@example.org>", "Terry Example <terry@example.ac.uk>"])

    def test_unknown_or_ambiguous_recipient_asks(self):
        got, ask = email_drafts.resolve_recipients("my supervisor")
        self.assertEqual(got, [])
        self.assertTrue(ask.startswith("Refused"))
        self.assertIn("my supervisor", ask)
        got, ask = email_drafts.resolve_recipients("Sam")
        self.assertEqual(got, [])
        self.assertIn("sam1@example.com", ask)
        self.assertIn("sam2@example.com", ask)

    def test_reply_recipient_comes_from_the_email(self):
        got, ask = email_drafts.resolve_recipients("", ORIGINAL)
        self.assertIsNone(ask)
        self.assertEqual(got, ["Terry Example <terry@example.ac.uk>"])
        got, ask = email_drafts.resolve_recipients("", "no headers, just text")
        self.assertEqual(got, [])
        self.assertTrue(ask.startswith("Refused"))

    # -- attachments in plain words --
    def test_latest_cv_and_cover_letter(self):
        got, ask = email_drafts.resolve_attachments(["my latest CV", "my cover letter"])
        self.assertIsNone(ask)
        self.assertEqual([os.path.basename(p) for p in got], ["CV_new.pdf", "Cover_Letter_UROP.pdf"])
        got, ask = email_drafts.resolve_attachments("my CV and cover letter")
        self.assertEqual(len(got), 2)

    def test_unknown_attachment_asks(self):
        got, ask = email_drafts.resolve_attachments(["the slides from Tuesday"])
        self.assertEqual(got, [])
        self.assertIn("the slides from Tuesday", ask)

    def test_full_path_attachment(self):
        p = self.j("cv", "CV_old.pdf")
        got, ask = email_drafts.resolve_attachments([p])
        self.assertEqual(got, [p])

    def test_missing_kind_asks(self):
        for f in os.listdir(self.j("out", "letters")):
            os.remove(self.j("out", "letters", f))
        got, ask = email_drafts.resolve_attachments(["cover letter"])
        self.assertEqual(got, [])
        self.assertIn("cover letter", ask)

    # -- the whole tool --
    def test_reply_draft_as_eml_never_sent(self):
        out, writer = self.draft(gist="say thanks, here they are, see you Thursday",
                                 replying_to=ORIGINAL, attachments=["my latest CV", "my cover letter"])
        self.assertTrue(out.startswith("Draft ready"), out)
        self.assertIn("Nothing was sent", out)
        self.assertIn("clipboard", out)
        files = os.listdir(self.j("drafts"))
        self.assertEqual(len(files), 1)
        with open(self.j("drafts", files[0]), "rb") as f:
            msg = email.message_from_bytes(f.read(), policy=email.policy.default)
        self.assertEqual(msg["X-Unsent"], "1")
        self.assertEqual(msg["Subject"], "Re: CV and cover letter meeting")
        self.assertIn("terry@example.ac.uk", msg["To"])
        names = sorted(p.get_filename() for p in msg.iter_attachments())
        self.assertEqual(names, ["CV_new.pdf", "Cover_Letter_UROP.pdf"])
        body = msg.get_body(("plain",)).get_content()
        self.assertTrue(body.rstrip().endswith(email_drafts.CONFIG["writing"]["sign_off"]))
        self.assertEqual(len(self.clip), 1)
        self.assertIn("Subject: Re: CV and cover letter meeting", self.clip[0])
        system, user = writer.calls[0]
        self.assertIn("say thanks", user)
        self.assertIn("CV and cover letter before we meet", user)

    def test_voice_and_preferences_reach_the_writer(self):
        writer = FakeWriter("Hi,\n\nThanks.\n\nBest,\nChuen")
        body = email_drafts.compose(["a@example.com"], "thanks", "", [], writer=writer,
                                    preferences="Standing instructions: keep it short")
        system, _ = writer.calls[0]
        for line in email_drafts.CONFIG["writing"]["voice"]:
            self.assertIn(line, system)
        self.assertIn("keep it short", system)
        self.assertEqual(body.count("Chuen"), 1)  # already signed: not signed twice

    def test_questions_before_any_draft(self):
        out, w = self.draft(to="my supervisor", subject="hi", gist="hello")
        self.assertTrue(out.startswith("Refused"))
        out2, _ = self.draft(to="a@example.com", gist="hello")
        self.assertTrue(out2.startswith("Refused"))  # no subject
        out3, _ = self.draft(to="a@example.com", subject="hi", gist="",)
        self.assertTrue(out3.startswith("Refused"))  # nothing to say
        self.assertEqual(w.calls, [])
        self.assertEqual(os.listdir(self.j("drafts")), [])
        self.assertEqual(self.clip, [])

    def test_outlook_saves_and_never_sends(self):
        made = []

        class Item:
            def __init__(self):
                self.Attachments = types.SimpleNamespace(added=[])
                self.Attachments.Add = self.Attachments.added.append
                self.saved = False
                made.append(self)

            def Save(self):
                self.saved = True

            def Send(self):
                raise AssertionError("draft_email must never send")

        class App:
            def CreateItem(self, kind):
                return Item()

        client = types.ModuleType("win32com.client")
        client.Dispatch = lambda progid: App()
        pkg = types.ModuleType("win32com")
        pkg.client = client
        with patch.dict(sys.modules, {"win32com": pkg, "win32com.client": client}):
            out, _ = self.draft(to="my personal tutor", subject="CV", gist="here is my CV",
                                attachments=["my CV"], backends=email_drafts.BACKENDS)
        self.assertTrue(out.startswith("Draft ready"), out)
        self.assertIn(email_drafts.CONFIG["backends"]["outlook_where"], out)
        self.assertTrue(made[0].saved)
        self.assertEqual([os.path.basename(p) for p in made[0].Attachments.added], ["CV_new.pdf"])
        self.assertEqual(os.listdir(self.j("drafts")), [])

    def test_outlook_missing_falls_back_to_eml(self):
        def no_outlook(draft):
            raise OSError("Invalid class string")
        out, _ = self.draft(to="a@example.com", subject="hi", gist="hello",
                            backends={"outlook_desktop": no_outlook, "eml_file": email_drafts._save_eml})
        self.assertTrue(out.startswith("Draft ready"), out)
        self.assertEqual(len(os.listdir(self.j("drafts"))), 1)


class FakeResp:
    def __init__(self, status=201, body=None):
        self.status_code, self._body = status, body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeHttp:
    """Records every Graph call; refuses anything that would send."""

    def __init__(self, create_status=201):
        self.calls, self.create_status = [], create_status

    def _check(self, url):
        if "send" in url.rsplit("/", 1)[-1].lower():
            raise AssertionError("draft_email must never send: " + url)

    def post(self, url, headers=None, json=None, timeout=None):
        self._check(url)
        self.calls.append(("POST", url, headers, json))
        if url.endswith("createUploadSession"):
            return FakeResp(201, {"uploadUrl": "https://upload.example/session"})
        return FakeResp(self.create_status, {"id": "MSG1"} if self.create_status < 400
                        else {"error": {"code": "ErrorAccessDenied"}})

    def put(self, url, data=None, headers=None, timeout=None):
        self._check(url)
        self.calls.append(("PUT", url, headers, data))
        return FakeResp(200)


class FakeApp:
    def __init__(self, accounts=({"username": "chuen@example.ac.uk"},), token="TOKEN"):
        self.accounts, self.token = list(accounts), token

    def get_accounts(self):
        return self.accounts

    def acquire_token_silent(self, scopes, account=None):
        self.scopes = scopes
        return {"access_token": self.token} if self.token else None


class GraphTest(unittest.TestCase):
    """The Microsoft Graph backend: a draft in his mailbox, never a send, never a sign-in."""

    setUp, tearDown, _file, draft = DraftTest.setUp, DraftTest.tearDown, DraftTest._file, DraftTest.draft

    def graph(self, app=None, http=None):
        http = http or FakeHttp()
        backends = {"graph": lambda d: email_drafts._save_graph(d, app=app or FakeApp(), http=http),
                    "eml_file": email_drafts._save_eml}
        return backends, http

    def test_draft_lands_in_drafts_with_attachments(self):
        backends, http = self.graph()
        out, _ = self.draft(to="my personal tutor", subject="CV", gist="here is my CV",
                            attachments=["my CV"], backends=backends)
        self.assertTrue(out.startswith("Draft ready"), out)
        self.assertIn("chuen@example.ac.uk", out)
        self.assertEqual(os.listdir(self.j("drafts")), [])
        [(verb, url, headers, msg)] = http.calls
        g = email_drafts.CONFIG["graph"]
        self.assertEqual((verb, url), ("POST", g["api"] + g["create_path"]))
        self.assertEqual(headers["Authorization"], "Bearer TOKEN")
        self.assertEqual(msg["subject"], "CV")
        self.assertEqual(msg["toRecipients"],
                         [{"emailAddress": {"name": "Terry Example", "address": "terry@example.ac.uk"}}])
        self.assertIn("Chuen", msg["body"]["content"])
        [att] = msg["attachments"]
        self.assertEqual(att["name"], "CV_new.pdf")
        self.assertEqual(base64.b64decode(att["contentBytes"]), b"%PDF fake")

    def test_big_attachment_goes_through_an_upload_session(self):
        with open(self.j("cfg.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["graph"]["max_inline_attachment_bytes"] = 1
        cfg["graph"]["upload_chunk_bytes"] = 4
        with open(self.j("cfg.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        email_drafts.configure(self.j("cfg.json"))
        backends, http = self.graph()
        out, _ = self.draft(to="a@example.com", subject="CV", gist="cv attached",
                            attachments=["my CV"], backends=backends)
        self.assertTrue(out.startswith("Draft ready"), out)
        self.assertNotIn("attachments", http.calls[0][3])
        self.assertTrue(http.calls[1][1].endswith("/me/messages/MSG1/attachments/createUploadSession"))
        puts = [c for c in http.calls if c[0] == "PUT"]
        self.assertEqual(b"".join(c[3] for c in puts), b"%PDF fake")
        self.assertEqual([c[2]["Content-Range"] for c in puts],
                         ["bytes 0-3/9", "bytes 4-7/9", "bytes 8-8/9"])
        self.assertTrue(all("Authorization" not in c[2] for c in puts))

    def test_not_signed_in_falls_back_without_network(self):
        http = FakeHttp()
        backends = {"graph": lambda d: email_drafts._save_graph(d, http=http),
                    "eml_file": email_drafts._save_eml}
        with patch.object(email_drafts, "graph_app", side_effect=AssertionError("no app, no network")):
            out, _ = self.draft(to="a@example.com", subject="hi", gist="hello", backends=backends)
        self.assertTrue(out.startswith("Draft ready"), out)
        self.assertEqual(http.calls, [])
        self.assertEqual(len(os.listdir(self.j("drafts"))), 1)

    def test_expired_or_refused_falls_back_to_eml(self):
        for n, (app, http) in enumerate(((FakeApp(token=None), FakeHttp()), (FakeApp(accounts=()), FakeHttp()),
                                         (FakeApp(), FakeHttp(create_status=403)))):
            backends, _ = self.graph(app=app, http=http)
            out, _ = self.draft(to="a@example.com", subject=f"hi {n}", gist="hello", backends=backends)
            self.assertTrue(out.startswith("Draft ready"), out)
            self.assertNotIn("chuen@example.ac.uk", out)
        self.assertEqual(len(os.listdir(self.j("drafts"))), 3)

    def test_never_asks_to_send(self):
        app = FakeApp()
        backends, _ = self.graph(app=app)
        self.draft(to="a@example.com", subject="hi", gist="hello", backends=backends)
        self.assertNotIn("mail.send", [s.lower() for s in app.scopes])
        self.assertEqual(email_drafts.CONFIG["backends"]["order"][0], "graph")

    def test_sign_in_script_saves_the_cache(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import graph_signin

        class Cache:
            has_state_changed = True

            def serialize(self):
                return '{"Account": {}}'

        class App:
            def initiate_device_flow(self, scopes, **kw):
                self.scopes = scopes
                return {"user_code": "ABC", "message": "go to the page and type ABC"}

            def acquire_token_by_device_flow(self, flow):
                return {"access_token": "T", "id_token_claims": {"preferred_username": "c@example.ac.uk"}}

        said, app = [], App()
        self.assertEqual(graph_signin.sign_in(out=said.append, app=app, cache=Cache()), "c@example.ac.uk")
        self.assertEqual(said, ["go to the page and type ABC"])
        self.assertNotIn("mail.send", [s.lower() for s in app.scopes])
        path = email_drafts._cache_path()
        self.assertTrue(path.startswith(self.tmp) and os.path.isfile(path))
        with open(path, "rb") as f:
            self.assertEqual(email_drafts._protect(f.read(), unprotect=True), b'{"Account": {}}')
        self.assertTrue(graph_signin.sign_out())
        self.assertFalse(os.path.exists(path))


class PolicyTest(unittest.TestCase):
    """Wired in as a write tool he confirms, offered only for email requests."""

    def test_registered_gated_and_offered(self):
        import brain
        import safety
        import tool_policy
        import tool_router
        self.assertIn("draft_email", brain.tools.TOOL_FUNCTIONS)
        self.assertEqual(tool_policy.side_effect("draft_email"), "write")
        self.assertIn("draft_email", safety.RISKY_TOOLS)

        def offered(text):
            return {d["function"]["name"] for d in tool_router.select(
                brain.TOOL_DEFINITIONS, [{"role": "user", "content": text}])}
        for text in ("send my CV and cover letter to my personal tutor",
                     "draft a reply to this email"):
            self.assertIn("draft_email", offered(text), text)
        for text in ("pull up my latest cv", "what's the weather tomorrow"):
            self.assertNotIn("draft_email", offered(text), text)
        # Writing the cover letter itself is a hand-off the model may choose
        # and he confirms (the delegate tool, 13 Nov 2026), not a draft.
        self.assertIn("delegate", offered("could you write me a cover letter for JP Morgan London"))


if __name__ == "__main__":
    unittest.main()
