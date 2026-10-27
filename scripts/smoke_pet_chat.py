"""Real-Tk pet-chat smoke checks with synthetic history and no model/TTS calls.

Exercises movement, screen edges, scaling, voice/typed reply presentation,
reply actions and closing during a pending reply. Saves fixture screenshots
under ignored logs/. Run on a Windows desktop, not a headless CI worker.
"""
import copy
import sys
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import ImageGrab, ImageTk
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tray
from floating_orb import _make_orb_frame
from reminder_card import ThinkingBubble, _work_area


def main():
    root = tk.Tk()
    root.withdraw()
    tray.root = root
    errors, checks = [], []
    def callback_error(*exc):
        errors.append(str(exc[1])); root.quit()
    root.report_callback_exception = callback_error
    history = [{'role': 'user', 'content': 'Hi Denisse'},
               {'role': 'assistant', 'content': "I'm here. What are we working on?"}]
    tray._reminder_card = tray._study_bubble = tray._thinking_bubble = tray._reply_bubble = None
    tray._thinking_sources.clear()
    orbwin = tk.Toplevel(root)
    orbwin.overrideredirect(True)
    orbwin.attributes('-topmost', True)
    orbwin.configure(bg=tray.POPUP_BG)
    orb_canvas = tk.Label(orbwin, bg=tray.POPUP_BG, bd=0)
    orb_canvas.pack(fill='both', expand=True)
    orb = SimpleNamespace(win=orbwin, total_px=88, react=Mock(), set_thinking=Mock())
    tray._floating_orb = orb
    output = Path('logs'); output.mkdir(exist_ok=True)
    popup = None
    scales = iter((1, 1.25, 1.5, 2))

    def check(ok, name):
        if not ok:
            raise AssertionError(name)
        checks.append(name)

    def widgets(widget):
        return [widget] + [child for w in widget.winfo_children() for child in widgets(w)]

    def next_scale():
        nonlocal popup
        try:
            scale = next(scales)
        except StopIteration:
            root.quit()
            return
        root.tk.call('tk', 'scaling', 96 * scale / 72)
        del history[2:]  # each scale gets an independent synthetic exchange
        tray.DPI_SCALE = scale
        orb.total_px = round(88 * scale)
        orb_canvas.photo = ImageTk.PhotoImage(_make_orb_frame(orb.total_px, round(48 * scale), .6))
        orb_canvas.configure(image=orb_canvas.photo)
        area = _work_area(root.winfo_screenwidth(), root.winfo_screenheight())
        positions = iter(((area[2] - orb.total_px - 35, area[3] - orb.total_px - 80),
                          (area[0] + 12, area[1] + 12),
                          (area[2] - orb.total_px - 12, area[1] + 12)))
        ox, oy = next(positions)
        orbwin.geometry(f'{orb.total_px}x{orb.total_px}+{ox}+{oy}')
        root.update_idletasks()
        popup = tray.ChatPopup()
        tray._chat_popup = popup
        popup._brain_ready(Mock(), None)

        def inspect():
            nonlocal popup
            popup.win.update_idletasks()
            x, y = popup.win.winfo_x(), popup.win.winfo_y()
            w, h = popup.win.winfo_width(), popup.win.winfo_height()
            check(x >= area[0] and y >= area[1] and x+w <= area[2] and y+h <= area[3],
                  f'chat stays inside work area at {scale}')
            check(popup.entry.winfo_ismapped(), f'input visible at {scale}')
            check(popup.entry.winfo_rooty()+popup.entry.winfo_height() <= y+h,
                  f'input not clipped at {scale}')
            check(popup.entry.cget('state') == 'normal', f'input ready at {scale}')
            stop = next(w for w in widgets(popup.win)
                        if isinstance(w, tk.Button) and w.cget('text') == 'Stop voice')
            check(stop.winfo_width() >= stop.winfo_reqwidth(), f'stop button fits at {scale}')
            if scale == 1:
                ImageGrab.grab((x, y, min(area[2], ox+orb.total_px+8),
                                min(area[3], y+h))).save(output/'pet_chat_fixture.png')
            try:
                nx, ny = next(positions)
                orbwin.geometry(f'{orb.total_px}x{orb.total_px}+{nx}+{ny}')
                root.after(140, inspect)
                return
            except StopIteration:
                pass
            history.extend([{'role': 'user', 'content': 'A voice follow-up'},
                            {'role': 'assistant', 'content': 'Here is the voice answer.'}])
            tray._show_reply_main('Here is the voice answer.')
            texts = [popup.canvas.itemcget(i, 'text') for i in popup.canvas.find_all()
                     if popup.canvas.type(i) == 'text']
            check(texts.count('Here is the voice answer.') == 1, f'voice answer shown once at {scale}')
            check(tray._reply_bubble is None, f'no duplicate reply window at {scale}')
            tray._show_reply_main('Your timer is done.')
            texts = [popup.canvas.itemcget(i, 'text') for i in popup.canvas.find_all()
                     if popup.canvas.type(i) == 'text']
            check('Your timer is done.' in texts, f'timer notice visible at {scale}')
            tray._show_reply_main('Here is the voice answer.', source='chat')
            popup.busy = True
            popup.entry.insert(0, 'shush')
            popup._send()
            check(popup.typing_label.cget('text') == 'Speech stopped', f'busy typed stop at {scale}')
            old = popup
            popup.close()
            check(tray._chat_popup is None, f'chat closes cleanly at {scale}')
            old._brain_ready(Mock(), None)  # late connection completion is harmless
            long_reply = 'A complete reply remains in shared chat history. ' * 20
            bubble = ThinkingBubble(root, orb, on_reply=Mock(), on_stop=Mock())
            check(bubble.deliver(long_reply), f'closed-chat reply lands at {scale}')
            check(bubble.full_message == long_reply, f'full reply preserved at {scale}')
            labels = [w for w in widgets(bubble.win) if isinstance(w, tk.Label)]
            reply_link = next(w for w in labels if w.cget('text') == 'Continue chat →')
            def inspect_reply():
                bubble.win.update_idletasks()
                check(reply_link.winfo_ismapped(), f'continue link visible at {scale}')
                check(reply_link.winfo_rootx()+reply_link.winfo_width() <= bubble.win.winfo_x()+bubble.win.winfo_width(),
                      f'continue link fits at {scale}')
                if scale == 1:
                    bx, by = bubble.win.winfo_x(), bubble.win.winfo_y()
                    ImageGrab.grab((bx, by, bx+bubble.win.winfo_width(), by+bubble.win.winfo_height())).save(output/'pet_reply_fixture.png')
                bubble.close()
                root.after(20, next_scale)
            root.after(400, inspect_reply)

        root.after(160, inspect)

    with patch.object(tray.conversation, 'replay', side_effect=lambda: copy.deepcopy(history)), \
         patch.object(tray.conversation, 'record'), patch.object(tray.ChatPopup, '_init_brain'), \
         patch.object(tray, 'get_status', return_value={'connected': True, 'model_ready': True}):
        def guarded_start():
            try:
                next_scale()
            except Exception as exc:
                errors.append(str(exc)); root.quit()
        root.after(0, guarded_start)
        root.after(20_000, root.quit)
        root.mainloop()
    root.destroy()
    print(f'{len(checks)} Tk checks passed; callback errors: {errors}')
    return 1 if errors or len(checks) < 80 else 0


if __name__ == '__main__':
    raise SystemExit(main())
