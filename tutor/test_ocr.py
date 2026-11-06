"""Generated image-only PDFs and fake OCR; no real vault, engine or model."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import fitz

from tutor import conf, extract, ingest, ocr


LONG = "The Fourier transform represents a signal as a sum of frequency components."


def make_pdf(path, pages):
    """Each (kind, text) becomes one native or raster-only page."""
    with fitz.open() as doc:
        for kind, text in pages:
            page = doc.new_page(width=400, height=300)
            if kind == "native":
                page.insert_text((25, 50), text, fontsize=8)
            else:
                with fitz.open() as source:
                    image = source.new_page(width=400, height=300)
                    image.insert_textbox(image.rect + (25, 25, -25, -25), text, fontsize=16)
                    page.insert_image(page.rect, stream=image.get_pixmap(dpi=100).tobytes("png"))
        doc.save(path)


class FakeEngine:
    language = "en-GB"
    max_dimension = 4000

    def __init__(self, texts=None):
        self.texts = iter(texts) if texts is not None else None
        self.images = []

    def recognize(self, png):
        self.images.append(fitz.IRect(fitz.Pixmap(png).irect))
        return next(self.texts) if self.texts is not None else LONG


class OCRFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pdf = self.root / "scan.pdf"
        self.cache = self.root / "cache"
        self.cfg = copy.deepcopy(conf.CONFIG["extract"]["ocr"])
        self.said = []
        self.engine = FakeEngine()
        self.factory = Mock(return_value=self.engine)
        # A missing injection must fail a test, never launch the real engine.
        guard = patch.object(ocr, "create_engine", side_effect=AssertionError("real OCR forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def session(self, **kw):
        return ocr.Session(self.cache, config=self.cfg, factory=self.factory, say=self.said.append, **kw)

    def read(self, session=None, path=None):
        return extract.read(str(path or self.pdf), ocr_session=session or self.session())


class OCRTests(OCRFixture):
    def test_image_only_pdf_is_marked_and_cache_survives_rename(self):
        make_pdf(self.pdf, [("image", LONG)])
        with fitz.open(self.pdf) as doc:
            self.assertEqual(doc[0].get_text(), "")
        session = self.session()
        text, why = self.read(session)
        self.assertEqual(why, "")
        self.assertIn(LONG, text)
        self.assertIn("OCR:", text)
        self.assertEqual(len(self.engine.images), 1)
        sidecar = json.loads(Path(session.touched[0]).read_text(encoding="utf-8"))
        self.assertEqual(sidecar["sha256"], extract.sha256(self.pdf))
        self.assertEqual(sidecar["pages"]["1"]["source"], "ocr")
        self.assertEqual(sidecar["pages"]["1"]["language"], "en-GB")
        renamed = self.root / "renamed.pdf"
        self.pdf.rename(renamed)
        self.factory.side_effect = AssertionError("cache must avoid the engine")
        self.assertEqual(self.read(path=renamed), (text, why))

    def test_native_pages_and_disabled_ocr_never_start_engine(self):
        make_pdf(self.pdf, [("native", LONG)])
        self.assertIn(LONG, self.read()[0])
        self.factory.assert_not_called()
        self.cfg["enabled"] = False
        make_pdf(self.pdf, [("image", LONG)])
        self.assertEqual(self.read()[0], "")
        self.factory.assert_not_called()
        self.assertFalse(self.cache.exists())

    def test_threshold_is_per_page_and_strict_and_dpi_is_configured(self):
        self.cfg.update(min_text_chars=20, dpi=144)
        make_pdf(self.pdf, [("native", "a" * 20), ("native", "b" * 19)])
        text, _ = self.read()
        self.assertEqual(len(self.engine.images), 1)
        self.assertIn("a" * 20, text)
        self.assertIn("b" * 19, text)  # sparse native text is retained too
        self.assertEqual(self.engine.images[0].width, 800)

    def test_runtime_engine_size_limits_rendering(self):
        self.engine.max_dimension = 600
        self.cfg["dpi"] = 400
        make_pdf(self.pdf, [("image", LONG)])
        self.read()
        self.assertLessEqual(self.engine.images[0].width, self.engine.max_dimension)

    def test_cap_counts_sparse_pages_and_is_recorded(self):
        self.cfg["page_cap"] = 1
        make_pdf(self.pdf, [("native", LONG), ("image", LONG), ("image", LONG)])
        session = self.session()
        self.read(session)
        info = session.records[extract.sha256(self.pdf)]
        self.assertEqual(info["ocr_pages"], [2])
        self.assertEqual(info["capped_pages"], [3])
        self.assertFalse(info["retry"])
        self.assertEqual(len(self.engine.images), 1)
        self.assertIn("page cap", self.said[0])

    def test_config_or_file_changes_invalidate_cache(self):
        make_pdf(self.pdf, [("image", LONG)])
        self.read()
        for key, value in (("language", "fr"), ("dpi", 150), ("min_text_chars", 30),
                           ("page_cap", 3), ("engine", "fixture")):
            with self.subTest(key=key):
                before = len(self.engine.images)
                self.cfg[key] = value
                self.read()
                self.assertEqual(len(self.engine.images), before + 1)
        before = len(self.engine.images)
        make_pdf(self.pdf, [("image", "A changed scan")])
        self.read()
        self.assertEqual(len(self.engine.images), before + 1)

    def test_corrupt_cache_is_rebuilt(self):
        make_pdf(self.pdf, [("image", LONG)])
        session = self.session()
        self.read(session)
        path = Path(session.touched[0])
        for broken in ("{bad", "null", '{"pages": []}'):
            path.write_text(broken, encoding="utf-8")
            before = len(self.engine.images)
            self.read()
            self.assertEqual(len(self.engine.images), before + 1)

    def test_unavailable_logs_once_keeps_native_and_retries_next_run(self):
        make_pdf(self.pdf, [("native", LONG), ("image", LONG), ("image", LONG)])
        self.factory.side_effect = ImportError("no engine\navailable")
        session = self.session()
        text, why = self.read(session)
        self.assertIn(LONG, text)
        self.assertEqual(why, "")
        other = self.root / "other.pdf"
        make_pdf(other, [("image", LONG)])
        self.assertEqual(self.read(session, other)[0], "")
        self.assertEqual(len(self.said), 1)
        self.assertNotIn("\n", self.said[0])
        self.assertTrue(session.records[extract.sha256(self.pdf)]["retry"])
        self.assertFalse(self.cache.exists())
        self.factory.side_effect = None
        self.assertIn("OCR:", self.read()[0])

    def test_partial_failure_reuses_successful_pages(self):
        make_pdf(self.pdf, [("image", LONG), ("image", LONG)])
        self.engine.recognize = Mock(side_effect=[LONG, RuntimeError("recognizer failed")])
        session = self.session()
        self.read(session)
        self.assertEqual(session.records[extract.sha256(self.pdf)]["pending_pages"], [2])
        self.engine.recognize.side_effect = [LONG]
        again = self.session()
        self.read(again)
        self.assertEqual(self.engine.recognize.call_count, 3)
        self.assertFalse(again.records[extract.sha256(self.pdf)]["retry"])

    def test_blank_result_is_cached_without_inventing_text(self):
        make_pdf(self.pdf, [("image", "")])
        self.engine = FakeEngine([""])
        self.factory.return_value = self.engine
        self.assertEqual(self.read()[0], "")
        self.assertEqual(self.read()[0], "")
        self.assertEqual(len(self.engine.images), 1)

    def test_read_only_session_does_not_create_cache(self):
        make_pdf(self.pdf, [("image", LONG)])
        self.assertIn("OCR:", self.read(self.session(write=False))[0])
        self.assertFalse(self.cache.exists())

    def test_cache_write_error_preserves_ocr(self):
        make_pdf(self.pdf, [("image", LONG)])
        self.cache.write_text("not a directory", encoding="utf-8")
        self.assertIn("OCR:", self.read()[0])
        self.assertIn("cache could not be saved", self.said[0])

    def test_pypdf_fallback_when_pymupdf_is_unavailable(self):
        make_pdf(self.pdf, [("native", LONG)])
        with patch.dict("sys.modules", {"fitz": None}):
            self.assertIn(LONG, self.read()[0])
        self.assertEqual(len(self.said), 1)
        self.factory.assert_not_called()


class IngestOCRTests(OCRFixture):
    def setUp(self):
        super().setUp()
        self.paths = conf.Paths(str(self.root / "vault"), str(self.root / "state"))
        self.module = Path(self.paths.modules_dir) / "5CCYB020 Signals and Systems"
        week = self.module / "Week 4"
        week.mkdir(parents=True)
        self.pdf = week / "Lecture - Notes.pdf"
        make_pdf(self.pdf, [("image", LONG)])
        self.calls = []

    def run_ingest(self, **kw):
        def model(system, user):
            self.calls.append(user)
            return json.dumps({"outcomes": ["Read transform"], "topics": [{"name": "Fourier transform"}]})
        return ingest.run(self.paths, call=model, say=self.said.append, ocr_factory=self.factory, **kw)

    def test_old_unmatched_scan_is_recovered_and_note_marks_ocr(self):
        state = ingest.load_state(self.paths)
        digest = extract.sha256(self.pdf)
        state["files"][digest] = {"status": "unmatched", "module": None, "week": None,
                                  "why": conf.CONFIG["match"]["unmatched_reasons"]["empty"]}
        ingest.save_state(self.paths, state)
        result = self.run_ingest()
        self.assertEqual(len(result["notes"]), 1)
        note = Path(result["notes"][0]).read_text(encoding="utf-8")
        self.assertIn("OCR source: Lecture - Notes.pdf", note)
        self.assertIn("Pages 1", note)
        self.assertIn("OCR:", self.calls[0])
        state = ingest.load_state(self.paths)
        self.assertEqual(state["files"][digest]["extraction"]["ocr_pages"], [1])
        self.assertTrue(any(p.endswith(".json") and "ocr" in p for p in result["touched"]))
        self.calls.clear()
        self.factory.reset_mock()
        result = self.run_ingest()
        self.assertEqual(len(result["unchanged"]), 1)
        self.assertEqual(self.calls, [])
        self.factory.assert_not_called()

    def test_mixed_pdf_recovers_after_unavailable_engine(self):
        make_pdf(self.pdf, [("native", LONG), ("image", LONG)])
        self.factory.side_effect = RuntimeError("engine missing")
        first = self.run_ingest()
        note = Path(first["notes"][0])
        self.assertIn("Incomplete OCR:", note.read_text(encoding="utf-8"))
        self.factory.side_effect = None
        again = self.run_ingest()
        self.assertEqual(len(again["notes"]), 1)
        self.assertIn("OCR source:", note.read_text(encoding="utf-8"))
        self.assertNotIn("Incomplete OCR:", note.read_text(encoding="utf-8"))

    def test_cap_increase_updates_unchanged_pdf_and_warns_in_note(self):
        make_pdf(self.pdf, [("image", LONG), ("image", LONG)])
        with patch.dict(conf.CONFIG["extract"]["ocr"], {"page_cap": 1}):
            first = self.run_ingest()
            self.assertIn("Incomplete OCR:", Path(first["notes"][0]).read_text(encoding="utf-8"))
        again = self.run_ingest()
        self.assertEqual(len(again["notes"]), 1)
        self.assertNotIn("Incomplete OCR:", Path(again["notes"][0]).read_text(encoding="utf-8"))

    def test_dry_ingest_never_writes_sidecars_or_notes(self):
        result = self.run_ingest(apply=False)
        self.assertEqual(result["touched"], [])
        self.assertEqual(self.calls, [])
        self.assertFalse(Path(self.paths.data_dir).exists())


class WindowsBridgeTests(unittest.TestCase):
    def test_bridge_is_bounded_hidden_and_passes_language_and_image_on_stdin(self):
        cfg = copy.deepcopy(conf.CONFIG["extract"]["ocr"])
        cfg["language"] = "en-GB"
        completed = subprocess.CompletedProcess([], 0, json.dumps({"language": "en-GB", "max_dimension": 9000,
                                                                   "text": LONG}), "")
        with patch.object(subprocess, "run", return_value=completed) as run:
            engine = ocr.WindowsEngine(cfg)
            self.assertEqual(engine.recognize(b"png"), LONG)
        args, kw = run.call_args
        self.assertIn(cfg["windows"]["script"], args[0][-1])
        self.assertEqual(json.loads(kw["input"])["language"], "en-GB")
        self.assertEqual(json.loads(kw["input"])["image"], "cG5n")
        self.assertEqual(kw["timeout"], cfg["windows"]["timeout_seconds"])
        self.assertEqual(kw["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_missing_language_and_timeout_are_reported_by_session(self):
        cfg = copy.deepcopy(conf.CONFIG["extract"]["ocr"])
        for failure in (subprocess.CompletedProcess([], 0, '{"error":"language missing"}', ""),
                        subprocess.TimeoutExpired("bridge", cfg["windows"]["timeout_seconds"])):
            with self.subTest(failure=failure):
                kw = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch.object(subprocess, "run", **kw):
                    with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)):
                        ocr.WindowsEngine(cfg)


if __name__ == "__main__":
    unittest.main()
