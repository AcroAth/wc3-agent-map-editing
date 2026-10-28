"""Screenshots of the game window.

``printwindow`` is the primary method: it asks the window to paint itself into
a memory DC, so it works while the game is covered by other windows.
``wgc`` (Windows Graphics Capture, optional package) is a fallback for windows
that return black frames.  ``screen`` grabs the desktop region and therefore
only works when the window is actually visible.

The returned PIL image is downscaled so that its width is at most
``max_width``; the accompanying info dict reports the exact ``scale`` so that
``client_pixel = image_pixel / scale``.
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageGrab

from . import winapi as w
from . import window

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_blank(image: Image.Image, tolerance: int = 4) -> bool:
    """Detect a useless capture: a (near-)uniform frame, e.g. the all-black
    result PrintWindow gives for some GPU swap chains.

    Checks the full-resolution extrema rather than a sample, so a dark map
    with a small HUD or a few units still counts as a real frame."""
    try:
        extrema = image.convert("RGB").getextrema()
    except Exception:
        return True
    return all(hi - lo <= tolerance for lo, hi in extrema)


def _out_dir(instance: str | None = None) -> Path:
    # Imported lazily to avoid a cycle (game imports crash, crash is standalone).
    from . import game

    try:
        return game.out_dir(instance)
    except Exception:
        base = Path(
            os.environ.get("WC3_TESTER_OUT")
            or (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "wc3-tester" / "runs")
        )
        name = f"attached-{instance}" if instance else "attached"
        path = base / name
        path.mkdir(parents=True, exist_ok=True)
        return path


def _save_png(image: Image.Image, directory: Path) -> str:
    shots = directory / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    path = shots / f"{stamp}.png"
    image.save(path, format="PNG")
    return str(path)


# ---------------------------------------------------------------------------
# PrintWindow
# ---------------------------------------------------------------------------


def _capture_printwindow(hwnd: int) -> tuple[Image.Image | None, dict]:
    cw, ch = window.client_size(hwnd)
    info: dict = {"client_width": cw, "client_height": ch}
    if cw <= 0 or ch <= 0:
        return None, {**info, "error": "empty client rect"}

    hdc_window = None
    hdc_mem = None
    hbitmap = None
    old_obj = None
    try:
        hdc_window = w.user32.GetWindowDC(hwnd)
        if not hdc_window:
            return None, {**info, "error": "GetWindowDC failed"}
        hdc_mem = w.gdi32.CreateCompatibleDC(hdc_window)
        if not hdc_mem:
            return None, {**info, "error": "CreateCompatibleDC failed"}
        hbitmap = w.gdi32.CreateCompatibleBitmap(hdc_window, cw, ch)
        if not hbitmap:
            return None, {**info, "error": "CreateCompatibleBitmap failed"}
        old_obj = w.gdi32.SelectObject(hdc_mem, hbitmap)

        # PW_CLIENTONLY (1) | PW_RENDERFULLCONTENT (2)
        flags = w.PW_CLIENTONLY | w.PW_RENDERFULLCONTENT
        ok = bool(w.user32.PrintWindow(hwnd, hdc_mem, flags))
        info["printwindow_ok"] = ok

        # The bitmap must not be selected into the DC for GetDIBits.
        if old_obj:
            w.gdi32.SelectObject(hdc_mem, old_obj)
            old_obj = None

        bmi = w.BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(w.BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = cw
        bmi.bmiHeader.biHeight = -ch  # top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = w.BI_RGB
        bmi.bmiHeader.biSizeImage = cw * ch * 4

        buf = (w.c_ubyte * (cw * ch * 4))()
        got = w.gdi32.GetDIBits(
            hdc_mem,
            hbitmap,
            0,
            ch,
            ctypes.cast(buf, w.LPVOID),
            ctypes.byref(bmi),
            w.DIB_RGB_COLORS,
        )
        if not got:
            return None, {**info, "error": f"GetDIBits failed (error {w.last_error()})"}

        image = Image.frombuffer(
            "RGB", (cw, ch), bytes(buf), "raw", "BGRX", 0, 1
        ).copy()
        return image, info
    except Exception as exc:  # pragma: no cover - Windows only
        return None, {**info, "error": f"printwindow failed: {exc}"}
    finally:
        try:
            if hdc_mem and old_obj:
                w.gdi32.SelectObject(hdc_mem, old_obj)
            if hbitmap:
                w.gdi32.DeleteObject(hbitmap)
            if hdc_mem:
                w.gdi32.DeleteDC(hdc_mem)
            if hdc_window:
                w.user32.ReleaseDC(hwnd, hdc_window)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Windows Graphics Capture (optional)
# ---------------------------------------------------------------------------


def _capture_wgc(hwnd: int) -> tuple[Image.Image | None, dict]:
    info: dict = {}
    try:
        from windows_capture import WindowsCapture  # type: ignore
    except Exception as exc:
        return None, {"error": f"windows_capture not importable: {exc}"}

    title = window.window_title(hwnd)
    if not title:
        return None, {"error": "window has no title; wgc needs one"}

    frames: list[tuple[int, int, bytes]] = []
    try:
        capture = WindowsCapture(cursor_capture=False, window_name=title)

        @capture.event
        def on_frame_arrived(frame, capture_control):  # pragma: no cover - Windows only
            try:
                buffer = bytes(frame.frame_buffer)
                frames.append((int(frame.width), int(frame.height), buffer))
            except Exception:
                try:
                    frames.append(
                        (
                            int(frame.width),
                            int(frame.height),
                            frame.frame_buffer.tobytes(),
                        )
                    )
                except Exception:
                    pass
            finally:
                try:
                    capture_control.stop()
                except Exception:
                    pass

        @capture.event
        def on_closed():  # pragma: no cover - Windows only
            pass

        capture.start()  # blocks until stop() is called above
    except Exception as exc:
        return None, {"error": f"wgc capture failed: {exc}"}

    if not frames:
        return None, {"error": "wgc produced no frames"}
    width, height, buffer = frames[0]
    try:
        image = Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRA", 0, 1).copy()
    except Exception as exc:
        return None, {"error": f"wgc frame conversion failed: {exc}"}

    cw, ch = window.client_size(hwnd)
    info["wgc_frame_size"] = [width, height]
    if cw > 0 and ch > 0 and (width != cw or height != ch):
        # Window capture sometimes includes borders; keep the client area.
        image = image.crop((0, 0, min(cw, width), min(ch, height)))
        info["cropped_to"] = [min(cw, width), min(ch, height)]
    return image, info


# ---------------------------------------------------------------------------
# Desktop grab
# ---------------------------------------------------------------------------


def _capture_screen(hwnd: int) -> tuple[Image.Image | None, dict]:
    cw, ch = window.client_size(hwnd)
    if cw <= 0 or ch <= 0:
        return None, {"error": "empty client rect"}
    sx, sy = window.client_to_screen(hwnd, 0, 0)
    bbox = (sx, sy, sx + cw, sy + ch)
    info = {
        "client_width": cw,
        "client_height": ch,
        "bbox": list(bbox),
        "warning": "screen capture only works if the window is visible and not covered",
    }
    try:
        image = ImageGrab.grab(bbox=bbox, all_screens=True)
        return image.convert("RGB"), info
    except Exception as exc:
        return None, {**info, "error": f"ImageGrab failed: {exc}"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def capture(
    hwnd: int,
    method: str = "auto",
    max_width: int = 1280,
    restore_minimized: bool = False,
    instance: str | None = None,
) -> tuple[Image.Image, dict]:
    """Capture the client area of *hwnd*.

    Returns ``(PIL.Image, info)``.  ``method`` is one of ``auto``,
    ``printwindow``, ``wgc`` or ``screen``.
    """
    method = (method or "auto").strip().lower()
    if method not in ("auto", "printwindow", "wgc", "screen"):
        raise ValueError("method must be auto, printwindow, wgc or screen")
    if not w.is_window(hwnd):
        raise RuntimeError("window handle is no longer valid (game exited?)")

    info: dict = {"method_requested": method, "attempts": []}
    minimized = window.is_minimized(hwnd)
    info["minimized"] = minimized
    if minimized:
        if restore_minimized:
            try:
                w.user32.ShowWindow(hwnd, w.SW_SHOWNOACTIVATE)
                time.sleep(0.4)
                info["restored"] = True
                info["minimized"] = window.is_minimized(hwnd)
            except Exception as exc:
                info["restore_error"] = str(exc)
        else:
            info["warning"] = (
                "window is minimised; PrintWindow/screen captures may be black. "
                "Retry with restore_minimized=True."
            )

    image: Image.Image | None = None
    chosen = method

    if method in ("auto", "printwindow"):
        img, sub = _capture_printwindow(hwnd)
        attempt = {"method": "printwindow", **sub}
        if img is not None:
            blank = is_blank(img)
            attempt["blank"] = blank
            if not blank:
                image = img
                chosen = "printwindow"
            else:
                attempt["note"] = "blank capture, trying fallback"
        info["attempts"].append(attempt)

    if image is None and method in ("auto", "wgc"):
        img, sub = _capture_wgc(hwnd)
        attempt = {"method": "wgc", **sub}
        if img is not None:
            blank = is_blank(img)
            attempt["blank"] = blank
            if not blank:
                image = img
                chosen = "wgc"
        info["attempts"].append(attempt)

    if image is None and method in ("auto", "screen"):
        img, sub = _capture_screen(hwnd)
        attempt = {"method": "screen", **sub}
        if img is not None:
            attempt["blank"] = is_blank(img)
            if attempt["blank"]:
                attempt["note"] = "blank capture (window covered or minimised?)"
            image = img
            chosen = "screen"
            if "warning" not in info:
                info["warning"] = sub.get("warning")
        info["attempts"].append(attempt)

    if image is None:
        details = "; ".join(
            f"{a.get('method')}: {a.get('error', a.get('note', 'blank'))}"
            for a in info["attempts"]
        )
        raise RuntimeError(f"all capture methods failed ({details})")

    cw, ch = window.client_size(hwnd)
    info.update(
        {
            "method": chosen,
            "client_width": cw,
            "client_height": ch,
            "image_width": image.width,
            "image_height": image.height,
            "scale": 1.0,
        }
    )
    try:
        if max_width and image.width > int(max_width):
            scale = int(max_width) / float(image.width)
            new_size = (
                max(1, int(round(image.width * scale))),
                max(1, int(round(image.height * scale))),
            )
            image = image.resize(new_size, Image.Resampling.LANCZOS)
            info["scale"] = scale
            info["image_width"], info["image_height"] = image.size
    except Exception as exc:
        info["resize_error"] = str(exc)

    try:
        path = _save_png(image, _out_dir(instance))
        info["path"] = path
    except Exception as exc:
        info["save_error"] = str(exc)

    return image, info


def screenshot_png(
    hwnd: int,
    method: str = "auto",
    max_width: int = 1280,
    restore_minimized: bool = False,
    instance: str | None = None,
) -> tuple[bytes, dict]:
    """Convenience wrapper returning PNG bytes (what the MCP tool ships)."""
    import io

    image, info = capture(
        hwnd,
        method=method,
        max_width=max_width,
        restore_minimized=restore_minimized,
        instance=instance,
    )
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue(), info
