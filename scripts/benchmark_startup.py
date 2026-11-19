"""Measure real Windows EXEs from process creation to a responsive main window.

Every trial uses its own temporary profile. Only processes launched here are
stopped afterwards. Results describe this machine and its file cache; they do
not simulate a reboot, antivirus scan, or the user's hardware.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time


class Windows:
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = [self.callback_type, wintypes.LPARAM]
        self.user.EnumWindows.restype = wintypes.BOOL
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsWindowVisible.restype = wintypes.BOOL
        self.user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.GetWindowTextW.restype = ctypes.c_int
        self.user.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
            wintypes.LPARAM, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        self.user.SendMessageTimeoutW.restype = ctypes.c_ssize_t

    def responsive_window(self, pid):
        windows = []
        @self.callback_type
        def visit(hwnd, _parameter):
            owner = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and self.user.IsWindowVisible(hwnd):
                title = ctypes.create_unicode_buffer(256)
                self.user.GetWindowTextW(hwnd, title, len(title))
                if title.value == "Word Lookup":
                    windows.append(hwnd)
            return True
        self.user.EnumWindows(visit, 0)
        for hwnd in windows:
            result = ctypes.c_size_t()
            # WM_NULL, SMTO_BLOCK | SMTO_ABORTIFHUNG: the GUI must process a message.
            if self.user.SendMessageTimeoutW(hwnd, 0, 0, 0, 3, 50, ctypes.byref(result)):
                return True
        return False


def trial(executable, windows, timeout):
    with tempfile.TemporaryDirectory(prefix="wordlookup-startup-") as profile:
        started = time.perf_counter()
        process = subprocess.Popen([str(executable)], cwd=executable.parent,
            env={**os.environ, "WORD_LOOKUP_DATA_DIR": profile},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            while time.perf_counter() - started < timeout:
                if process.poll() is not None:
                    raise RuntimeError(f"EXE exited before its main window: {process.returncode}")
                if windows.responsive_window(process.pid):
                    return time.perf_counter() - started
                time.sleep(.01)
            raise TimeoutError(f"Main window was not responsive within {timeout:g} seconds")
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3, choices=range(1, 11))
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This benchmark requires Windows and the actual packaged EXEs.")
    variants = {"before": args.before.resolve(), "after": args.after.resolve()}
    if not all(path.is_file() for path in variants.values()):
        parser.error("Both EXEs must exist.")
    if not 1 <= args.timeout <= 120:
        parser.error("Timeout must be between 1 and 120 seconds.")
    windows = Windows()
    samples = {name: [] for name in variants}
    for iteration in range(args.runs):
        # Alternate order to reduce a consistent advantage from measuring second.
        order = ("before", "after") if iteration % 2 == 0 else ("after", "before")
        for name in order:
            samples[name].append(trial(variants[name], windows, args.timeout))
    medians = {name: statistics.median(values) for name, values in samples.items()}
    report = {"platform": sys.platform, "python": sys.version.split()[0],
        "metric": "process creation to visible, responsive Word Lookup main window",
        "seconds": samples, "median_seconds": medians,
        "median_reduction_percent": 100 * (1 - medians["after"] / medians["before"]),
        "limitations": "Same Windows machine; isolated empty profiles; real listeners and settings UI. "
            "No reboot/file-cache flush; EXE build/self-test may warm files. Not user-PC or Mafia FPS data."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
