"""ui_theme.json drives floating_orb.py and orb_controls.py.

Rule 0 (AGENTS.md): the colours, sizes, timings, motions and labels those
modules used to hold inline are data now. These tests pin: the shipped theme
validates cleanly (and ui_theme.json ships equal to its defaults), changing
the data changes the behaviour of each module, and a bad value logs one line
and falls back to the shipped default instead of crashing.

No Tk window is opened: only the pure render/hit-test helpers are called.
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import ui_theme  # noqa: E402
import floating_orb  # noqa: E402
import orb_controls  # noqa: E402

DEFAULTS = json.loads((ROOT / "ui_theme.defaults.json").read_text(encoding="utf-8"))


class _Theme(unittest.TestCase):
    """Each test writes its own ui_theme.json and points the loader at it."""

    def setUp(self):
        self._env = os.environ.get("DENISSE_UI_THEME")
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        if self._env is None:
            os.environ.pop("DENISSE_UI_THEME", None)
        else:
            os.environ["DENISSE_UI_THEME"] = self._env
        ui_theme.reload()

    def use(self, edit=None, raw=None, expect_warning=False):
        data = copy.deepcopy(DEFAULTS)
        if edit:
            edit(data)
        p = self.tmp / "ui_theme.json"
        p.write_text(raw if raw is not None else json.dumps(data), encoding="utf-8")
        os.environ["DENISSE_UI_THEME"] = str(p)
        ctx = self.assertLogs("denisse.ui_theme", "WARNING") if expect_warning or raw is not None \
            else self.assertNoLogs("denisse.ui_theme", "WARNING")
        with ctx as logs:
            ui_theme.reload()
        return logs


def _orb(breath_frames=24, ring_frames=120):
    """A FloatingOrb with just the state _pet_frame/_pet_state need."""
    class Img:
        def transform(self, size, kind, data, resample=None):
            return data
    o = object.__new__(floating_orb.FloatingOrb)
    o._phase, o.motion_enabled, o.total_px, o._scale = 0.0, True, 58, 1.0
    o._images = [Img()] * ring_frames
    o._breath_frames, o._ring_frames = breath_frames, ring_frames
    o._held = o._thinking = o._hovered = False
    o._reaction, o._reaction_until, o._last_interaction = "idle", 0.0, 0.0
    return o


class Loader(_Theme):
    def test_shipped_files_validate_cleanly(self):
        for name in ("ui_theme.json", "ui_theme.defaults.json"):
            with self.subTest(name=name):
                os.environ["DENISSE_UI_THEME"] = str(ROOT / name)
                with self.assertNoLogs("denisse.ui_theme", "WARNING"):
                    ui_theme.reload()

    def test_live_theme_ships_equal_to_defaults(self):
        live = json.loads((ROOT / "ui_theme.json").read_text(encoding="utf-8"))
        self.assertEqual(live, DEFAULTS)

    def test_bad_colour_falls_back(self):
        def edit(d):
            d["palette"]["accent"] = "purple"
        logs = self.use(edit, expect_warning=True)
        self.assertIn("palette.accent", "\n".join(logs.output))
        self.assertEqual(ui_theme.colour("accent"), DEFAULTS["palette"]["accent"])

    def test_wrong_type_and_negative_fall_back(self):
        def edit(d):
            d["animation"]["tick_ms"] = "fast"
            d["orb"]["size"] = -3
        self.use(edit, expect_warning=True)
        self.assertEqual(ui_theme.get("animation")["tick_ms"], DEFAULTS["animation"]["tick_ms"])
        self.assertEqual(ui_theme.get("orb")["size"], DEFAULTS["orb"]["size"])

    def test_unknown_motion_kind_falls_back(self):
        def edit(d):
            d["animation"]["motion"]["idle"]["kind"] = "spin"
        self.use(edit, expect_warning=True)
        self.assertEqual(ui_theme.get("animation")["motion"]["idle"], DEFAULTS["animation"]["motion"]["idle"])

    def test_unknown_dock_action_falls_back(self):
        def edit(d):
            d["dock"]["buttons"][0]["action"] = "email"
        self.use(edit, expect_warning=True)
        self.assertEqual(ui_theme.get("dock")["buttons"], DEFAULTS["dock"]["buttons"])

    def test_ring_arc_must_name_a_palette_colour(self):
        def edit(d):
            d["orb"]["ring"]["arcs"][1]["colour"] = "nope"
        self.use(edit, expect_warning=True)
        self.assertEqual(ui_theme.get("orb")["ring"]["arcs"], DEFAULTS["orb"]["ring"]["arcs"])

    def test_unreadable_file_uses_defaults(self):
        self.use(raw="{ not json")
        self.assertEqual(ui_theme.get("bubble")["bg"], DEFAULTS["bubble"]["bg"])

    def test_bad_template_falls_back(self):
        def edit(d):
            d["dock"]["voice_on_tip"] = "{who} is listening"
        self.use(edit, expect_warning=True)
        self.assertEqual(ui_theme.get("dock")["voice_on_tip"], DEFAULTS["dock"]["voice_on_tip"])


class FloatingOrbData(_Theme):
    def test_ring_colour_comes_from_the_palette(self):
        self.use()
        before = floating_orb._make_ring_frame(58, 22, 0).tobytes()
        self.use(lambda d: d["palette"].__setitem__("accent_bright", "#00ff00"))
        after = floating_orb._make_ring_frame(58, 22, 0)
        self.assertNotEqual(before, after.tobytes())
        greens = [p for p in (after.getpixel((x, y)) for x in range(58) for y in range(58)) if p[3] == 255 and p[:3] == (0, 255, 0)]
        self.assertTrue(greens, "the long arc should be drawn in the new accent_bright")

    def test_glow_strength_comes_from_the_data(self):
        def glow_alpha():  # a pixel in the halo, left of the core
            return floating_orb._make_orb_frame(58, 34, 0.0).getpixel((10, 29))[3]
        self.use()
        default = glow_alpha()
        self.use(lambda d: d["orb"]["glow"].__setitem__("alpha_rest", 20))
        self.assertLess(glow_alpha(), default)

    def test_mood_motion_and_speed_come_from_the_data(self):
        self.use()
        o = _orb()
        self.assertEqual(o._pet_frame("curious", 1.0), (1, 0, -0.0, 0, 1, 2.0))  # lift 2 px
        self.assertAlmostEqual(o._phase, DEFAULTS["animation"]["speed"]["default"])

        def edit(d):
            d["animation"]["motion"]["curious"] = {"kind": "lift", "amplitude": 5.0, "rate": 1.0}
            d["animation"]["speed"]["default"] = 3.0
        self.use(edit)
        o = _orb()
        self.assertEqual(o._pet_frame("curious", 1.0), (1, 0, -0.0, 0, 1, 5.0))
        self.assertAlmostEqual(o._phase, 3.0)

    def test_sleepy_after_comes_from_the_data(self):
        self.use()
        o = _orb()
        o._last_interaction = 0.0
        self.assertEqual(o._pet_state(100.0), "idle")
        self.use(lambda d: d["animation"].__setitem__("sleepy_after_s", 60))
        self.assertEqual(o._pet_state(100.0), "sleepy")

    def test_flatten_matte_defaults_to_theme_accent(self):
        from PIL import Image
        img = Image.new("RGBA", (2, 2), (0, 0, 0, 200))
        self.use(lambda d: d["palette"].__setitem__("accent", "#000000"))
        dark = floating_orb._flatten(img).getpixel((0, 0))
        self.use()
        light = floating_orb._flatten(img).getpixel((0, 0))
        self.assertNotEqual(dark, light)


class OrbControlsData(_Theme):
    def test_button_order_and_labels_come_from_the_data(self):
        self.use()
        x = DEFAULTS["dock"]["layout"]["first_x"]
        y = DEFAULTS["dock"]["layout"]["height"] / 2
        self.assertEqual(orb_controls._button_at(1.0, x, y), "chat")
        self.assertEqual(orb_controls._tip_text("voice", True), "Voice: listening on")

        def edit(d):
            b = d["dock"]["buttons"]
            b[0], b[2] = b[2], b[0]
            b[1]["label"] = "Talk"
            d["dock"]["voice_on_tip"] = "{label} (on)"
        self.use(edit)
        self.assertEqual(orb_controls._button_at(1.0, x, y), "reminders")
        self.assertEqual(orb_controls._tip_text("voice", True), "Talk (on)")
        self.assertEqual(orb_controls._tip_text("voice", False), "Talk")

    def test_dock_size_and_colour_come_from_the_data(self):
        self.use()
        before = orb_controls._dock_frame(1.0)
        self.assertEqual(before.size, (DEFAULTS["dock"]["layout"]["width"], DEFAULTS["dock"]["layout"]["height"]))

        def edit(d):
            d["dock"]["layout"]["width"] = 200
            d["dock"]["colours"]["fill"] = "#ff0000"
        self.use(edit)
        after = orb_controls._dock_frame(1.0)
        self.assertEqual(after.size, (200, DEFAULTS["dock"]["layout"]["height"]))
        r, g, b, a = after.getpixel((100, 40))
        self.assertGreater(r, 200)
        self.assertLess(g, 40)


if __name__ == "__main__":
    unittest.main()
