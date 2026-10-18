"""
Denisse -- real per-pixel alpha for a Tk toplevel on Windows.

Tk's -transparentcolor is a colour key: each pixel is either fully shown
or fully gone, so a soft glow comes out as a hard, spiky edge (and any
part-alpha pixel blends toward the key colour into a dark halo). Asked
2026-10-14 to keep the orb's soft purple glow without that, so the orb
hands its frames to Windows directly with UpdateLayeredWindow, which
composites a premultiplied BGRA bitmap with true alpha over whatever is
behind it. Tk still owns the window (position, mouse events -- clicks on
fully transparent pixels fall through, as before), it just no longer
paints it.

Only for windows whose whole look is a PIL image: Tk widgets inside a
layered window updated this way aren't drawn at all.
"""

import ctypes
from ctypes import wintypes

from PIL import Image, ImageChops

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
ULW_ALPHA = 0x2
AC_SRC_OVER, AC_SRC_ALPHA = 0x00, 0x01
BI_RGB, DIB_RGB_COLORS = 0, 0

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_long)
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = (wintypes.HWND,)
user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
gdi32.DeleteDC.argtypes = (wintypes.HDC,)


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = (wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD)
user32.UpdateLayeredWindow.argtypes = (
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
    wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
)


def to_bgra(img: Image.Image) -> bytes:
    """RGBA -> premultiplied BGRA bytes, the format UpdateLayeredWindow
    wants. Precompute these once per frame; this is the slow part."""
    r, g, b, a = img.convert("RGBA").split()
    r, g, b = (ImageChops.multiply(c, a) for c in (r, g, b))
    return Image.merge("RGBA", (b, g, r, a)).tobytes()


class LayeredPainter:
    """Owns one size x size DIB and pushes frames to a Tk toplevel."""

    def __init__(self, win, size):
        win.update_idletasks()
        self.hwnd = int(win.wm_frame(), 16)
        self.w, self.h = size
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)

        screen = user32.GetDC(None)
        self.mem = gdi32.CreateCompatibleDC(screen)
        user32.ReleaseDC(None, screen)
        bih = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), self.w, -self.h, 1, 32, BI_RGB, 0, 0, 0, 0, 0)
        self.bits = ctypes.c_void_p()
        self.dib = gdi32.CreateDIBSection(self.mem, ctypes.byref(bih), DIB_RGB_COLORS, ctypes.byref(self.bits), None, 0)
        self.old = gdi32.SelectObject(self.mem, self.dib)
        self.nbytes = self.w * self.h * 4
        self.blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)

    def show(self, bgra: bytes) -> bool:
        """Push one frame (from to_bgra). Position is left to Tk."""
        ctypes.memmove(self.bits, bgra, self.nbytes)
        return bool(user32.UpdateLayeredWindow(
            self.hwnd, None, None, ctypes.byref(wintypes.SIZE(self.w, self.h)),
            self.mem, ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(self.blend), ULW_ALPHA,
        ))

    def close(self):
        try:
            gdi32.SelectObject(self.mem, self.old)
            gdi32.DeleteObject(self.dib)
            gdi32.DeleteDC(self.mem)
        except Exception:
            pass
