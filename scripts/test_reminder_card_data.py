"""Card rendering and interaction follow data without a desktop or real tasks."""
import copy
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import reminder_card as card
import reminders_config
import ui_theme

DEFAULTS = json.loads((ROOT / 'reminders.defaults.json').read_text(encoding='utf-8'))
THEME = json.loads((ROOT / 'ui_theme.defaults.json').read_text(encoding='utf-8'))
TASK = {'id': 'fixture', 'text': 'Water plants', 'due': None, 'priority': 'high'}


class Widget:
    """Capture the Tk boundary; geometry and callbacks are tested separately."""
    def __init__(self, kind, parent=None, **options):
        self.kind, self.parent, self.options = kind, parent, options
        self.packed = {}
        self.value = ''

    def pack(self, **options):
        self.packed = options

    def config(self, **options):
        self.options.update(options)

    def insert(self, index, value):
        self.value = value

    def get(self):
        return self.value

    def bind(self, *args):
        pass

    def create_window(self, *args, **kwargs):
        pass

    def destroy(self):
        pass


def render_card(module=card, mode='list', scale=1):
    widgets = []

    def factory(kind):
        def make(*args, **kwargs):
            widget = Widget(kind, *args, **kwargs)
            widgets.append(widget)
            return widget
        return make

    cls = module.ThinkingBubble if mode == 'reply' else module.ReminderCard
    instance = cls.__new__(cls)
    instance.s, instance.font = scale, 'Fixture Font'
    instance.body, instance.canvas = None, None
    instance.win = Mock()
    instance.tasks = SimpleNamespace(list_tasks=lambda: [TASK, {**TASK, 'id': 'second'}])
    instance.typing, instance.thinking = mode == 'typing', False
    instance.editing = TASK['id'] if mode == 'edit' else ('new' if mode == 'new' else None)
    instance.note, instance._dots = 'Fixture note', 0
    instance._fit_list, instance._layout = Mock(), Mock()
    instance.on_reply, instance.on_stop = Mock(), Mock()
    instance.message = 'Fixture reply'
    with patch.multiple(module.tk, **{kind: factory(kind) for kind in ('Frame', 'Label', 'Entry', 'Canvas')}):
        instance.render()
    return instance, widgets


class CardData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            'DENISSE_REMINDERS': str(Path(self.tmp.name) / 'reminders.json'),
            'DENISSE_UI_THEME': str(Path(self.tmp.name) / 'theme.json'),
            'DENISSE_USER_NAME': 'Fixture User',
        })
        self.env.start()
        self.addCleanup(self.restore)
        self.use()

    def restore(self):
        self.env.stop()
        reminders_config.reload()
        ui_theme.reload()
        importlib.reload(card)

    def use(self, layout=None, timing=None, text=None, interaction=None, theme=None):
        data = copy.deepcopy(DEFAULTS)
        for key, edits in (('layout', layout), ('timing', timing), ('text', text), ('interaction', interaction)):
            data['card'][key].update(edits or {})
        Path(os.environ['DENISSE_REMINDERS']).write_text(json.dumps(data), encoding='utf-8')
        Path(os.environ['DENISSE_UI_THEME']).write_text(json.dumps(theme or THEME), encoding='utf-8')
        reminders_config.reload()
        ui_theme.reload()
        importlib.reload(card)

    def test_default_list_and_editor_layout(self):
        instance, widgets = render_card()
        label = next(w for w in widgets if w.options.get('text') == TASK['text'])
        self.assertEqual(label.options['wraplength'], 197)
        instance.win.after.assert_called_once_with(30, instance._scroll_to_edit)
        instance, widgets = render_card(mode='edit')
        entries = [w for w in widgets if w.kind == 'Entry']
        self.assertEqual([w.packed['ipady'] for w in entries], [2, 2])
        self.assertEqual([call.args[0] for call in instance.win.after.call_args_list], [10, 30])

    def test_layout_and_delays_change_at_widget_boundary(self):
        self.use(layout={'row_controls_px': 40, 'bar_gap_px': 8, 'priority_stripe_px': 5,
                         'entry_pad_y_px': 6, 'entry_border_px': 3, 'chip_border_px': 2,
                         'chip_pad_x_px': 9, 'chip_gap_px': 11},
                 timing={'scroll_to_edit_ms': 72, 'edit_focus_ms': 45})
        instance, widgets = render_card(scale=2)
        label = next(w for w in widgets if w.options.get('text') == TASK['text'])
        self.assertEqual(label.options['wraplength'], 448)
        self.assertTrue(any(w.kind == 'Frame' and w.options.get('width') == 10 for w in widgets))
        instance.win.after.assert_called_once_with(72, instance._scroll_to_edit)
        instance, widgets = render_card(mode='edit', scale=2)
        entries = [w for w in widgets if w.kind == 'Entry']
        self.assertEqual([w.packed['ipady'] for w in entries], [12, 12])
        self.assertEqual([w.options['highlightthickness'] for w in entries], [3, 3])
        chip = next(w for w in widgets if w.options.get('text') == 'High')
        self.assertEqual((chip.options['padx'], chip.options['highlightthickness'], chip.packed['padx']), (18, 2, (0, 22)))
        self.assertEqual([call.args[0] for call in instance.win.after.call_args_list], [45, 72])

    def test_reply_actions_use_configured_spacing_and_text(self):
        self.use(layout={'reply_actions_top_px': 17, 'reply_stop_gap_px': 19}, text={'stop_voice': 'Quiet'})
        _, widgets = render_card(mode='reply', scale=2)
        stop = next(w for w in widgets if w.options.get('text') == 'Quiet')
        self.assertEqual(stop.packed['padx'], (38, 0))
        self.assertEqual(stop.parent.packed['pady'], (34, 0))

    def test_typing_count_symbol_separator_and_delay(self):
        self.use(layout={'typing_dot_count': 2}, timing={'typing_dot_ms': 123},
                 text={'typing_dot': '<>', 'typing_dot_join': '/'})
        instance, _ = render_card(mode='typing')
        self.assertEqual(instance._typing_label.options['text'], '<>')
        instance._tick_dots()
        self.assertEqual(instance._typing_label.options['text'], '<>/<>')
        instance._tick_dots()
        self.assertEqual(instance._typing_label.options['text'], '<>')
        self.assertEqual(instance.win.after.call_args.args[0], 123)

    def test_scroll_speed_uses_data_preserving_small_delta_direction(self):
        for settings, expected in (({}, [-2, 4, -1, 1]),
                                   ({'wheel_units_per_notch': 4, 'touchpad_units': 3}, [-4, 8, -3, 3])):
            self.use(interaction=settings)
            instance = card.ReminderCard.__new__(card.ReminderCard)
            instance._list_full, instance._list_view = 1000, 100
            instance._list_canvas, instance._draw_bar = Mock(), Mock()
            instance._list_canvas.yview.return_value = (0.1, 0.2)
            for delta, step in zip((120, -240, 1, -1), expected):
                self.assertEqual(instance._on_wheel(SimpleNamespace(delta=delta)), 'break')
                instance._list_canvas.yview_scroll.assert_called_with(step, 'units')

    def test_scroll_thumb_track_and_edit_padding(self):
        self.use(layout={'bar_track_px': 3, 'bar_thumb_min_px': 24, 'scroll_edit_pad_px': 13})
        instance = card.ReminderCard.__new__(card.ReminderCard)
        instance.s = 2
        instance._list_full, instance._list_view = 1000, 100
        instance._bar, instance._list_canvas, instance._edit_box = Mock(), Mock(), Mock()
        instance._list_canvas.yview.return_value = (0.1, 0.2)
        instance._edit_box.winfo_y.return_value = 100
        instance._draw_bar()
        rectangles = instance._bar.create_rectangle.call_args_list
        self.assertEqual(rectangles[0].args, (4, 0, 7, 100))
        self.assertEqual(rectangles[1].args, (0, 10, 8, 58))
        instance._scroll_to_edit()
        instance._list_canvas.yview_moveto.assert_called_once_with(0.074)

    def test_slide_easing_and_duration(self):
        self.use(timing={'slide_ease_power': 2, 'anim_ms': 400, 'anim_steps': 4})
        instance = card.ReminderCard.__new__(card.ReminderCard)
        instance.s, instance.win = 1, Mock()
        instance._slide_in('100x50', 20, 30, 'right')
        delay, step, index = instance.win.after.call_args.args
        self.assertEqual((delay, index), (100, 1))
        step(2)
        instance.win.attributes.assert_called_with('-alpha', 0.75)
        instance.win.geometry.assert_called_with('100x50+26+30')

    def test_shared_theme_controls_card_colour_and_glow(self):
        before = card._BubbleArt(100, 60, 'right', 30, 1, 9)
        old_base = before._base(0.5).tobytes()
        old_frame = before.frame(0.5, 0).tobytes()
        theme = copy.deepcopy(THEME)
        theme['palette'].update(ring_violet='#12ab34', transparent_key='#040506')
        theme['orb']['glow'].update(alpha_rest=100, alpha_swing=20.0)
        self.use(theme=theme)
        self.assertEqual(card.TRANSPARENT_KEY, '#040506')
        after = card._BubbleArt(100, 60, 'right', 30, 1, 9)
        self.assertNotEqual(old_base, after._base(0.5).tobytes())
        frame = after.frame(0.5, 0)
        self.assertNotEqual(old_frame, frame.tobytes())
        self.assertIn((18, 171, 52, 255), set(frame.getdata()))

    def test_art_geometry_and_glow_follow_data(self):
        before = card._BubbleArt(100, 60, 'right', 30, 1, 9)
        for edits, mask in (({'tail_overlap_strokes': 8}, 'fill'),
                            ({'tail_inset_y': 2.0}, 'ring'),
                            ({'tail_inset_tip': 4.0}, 'ring'),
                            ({'stroke_min_px': 5.0}, 'ring'),
                            ({'glow_blur_frac': 0.2}, 'glow')):
            with self.subTest(edits=edits):
                self.use(layout=edits)
                after = card._BubbleArt(100, 60, 'right', 30, 1, 9)
                self.assertNotEqual(getattr(before, mask).tobytes(), getattr(after, mask).tobytes())
        self.use(layout={'glow_strength': 0.3})
        after = card._BubbleArt(100, 60, 'right', 30, 1, 9)
        # Both must use their own settings, because the base is computed lazily.
        dim = after._base(0.5).tobytes()
        self.use()
        self.assertNotEqual(before._base(0.5).tobytes(), dim)

    def test_arc_feather_and_offset_follow_data(self):
        old_lut = card._BubbleArt._arc_lut(0, 150)
        old_frame = card._BubbleArt(100, 60, 'right', 30, 1, 9).frame(0.5, 0).tobytes()
        self.use(layout={'arc_feather_deg': 20, 'short_arc_offset_deg': 100})
        self.assertLess(card._BubbleArt._arc_lut(0, 150)[3], old_lut[3])
        self.assertNotEqual(card._BubbleArt(100, 60, 'right', 30, 1, 9).frame(0.5, 0).tobytes(), old_frame)

    def test_reply_limit_accounts_for_entire_configured_suffix(self):
        for marker, expected in (('…', 'abcdefghijk…'), ('[...]', 'abcdefg[...]'),
                                 ('', 'abcdefghijkl'), ('abcdefghijklmn', 'abcdefghijkl')):
            with self.subTest(marker=marker):
                self.use(layout={'reply_max_chars': 12}, text={'ellipsis': marker})
                instance = card.ThinkingBubble.__new__(card.ThinkingBubble)
                instance.win, instance.render = Mock(), Mock()
                self.assertTrue(instance.deliver('abcdefghijklmnopqrst'))
                self.assertEqual(instance.message, expected)
                self.assertEqual(instance.full_message, 'abcdefghijklmnopqrst')


if __name__ == '__main__':
    unittest.main(verbosity=2)
