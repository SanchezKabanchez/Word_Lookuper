#!/usr/bin/env python3
r"""Render real Tk views from fixed, local fixtures; never query a translator.

Linux: xvfb-run -a -s '-screen 0 1440x1000x24' .venv/bin/python \
    scripts/render_screens.py --output-dir artifacts/screenshots
Windows: .venv\Scripts\python scripts\render_screens.py --scale-percent 125 \
    --output-dir artifacts/screenshots/windows-tk-125pct

Windows variants control Tk font scaling relative to 96 DPI; they do not change
the desktop's DPI or replace a real monitor/mixed-DPI manual check.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch


def _windows_display_info(root):
    """Read reported OS DPI without changing the user's display settings."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    info = {"hardware_dpi_tested": False}
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetDpiForSystem.restype = wintypes.UINT
        info["reported_system_dpi"] = int(user32.GetDpiForSystem())
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        user32.GetDpiForWindow.restype = wintypes.UINT
        info["reported_window_dpi"] = int(user32.GetDpiForWindow(root.winfo_id()))
    except (AttributeError, OSError) as exc:
        info["system_dpi_unavailable"] = type(exc).__name__
    try:
        awareness = ctypes.c_int()
        result = ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(awareness))
        if result == 0:
            info["process_dpi_awareness"] = int(awareness.value)
        else:
            info["process_dpi_awareness_hresult"] = int(result)
    except (AttributeError, OSError) as exc:
        info["process_dpi_awareness_unavailable"] = type(exc).__name__
    return info


def _configure_scaling(root, scale_percent):
    """Called immediately after Tk creation, before application fonts/widgets."""
    import tkinter.font as tkfont
    native = float(root.tk.call("tk", "scaling"))
    baseline = 96.0 / 72.0
    if sys.platform == "win32":
        requested = baseline * scale_percent / 100
        root.tk.call("tk", "scaling", requested)
        if "segoe ui" not in {family.casefold() for family in tkfont.families(root)}:
            raise RuntimeError("Segoe UI is required for Windows screenshot variants")
    else:
        requested = native
    return {
        "mode": "controlled Tk scaling" if sys.platform == "win32" else "native Tk scaling",
        "scale_percent": scale_percent,
        "baseline_dpi": 96 if sys.platform == "win32" else None,
        "baseline_tk_scaling": baseline if sys.platform == "win32" else native,
        "native_tk_scaling": native,
        "requested_tk_scaling": requested,
        "actual_tk_scaling": float(root.tk.call("tk", "scaling")),
        "tk_reported_pixels_per_inch": float(root.winfo_fpixels("1i")),
        "screen_pixels": [root.winfo_screenwidth(), root.winfo_screenheight()],
        "hardware_dpi_tested": False,
        "limitation": "Tk scaling variants; desktop resolution and hardware/mixed-monitor DPI are unchanged.",
    }


def _settle(root, ready=lambda: True, timeout=3.0):
    deadline = time.monotonic() + timeout
    while True:
        root.update_idletasks()
        root.update()
        if ready():
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("View did not finish rendering before the deadline")
        time.sleep(0.01)


def _capture(root, window, path):
    from PIL import ImageGrab
    _settle(root)
    window.lift()
    _settle(root)
    x, y = window.winfo_rootx(), window.winfo_rooty()
    width, height = window.winfo_width(), window.winfo_height()
    if width < 20 or height < 20:
        raise RuntimeError(f"View is unexpectedly small: {width}x{height}")
    ImageGrab.grab(bbox=(x, y, x + width, y + height)).convert("RGB").save(path)
    return {"file": path.name, "width": width, "height": height}


def _backdrop(width, height, light=False):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (width, height), "#faf8ed" if light else "#202b39")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, height // 2, width, height), fill="#fffdf1" if light else "#17232d")
    draw.polygon(((0, height // 2), (width // 3, 160), (width, height // 2)),
                 fill="#eee9d8" if light else "#334453")
    paths = (Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf",
             Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
    font = next((ImageFont.truetype(str(p), 28) for p in paths if p.exists()),
                ImageFont.load_default(size=28))
    # Word positions are provided to OCR below, keeping this visual test independent
    # of engine versions, model initialization and game fonts.
    words = []
    for row, text in enumerate(("Follow the changing signal.", "We will find the hidden passage.")):
        x, y = 140, height // 2 + 60 + row * 48
        for word in text.split():
            length = int(draw.textlength(word, font=font))
            draw.text((x, y), word, font=font, fill="#252c34" if light else "#f4f1e8", stroke_width=1)
            words.append((word, x, y, x + length, y + 34, row))
            x += length + int(draw.textlength(" ", font=font))
    return image, words


def render(output, scale_percent=100):
    if sys.platform != "win32" and scale_percent != 100:
        raise ValueError("125% and 150% variants require Windows and Segoe UI")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="word-lookup-render-") as data_dir, ExitStack() as patches:
        patches.enter_context(patch.dict(os.environ, {
            "XDG_CONFIG_HOME": data_dir, "APPDATA": data_dir,
            "PYGAME_HIDE_SUPPORT_PROMPT": "1",
        }))
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import requests
        import word_lookup_secrets
        # Import does not load settings/keys; also isolate the OS vault during
        # any widget-driven saves in this renderer.
        patches.enter_context(patch.object(word_lookup_secrets, "load_key", return_value=""))
        patches.enter_context(patch.object(word_lookup_secrets, "store_key", return_value=False))
        patches.enter_context(patch.object(requests.Session, "request", side_effect=AssertionError("Screenshots must stay offline")))
        import word_lookup as w
        # Isolate config, persistence, hooks and remote services from the user's
        # desktop. Widgets and their real layout/build methods remain unchanged.
        patches.enter_context(patch.object(w, "config_path", lambda: str(Path(data_dir) / "config.json")))
        patches.enter_context(patch.object(w, "cfg", copy.deepcopy(w.DEFAULT_CONFIG)))
        patches.enter_context(patch.object(w, "_saved_words", []))
        patches.enter_context(patch.object(w, "_card_cache", {}))
        patches.enter_context(patch.object(w.HotkeyManager, "start", lambda self: None))
        patches.enter_context(patch.object(w.mouse.Listener, "start", lambda self: None))
        patches.enter_context(patch.object(w.App, "_setup_tray", lambda self: None))
        patches.enter_context(patch.object(w.App, "_check_ocr", lambda self: None))
        original_tk = w.tk.Tk
        display = {}

        def scaled_root(*args, **kwargs):
            root = original_tk(*args, **kwargs)
            try:
                display.update(_configure_scaling(root, scale_percent))
            except Exception:
                root.destroy()
                raise
            return root

        patches.enter_context(patch.object(w.tk, "Tk", scaled_root))
        w.cfg["popup"]["timeout"] = 0
        w.cfg["freeze"]["pause_game"] = False
        w.cfg["freeze"]["selection_mode"] = "keyboard"
        app = w.App()
        display["application_font"] = w.FONT
        display["resolved_default_font"] = w.tkfont.nametofont("TkDefaultFont").actual()
        display["windows"] = _windows_display_info(app.root)
        files = []
        try:
            _settle(app.root)
            app.root.withdraw()
            image, words = _backdrop(app.root.winfo_screenwidth(), app.root.winfo_screenheight())
            card = copy.deepcopy(w.SAMPLE_CARD)
            card.update(source="Example game", context=card["sentence"], _context_image=image)
            app.show_lookup(card, None, (150, 65, 260, 95))
            files.append(_capture(app.root, app.popup.win, output / "card.png"))
            app.dismiss_lookup()

            # Preserve compact application defaults while exercising every optional
            # block in a separate card. At larger scales, capture its lower scroll
            # position too so definitions, chips and footer can be reviewed.
            sections, popup_options = copy.deepcopy(w.cfg["sections"]), copy.deepcopy(w.cfg["popup"])
            try:
                w.cfg["sections"].update({name: True for name in sections})
                w.cfg["popup"].update({name: True for name, value in popup_options.items() if isinstance(value, bool)})
                app.show_lookup(card, None, (150, 65, 260, 95))
                files.append(_capture(app.root, app.popup.win, output / "card-detailed.png"))
                app.popup.viewport.canvas.yview_moveto(1.0)
                files.append(_capture(app.root, app.popup.win, output / "card-detailed-bottom.png"))
                app.dismiss_lookup()
            finally:
                w.cfg["sections"] = sections
                w.cfg["popup"] = popup_options

            w.add_saved_word(card)
            for word, translation in (("signal", "сигнал"), ("passage", "прохід")):
                extra = dict(card, word=word, base=word, translation=translation)
                w.add_saved_word(extra)
            for entry in w.load_saved_words():
                entry["added"] = "2026-01-01T12:00:00"
            app.open_saved_words()
            width = min(1200, max(980, app.root.winfo_screenwidth() - 80))
            height = min(740, max(540, app.root.winfo_screenheight() - 80))
            app.saved_win.geometry(f"{width}x{height}+20+20")
            app.show_saved_details(card["word"])
            files.append(_capture(app.root, app.saved_win, output / "library.png"))
            app._hide_saved_window()

            def grab_fixture(*args, **kwargs):
                return image.copy(), 0, 0, image.width, image.height

            patches.enter_context(patch.object(w, "_grab", grab_fixture))
            patches.enter_context(patch.object(w, "ocr_region", lambda *args, **kwargs: [w.W(*word) for word in words]))
            selector = w.RegionSelector(app, lambda *args: None)
            try:
                _settle(app.root, lambda: getattr(selector, "navigator", None) is not None)
                files.append(_capture(app.root, selector.win, output / "word-selection.png"))
            finally:
                selector.finish(None)

            image, words = _backdrop(app.root.winfo_screenwidth(), app.root.winfo_screenheight(), light=True)
            selector = w.RegionSelector(app, lambda *args: None)
            try:
                _settle(app.root, lambda: getattr(selector, "navigator", None) is not None)
                files.append(_capture(app.root, selector.win, output / "word-selection-light.png"))
            finally:
                selector.finish(None)

            image, words = _backdrop(app.root.winfo_screenwidth(), app.root.winfo_screenheight())
            cursor = w.FreeCursorOverlay(app, (words[0][1] + 30, words[0][2] + 16), lambda *args: None)
            try:
                files.append(_capture(app.root, cursor.win, output / "cursor-overlay.png"))
            finally:
                cursor.finish(None)
        finally:
            app.quit()
        (output / "manifest.json").write_text(json.dumps({
            "platform": sys.platform, "python": sys.version.split()[0],
            "tk": str(w.tk.TkVersion), "fixtures": "local synthetic game scene and sample cards",
            "display": display,
            "screenshots": files,
        }, indent=2), encoding="utf-8")
        for item in files:
            print(f"{output / item['file']} ({item['width']}x{item['height']})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/screenshots"))
    parser.add_argument("--scale-percent", type=int, choices=(100, 125, 150), default=100,
                        help="Windows: controlled Tk scaling at 96 DPI × this percentage; Linux uses native scaling")
    args = parser.parse_args()
    if sys.platform != "win32" and args.scale_percent != 100:
        parser.error("125% and 150% variants require Windows and Segoe UI")
    render(args.output_dir.resolve(), args.scale_percent)


if __name__ == "__main__":
    main()
