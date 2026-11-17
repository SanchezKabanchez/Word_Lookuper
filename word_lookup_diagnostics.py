"""Bounded diagnostics: redacted text reports and an optional private OCR crop."""
from collections import deque
from datetime import datetime, timezone
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import threading
import time
import zipfile

from word_lookup_version import __version__

DEPENDENCIES = ("requests", "pillow", "mss", "pynput", "psutil", "keyring", "pygame", "pystray",
                "rapidocr-onnxruntime", "onnxruntime", "pytesseract", "ctranslate2", "sentencepiece")


def environment_info(root=None) -> dict:
    result = {"app_version": __version__, "python": sys.version, "platform": platform.platform(),
              "dependencies": {}}
    for name in DEPENDENCIES:
        try:
            result["dependencies"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result["dependencies"][name] = None
    if root is not None:
        result["display"] = {"tk_version": str(root.tk.call("info", "patchlevel")),
            "tk_scaling_pixels_per_point": float(root.tk.call("tk", "scaling")),
            "screen_size": [root.winfo_screenwidth(), root.winfo_screenheight()],
            "window_geometry": root.geometry()}
        if sys.platform.startswith("win"):
            import ctypes
            from ctypes import wintypes
            user = ctypes.WinDLL("user32", use_last_error=True)
            if hasattr(user, "GetDpiForWindow"):
                user.GetDpiForWindow.argtypes = [wintypes.HWND]
                user.GetDpiForWindow.restype = wintypes.UINT
                result["display"]["native_window_dpi"] = user.GetDpiForWindow(root.winfo_id())
    return result


class DiagnosticsRecorder:
    def __init__(self, settings, redact):
        self.settings, self.redact = settings, redact
        self._events = deque(maxlen=2000)
        self._lock = threading.RLock()
        self._crop = None
        self._boxes = []
        self._next_sample = 0.
        self._process = None

    def record(self, name: str, **fields) -> None:
        if not self.settings()["diagnostics"]["enabled"]:
            return
        event = {"event": name, "utc": datetime.now(timezone.utc).isoformat(),
                 "monotonic": time.perf_counter(), **fields}
        with self._lock:
            self._events.append(event)

    def sample(self) -> None:
        if not self.settings()["diagnostics"]["enabled"] or time.monotonic() < self._next_sample:
            return
        self._next_sample = time.monotonic() + 1
        import psutil
        try:
            if self._process is None:
                self._process = psutil.Process()
            self.record("app.resources", cpu_percent=self._process.cpu_percent(),
                        rss_bytes=self._process.memory_info().rss, threads=self._process.num_threads())
        except psutil.Error as error:
            self.record("app.resources_unavailable", error=type(error).__name__)

    def capture_ocr(self, image, words, engine: str) -> None:
        options = self.settings()["diagnostics"]
        self.record("ocr.result", engine=engine, words=len(words), width=image.width, height=image.height)
        if not (options["enabled"] and options["include_ocr_image"]):
            return
        crop = image.copy().convert("RGB")
        crop.thumbnail((1024, 576))
        sx, sy = crop.width / image.width, crop.height / image.height
        boxes = [{"text": word.text, "box": [word.x0 * sx, word.y0 * sy, word.x1 * sx, word.y1 * sy]}
                 for word in words[:1000]]
        with self._lock:
            self._crop, self._boxes = crop, boxes

    def discard_image(self) -> None:
        with self._lock:
            self._crop, self._boxes = None, []

    def export(self, filename: str, report: str, environment: dict, library: dict) -> None:
        with self._lock:
            timeline = list(self._events)
            include = self.settings()["diagnostics"]["include_ocr_image"]
            crop = self._crop.copy() if include and self._crop is not None else None
            boxes = list(self._boxes) if crop is not None else []
        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".wordlookup-diagnostic-", delete=False) as handle:
                temporary = handle.name
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("report.txt", self.redact(report))
                for name, data in (("environment.json", environment), ("timeline.json", timeline),
                                   ("library-summary.json", library)):
                    archive.writestr(name, self.redact(json.dumps(data, ensure_ascii=False, indent=2)))
                if crop is not None:
                    from PIL import ImageDraw
                    draw = ImageDraw.Draw(crop)
                    for box in boxes:
                        draw.rectangle(box["box"], outline="#00d5ff", width=2)
                    stream = io.BytesIO()
                    crop.save(stream, "PNG")
                    archive.writestr("ocr-boxes.png", stream.getvalue())
                    archive.writestr("ocr-boxes.json", self.redact(json.dumps(boxes, ensure_ascii=False, indent=2)))
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.remove(temporary)
