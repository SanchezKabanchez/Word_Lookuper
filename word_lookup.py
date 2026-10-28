#!/usr/bin/env python3
"""
Word Lookup — словник «під курсором» у стилі macOS Look Up.
Працює скрізь: у браузері, PDF, а також в іграх, де текст неможливо виділити.

Як це працює
    Натискаєш кнопку (за замовчуванням середня кнопка миші або Ctrl+Alt+D) —
    програма робить знімок екрана навколо курсора, розпізнає слова (OCR),
    підсвічує слово під курсором і показує картку:
        English – Ukrainian   переклад за частинами мови
        English               визначення та приклади
        English Thesaurus     синоніми й антоніми

Встановлення
    pip install pynput pyperclip deep-translator requests mss pillow
    + ОДИН з OCR-рушіїв:
        pip install rapidocr-onnxruntime         (найпростіше, лише pip)
      або Tesseract (точніші межі слів):
        Windows: https://github.com/UB-Mannheim/tesseract/wiki
        macOS:   brew install tesseract
        Linux:   sudo apt install tesseract-ocr
        і   pip install pytesseract
    Необов'язково:  pip install pygame   (озвучення)
                    pip install pystray  (значок у треї)
    Linux: sudo apt install python3-tk xclip

Запуск:  python word_lookup.py
Для ігор: режим «Вікно без рамки» (Borderless), інакше картка може не з'явитись.
"""

import copy
import base64
import hashlib
import html
import importlib.util
import io
import itertools
import json
import math
import ntpath
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk
import tkinter.font as tkfont

import pyperclip
import requests
from deep_translator import GoogleTranslator
from pynput import keyboard, mouse

APP_NAME = "Word Lookup"
FONT = "Segoe UI" if sys.platform.startswith("win") else "Helvetica" if sys.platform == "darwin" else "DejaVu Sans"
KEY = "#ff00fe"  # колір-ключ прозорості для заокруглених кутів (Windows)


def _foreground_window():
    """Яке вікно зараз активне (тільки Windows) — щоб повернути йому фокус після
    закриття нашої накладки. Без цього гра іноді лишається без фокусу, курсор миші
    виглядає «залоченим», і допомагає лише випадковий клік по Пуску/панелі задач,
    що змушує Windows переоцінити активне вікно."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes
        fn = ctypes.windll.user32.GetForegroundWindow
        fn.restype = wintypes.HWND
        return fn()
    except Exception:
        return None


def _restore_foreground(hwnd) -> None:
    if not hwnd or not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        from ctypes import wintypes
        fn = ctypes.windll.user32.SetForegroundWindow
        fn.argtypes = [wintypes.HWND]
        fn.restype = wintypes.BOOL
        fn(hwnd)
    except Exception:
        pass


def release_cursor_clip() -> bool:
    """Знімає обмеження руху курсора (ClipCursor), яким гра тримає мишу в центрі екрана —
    це те саме, що відбувається, коли натиснути «Пуск». Лише Windows; безпечно викликати
    будь-коли й скільки завгодно разів (нічого не ламає, лише відпускає курсор)."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.ClipCursor(None))
    except Exception:
        return False


def _foreground_title() -> str:
    """Заголовок активного вікна (тільки Windows) — з нього беремо назву гри для тегу
    в Anki. На інших системах повертає порожній рядок."""
    if not sys.platform.startswith("win"):
        return ""
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception:
        return ""


_IGNORED_TITLES = {"", "program manager"}


def _game_source() -> str:
    t = " ".join(_foreground_title().split())
    return "" if (t.lower() in _IGNORED_TITLES or t == APP_NAME) else t[:80]


# Окремий процес володіє парою suspend/resume. Він відновлює гру, якщо Word Lookup
# завершується, закриває канал або перевищує ліміт, без залежності від Tk/mainloop.
_PAUSE_WATCHDOG = r'''
import os, sys, time, threading, queue
q = queue.Queue()
def commands():
    pending = b""
    try:
        while True:
            chunk = os.read(0, 4096)
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                q.put(line.decode("ascii", errors="replace"))
    finally:
        q.put(None)
threading.Thread(target=commands, daemon=True).start()
paused = False
target = None
def same(proc, created):
    try:
        return proc.is_running() and proc.create_time() == created
    except Exception:
        return False
try:
    import psutil
    parent = psutil.Process(int(sys.argv[1]))
    parent_created = float(sys.argv[2])
    target = psutil.Process(int(sys.argv[3]))
    created = float(sys.argv[4])
    ttl = max(0.2, min(300.0, float(sys.argv[5])))
    if not same(parent, parent_created) or not same(target, created):
        raise RuntimeError("Process identity changed")
    if target.pid in (os.getpid(), parent.pid) or target.status() == psutil.STATUS_STOPPED:
        raise RuntimeError("Target is not eligible for pause")
    print("READY", flush=True)
    if q.get(timeout=5) == "ARM" and same(parent, parent_created):
        if not same(target, created):
            raise RuntimeError("Target exited before pause")
        target.suspend()
        paused = True
        deadline = time.monotonic() + ttl
        print("PAUSED", flush=True)
        while time.monotonic() < deadline and same(parent, parent_created) and same(target, created):
            try:
                command = q.get(timeout=0.05)
                if command is None or command == "RESUME":
                    break
            except queue.Empty:
                pass
except Exception as e:
    print("ERROR: " + type(e).__name__ + ": " + str(e), flush=True)
finally:
    if paused:
        if not same(target, created):
            print("TARGET_EXITED", flush=True)
        else:
            for attempt in range(3):
                try:
                    target.resume()
                    print("RESUMED", flush=True)
                    break
                except Exception as e:
                    if attempt == 2:
                        print("ERROR: Resume failed: " + str(e), flush=True)
                    time.sleep(0.1)
'''


class GamePauseGuard:
    """Контрольована пауза одного PID з перевіркою часу створення і зовнішнім сторожем."""

    def __init__(self, pid, created, seconds=120):
        self.pid, self.created, self.seconds = int(pid), float(created), float(seconds)
        self.process = None
        self.lines = queue.Queue()
        self.last_status = ""
        self.reader = None

    def _read_output(self):
        pending = b""
        try:
            while True:
                chunk = os.read(self.process.stdout.fileno(), 4096)
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    self.last_status = line.decode("utf-8", errors="replace").strip()
                    self.lines.put(self.last_status)
        finally:
            self.lines.put(None)

    def _expect(self, expected):
        try:
            line = self.lines.get(timeout=4)
        except queue.Empty:
            raise RuntimeError("Сторож паузи не відповів вчасно")
        if line != expected:
            raise RuntimeError(line or "Сторож паузи завершився")

    def start(self):
        if self.process is not None:
            raise RuntimeError("Пауза вже запущена")
        import psutil
        if self.pid == os.getpid() or self.pid <= 0:
            raise ValueError("Не можна призупинити Word Lookup або системний PID")
        parent = psutil.Process()
        flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-c", _PAUSE_WATCHDOG, str(parent.pid), str(parent.create_time()),
             str(self.pid), str(self.created), str(self.seconds)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=flags)
        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.reader.start()
        try:
            self._expect("READY")
            self.process.stdin.write(b"ARM\n")
            self.process.stdin.flush()
            self._expect("PAUSED")
        except Exception:
            self.release()
            raise
        return self

    @property
    def active(self):
        return self.process is not None and self.process.poll() is None

    def release(self):
        if self.process is None:
            return True, ""
        if self.process.stdin and not self.process.stdin.closed:
            try:
                self.process.stdin.write(b"RESUME\n")
                self.process.stdin.flush()
            except (OSError, ValueError):
                pass
            finally:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # Не вбиваємо сторожа: він ще має відновити гру після свого ліміту.
            return False, "Сторож ще працює; автоматичне відновлення — після ліміту паузи"
        if self.reader:
            self.reader.join(timeout=0.2)
        if self.process.stdout:
            self.process.stdout.close()
        return self.last_status in ("RESUMED", "TARGET_EXITED"), self.last_status


_NON_GAME_EXES = frozenset({
    "python.exe", "pythonw.exe", "python3.exe", "explorer.exe", "dwm.exe", "steam.exe",
    "steamwebhelper.exe", "epicgameslauncher.exe", "epicwebhelper.exe", "galaxyclient.exe",
    "ubisoftconnect.exe", "upc.exe", "eadesktop.exe", "ealauncher.exe", "origin.exe",
    "battle.net.exe", "riotclientservices.exe", "rockstargameslauncher.exe",
    "svchost.exe", "csrss.exe", "winlogon.exe", "services.exe", "lsass.exe", "sihost.exe",
    "runtimebroker.exe", "applicationframehost.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe", "textinputhost.exe",
    "systemsettings.exe", "taskmgr.exe", "taskhostw.exe", "securityhealthsystray.exe",
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "iexplore.exe",
    "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "conhost.exe", "openconsole.exe",
    "code.exe", "cursor.exe", "devenv.exe", "idea64.exe", "rider64.exe", "notepad.exe", "notepad++.exe",
    "discord.exe", "obs64.exe", "obs32.exe", "vlc.exe", "mpv.exe", "wmplayer.exe", "spotify.exe",
    "blender.exe", "aseprite.exe", "unity.exe", "unrealeditor.exe", "ue4editor.exe",
    "wallpaper32.exe", "wallpaper64.exe", "calculatorapp.exe",
})
_KNOWN_GAME_EXES = frozenset({
    "cyberpunk2077.exe", "hogwartslegacy.exe", "rdr2.exe", "eldenring.exe", "witcher3.exe",
    "bg3.exe", "bg3_dx11.exe", "skyrimse.exe", "skyrim.exe", "fallout4.exe", "starfield.exe",
    "gta5.exe", "gta5_enhanced.exe", "kingdomcome.exe", "kingdomcome2.exe", "terraria.exe",
    "stardew valley.exe", "hollow_knight.exe", "hades.exe", "hades2.exe", "factorio.exe",
})
_game_registry_cache = {"until": 0.0, "paths": set()}
_last_game_detection = {}


def windows_exe_path(path):
    if not isinstance(path, str) or not path.strip():
        return ""
    value = path.strip().replace("/", "\\")
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normpath(value).replace("\\", "/").casefold()


def non_game_process(name, exe_path=""):
    name = name.strip().casefold()
    if (name in _NON_GAME_EXES or re.fullmatch(r"python(?:w|\d+(?:\.\d+)*w?)?\.exe", name)
            or any(part in name for part in ("launcher", "crashreport", "crashhandler", "anticheat"))):
        return True
    path = windows_exe_path(exe_path)
    system = windows_exe_path(os.environ.get("SystemRoot", r"C:\Windows"))
    return bool(path and system and (path == system or path.startswith(system + "/")))


def registered_game_paths():
    """Локальні позначки Windows Game Mode; не скануємо весь список процесів."""
    if not sys.platform.startswith("win"):
        return set()
    now = time.monotonic()
    if now < _game_registry_cache["until"]:
        return _game_registry_cache["paths"]
    paths = set()
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"System\GameConfigStore\Children") as parent:
            for i in range(winreg.QueryInfoKey(parent)[0]):
                try:
                    with winreg.OpenKey(parent, winreg.EnumKey(parent, i)) as child:
                        value = winreg.QueryValueEx(child, "MatchedExeFullPath")[0]
                        if isinstance(value, str) and value:
                            paths.add(windows_exe_path(value))
                except OSError:
                    continue
    except OSError:
        pass
    _game_registry_cache.update(until=now + 60, paths=paths)
    return paths


def game_detection_reason(name, exe_path, registered=()):
    """Консервативні ознаки гри; звичайне повноекранне вікно не є такою ознакою."""
    name, path = name.strip().casefold(), windows_exe_path(exe_path)
    if (non_game_process(name, exe_path) or not path or not ntpath.isabs(path)
            or ntpath.basename(path) != name):
        return ""
    if path in registered:
        return "Windows Game Mode"
    if re.search(r"-(?:win64|win32)-shipping\.exe$", name):
        return "Unreal Engine Shipping"
    if name in _KNOWN_GAME_EXES:
        return "відома гра"
    parts = path.split("/")
    for marker, label in ((["steamapps", "common"], "Steam"), (["epic games"], "Epic Games"),
                          (["gog galaxy", "games"], "GOG"), (["gog games"], "GOG"),
                          (["xboxgames"], "Xbox"), (["ea games"], "EA"),
                          (["ubisoft game launcher", "games"], "Ubisoft"),
                          (["rockstar games"], "Rockstar")):
        for i in range(len(parts) - len(marker) - 1):
            if parts[i:i + len(marker)] == marker:
                folder = parts[i + len(marker)]
                if folder not in {"launcher", "launchers", "redist", "redistributables", "support", "tools"}:
                    return label
    return ""


def foreground_process_info():
    """Один знімок активного вікна та процесу, без зміни його стану."""
    if not sys.platform.startswith("win"):
        raise RuntimeError("Пауза процесу гри доступна лише у Windows")
    import ctypes
    from ctypes import wintypes
    import psutil
    hwnd = _foreground_window()
    pid = wintypes.DWORD()
    user = ctypes.windll.user32
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value or pid.value in (os.getpid(), os.getppid()):
        raise RuntimeError("Повернись у гру та натисни Ctrl+Alt+R або Ctrl+Alt+G")
    target = psutil.Process(pid.value)
    with target.oneshot():
        info = {"pid": target.pid, "created": target.create_time(), "name": target.name(),
                "status": target.status(), "hwnd": hwnd, "title": _foreground_title()}
        try:
            info["exe"] = target.exe()
        except psutil.AccessDenied:
            info["exe"] = ""
    if not target.is_running():
        raise RuntimeError("Процес завершився під час визначення гри")
    return info


def foreground_game(expected_exe=""):
    """Порожня назва — автовизначення; непорожня — точний ручний вибір .exe."""
    global _last_game_detection
    _last_game_detection = {}
    info = foreground_process_info()
    if info["pid"] in (0, os.getpid(), os.getppid()) or non_game_process(info["name"], info["exe"]):
        raise RuntimeError("Це службова або звичайна програма; повернись у вікно гри")
    name = expected_exe.strip().casefold()
    if name:
        if (not name.endswith(".exe") or len(name) <= 4 or ntpath.basename(name) != name
                or ntpath.splitdrive(name)[0] or non_game_process(name)):
            raise RuntimeError("Укажи точну назву процесу гри, наприклад HogwartsLegacy.exe")
        if info["name"].casefold() != name:
            raise RuntimeError("Активне вікно не належить грі, указаній у налаштуваннях")
        reason = "ручний вибір"
    else:
        reason = game_detection_reason(info["name"], info["exe"], registered_game_paths())
        if not reason:
            raise RuntimeError("Не вдалося впевнено розпізнати гру в активному вікні. "
                               "Вимкни автовизначення й укажи її .exe вручну.")
    import psutil
    if info["status"] == psutil.STATUS_STOPPED:
        raise RuntimeError("Цей процес уже призупинений іншим інструментом")
    if _foreground_window() != info["hwnd"]:
        raise RuntimeError("Активне вікно змінилось. Повернись у гру й повтори гарячу клавішу.")
    _last_game_detection = {**info, "reason": reason}
    return info["pid"], info["created"], info["hwnd"]


os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
AUDIO_OK = importlib.util.find_spec("pygame") is not None

if sys.platform.startswith("win"):  # координати миші = фізичні пікселі
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

TRAY_OK = False
if sys.platform != "darwin":
    try:
        import pystray  # noqa: F401
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
        TRAY_OK = True
    except Exception:
        pass

# ============================== Конфігурація ===============================
DEFAULT_CONFIG = {
    "version": 4,
    "hotkeys": {
        "lookup_word": "ctrl+alt+d",
        "select_region": "ctrl+alt+r",
        "free_cursor": "ctrl+alt+g",
        "unlock_cursor": "ctrl+alt+u",
        "translate_line": "ctrl+alt+l",
        "translate_selection": "ctrl+alt+t",
        "speak": "ctrl+alt+s",
        "copy_translation": "",
        "toggle_pause": "",
        "close_popup": "",
        "resume_game": "ctrl+alt+p",
    },
    "mouse": {"button": "middle", "modifier": "none"},
    "languages": {"target": "uk"},
    "sections": {"bilingual": True, "definitions": True, "thesaurus": True},
    "dictionary": {"max_translations": 8, "max_definitions": 2, "max_synonyms": 5},
    "ocr": {"engine": "auto", "tesseract_cmd": "", "region_w": 480,
            "region_h": 100, "upscale": 2.0, "highlight": True,
            "split_merged_words": True},
    "popup": {"theme": "light", "font_size": 10, "width": 400, "opacity": 1.0,
              "timeout": 60, "highlight_color": "#ffee00", "btn_speak": True,
              "sentence": True, "sentence_translation": True, "translate_definition": True,
              "phonetic": True, "examples": True, "alternate_translations": True,
              "context_image": True},
    "cache": {"max_entries": 1000},
    "deepl": {"enabled": True, "key": ""},
    "anki": {"game": "", "auto_game_tag": True, "deck": "Word Lookup"},
    "freeze": {"hover_highlight": True, "pause_game": False, "auto_game": True,
               "game_exe": "", "pause_seconds": 120},
    "auto": {"enabled": False, "drag_px": 12, "double_click_s": 0.35},
    "app": {"start_minimized": False},
    "learning": {"capture_context": False},
}

ACTIONS = [
    ("lookup_word", "Знайти слово під курсором (OCR)"),
    ("select_region", "Виділити рамкою слово чи рядок (як скріншот)"),
    ("free_cursor", "Керований курсор клавіатурами (для ігор із захопленою мишею)"),
    ("unlock_cursor", "Відкріпити курсор (зняти обмеження, яким гра тримає мишу в центрі)"),
    ("translate_line", "Перекласти рядок під курсором (OCR)"),
    ("translate_selection", "Перекласти виділений текст"),
    ("speak", "Озвучити слово"),
    ("copy_translation", "Скопіювати переклад"),
    ("toggle_pause", "Пауза / продовжити"),
    ("close_popup", "Закрити картку"),
    ("resume_game", "Відновити призупинену гру й закрити накладку"),
]

THEMES = {
    "light": dict(bg="#ffffff", fg="#202c40", dim="#59677b", head="#455978",
                  line="#e3e9f2", link="#2864dc", example="#53657b", border="#d5deed"),
    "dark": dict(bg="#202936", fg="#edf2fa", dim="#a8b7ca", head="#bdcce1",
                 line="#3a485d", link="#87b7ff", example="#c0cddd", border="#45546b"),
    "sepia": dict(bg="#f4ecd8", fg="#3b2f1e", dim="#8a7a60", head="#6b5a40",
                  line="#d8cba8", link="#a4562b", example="#4d3f2a", border="#cdbf9f"),
}
THEME_NAMES = {"Світла": "light", "Темна": "dark", "Сепія": "sepia"}
CARD_CONTENT_OPTIONS = (
    (("sections", "bilingual"), "Переклад слова"),
    (("popup", "sentence"), "Речення мовою оригіналу"),
    (("popup", "sentence_translation"), "Переклад речення"),
    (("popup", "phonetic"), "Транскрипція"),
    (("sections", "definitions"), "Визначення англійською"),
    (("popup", "translate_definition"), "Переклад визначення"),
    (("popup", "examples"), "Приклади вживання"),
    (("sections", "thesaurus"), "Синоніми й антоніми"),
    (("popup", "alternate_translations"), "Додаткові варіанти перекладу"),
    (("popup", "context_image"), "Зображення контексту"),
    (("popup", "btn_speak"), "Кнопки озвучення"),
)
LANG_NAMES = {"Українська": "uk", "Польська": "pl", "Німецька": "de", "Французька": "fr",
              "Іспанська": "es", "Італійська": "it", "Чеська": "cs", "Турецька": "tr"}
LANG_EN = {"uk": "Ukrainian", "pl": "Polish", "de": "German", "fr": "French",
           "es": "Spanish", "it": "Italian", "cs": "Czech", "tr": "Turkish"}
MOUSE_BTN_NAMES = {"Вимкнено": "none", "Середня кнопка (колесо)": "middle",
                   "Бічна кнопка «назад»": "x1", "Бічна кнопка «вперед»": "x2"}
MOUSE_MOD_NAMES = {"Без клавіші": "none", "Ctrl": "ctrl", "Alt": "alt", "Shift": "shift"}
ENGINE_NAMES = {"Авто": "auto", "Tesseract": "tesseract", "RapidOCR": "rapidocr"}
MOUSE_BTN_ALIASES = {"middle": {"middle"}, "x1": {"x1", "button8"}, "x2": {"x2", "button9"}}


def config_path() -> str:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "WordLookup", "config.json")


def _merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if k in base:
            if isinstance(base[k], dict) and isinstance(v, dict):
                _merge(base[k], v)
            elif not isinstance(base[k], dict) and isinstance(v, type(base[k])):
                base[k] = v


def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(config_path(), encoding="utf-8") as f:
            saved = json.load(f)
        saved_version = saved.get("version", 1) if isinstance(saved, dict) else 1
        if not isinstance(saved_version, int):
            saved_version = 1
        if isinstance(saved, dict) and saved_version < DEFAULT_CONFIG["version"]:
            if saved_version < 2 and isinstance(saved.get("ocr"), dict):  # нові, швидші значення за замовчуванням
                for k in ("region_w", "region_h", "upscale"):
                    saved["ocr"].pop(k, None)
            if saved_version < 3:
                # раніше картка висіла нескінченно (0 = ніколи не закривати); якщо це старе
                # значення ще не чіпали — підхопити новий типовий тайм-аут в 1 хвилину
                if isinstance(saved.get("popup"), dict) and saved["popup"].get("timeout") == 0:
                    saved["popup"].pop("timeout", None)
            if saved_version < 4:
                # Замінюємо лише колишній типовий розмір; інші власні розміри лишаються.
                if isinstance(saved.get("popup"), dict) and saved["popup"].get("font_size") == 12:
                    saved["popup"]["font_size"] = 10
                old_popup = saved.get("popup")
                if isinstance(old_popup, dict) and "sentence_translation" not in old_popup \
                        and isinstance(old_popup.get("sentence"), bool):
                    old_popup["sentence_translation"] = old_popup["sentence"]
            saved["version"] = DEFAULT_CONFIG["version"]
        if isinstance(saved, dict):
            _merge(cfg, saved)
            old_hotkeys = saved.get("hotkeys", {})
            if isinstance(old_hotkeys, dict) and "resume_game" not in old_hotkeys:
                resume = cfg["hotkeys"]["resume_game"]
                if resume and any(combo == resume for action, combo in cfg["hotkeys"].items()
                                  if action != "resume_game"):
                    cfg["hotkeys"]["resume_game"] = ""
    except (OSError, ValueError):
        pass
    return cfg


def atomic_json_write(path, data, indent=2):
    """Не залишаємо половину файлу при збої; у тимчасових файлів приватні права."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".wordlookup-", delete=False) as f:
            tmp = f.name
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


def save_config(cfg: dict) -> None:
    try:
        atomic_json_write(config_path(), cfg)
    except OSError as e:
        print(f"Не вдалося зберегти налаштування: {e}")


cfg = load_config()
events: "queue.Queue[tuple]" = queue.Queue()
_token_counter = itertools.count(1)  # унікальний номер кожного пошуку слова


def post_lookup(token, kind, *args):
    """Перевірка актуальності відбувається при обробці, а не лише перед чергою."""
    events.put(("lookup_event", token, (kind, *args)))


def clamp(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


# ============================== Гарячі клавіші =============================
MOD_ORDER = ["ctrl", "alt", "alt_gr", "shift", "cmd"]
MODS = set(MOD_ORDER)
MOD_KEYS = {"ctrl": keyboard.Key.ctrl, "alt": keyboard.Key.alt,
            "alt_gr": keyboard.Key.alt_gr, "shift": keyboard.Key.shift,
            "cmd": keyboard.Key.cmd}
COPY_MOD = keyboard.Key.cmd if sys.platform == "darwin" else keyboard.Key.ctrl
PRETTY = {"ctrl": "Ctrl", "alt": "Alt", "alt_gr": "AltGr", "shift": "Shift",
          "cmd": "Cmd" if sys.platform == "darwin" else "Win", "plus": "+"}
RESERVED = {"ctrl+c", "ctrl+v", "ctrl+x", "ctrl+a", "ctrl+z", "cmd+c", "cmd+v", "cmd+x"}


def pretty(combo: str) -> str:
    if not combo:
        return "— не задано —"
    out = []
    for p in combo.split("+"):
        if p in PRETTY:
            out.append(PRETTY[p])
        elif len(p) == 1 or (p[0] == "f" and p[1:].isdigit()):
            out.append(p.upper())
        else:
            out.append(p.replace("_", " ").capitalize())
    return " + ".join(out)


def combo_problem(combo: str):
    parts = combo.split("+")
    key, mods = parts[-1], parts[:-1]
    if not mods and (len(key) == 1 or key.startswith("vk")):
        return "Для літер і цифр потрібна хоча б одна клавіша-модифікатор (Ctrl, Alt, Shift)."
    if combo in RESERVED:
        return "Ця комбінація зарезервована системою (копіювання/вставлення)."
    return None


def norm_key(key):
    """Перетворює подію pynput на ім'я клавіші (незалежно від розкладки на Windows)."""
    if isinstance(key, keyboard.Key):
        n = key.name
        if n == "alt_gr":
            return n
        for base in ("ctrl", "alt", "shift", "cmd"):
            if n == base or n.startswith(base + "_"):
                return base
        return n
    vk = getattr(key, "vk", None)
    if sys.platform.startswith("win") and vk is not None:
        if 0x41 <= vk <= 0x5A:
            return chr(vk).lower()
        if 0x30 <= vk <= 0x39:
            return chr(vk)
    ch = getattr(key, "char", None)
    if ch:
        if ord(ch) < 32:
            ch = chr(ord(ch) + 96)
        return "plus" if ch == "+" else ch.lower()
    return f"vk{vk}" if vk is not None else None


class HotkeyManager:
    """Один глобальний слухач клавіатури: розпізнає комбінації й записує нові."""
    STALE_AFTER = 30.0

    def __init__(self, on_action):
        self.on_action = on_action
        self.bindings = {}
        self.down = {}
        self.recorder = None
        self.ignore_until = 0.0
        self.on_esc = None
        self.raw_mode_cb = None  # поки задано — усі натискання йдуть напряму сюди
        self.listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self.listener.daemon = True

    def start(self):
        self.listener.start()

    def set_bindings(self, hotkeys: dict) -> None:
        self.bindings = {frozenset(c.split("+")): a for a, c in hotkeys.items() if c}

    def start_recording(self, cb) -> None:
        self.down.clear()
        self.recorder = cb

    def start_raw_mode(self, cb) -> None:
        """Усі подальші натискання клавіш ідуть напряму в cb(ім'я_клавіші), а не в
        звичайне зіставлення гарячих клавіш — працює незалежно від того, яке вікно
        зараз у фокусі (той самий глобальний хук, що й звичайні гарячі клавіші),
        тому спрацьовує навіть коли фокус лишається у грі."""
        self.down.clear()
        self.raw_mode_cb = cb

    def stop_raw_mode(self) -> None:
        self.raw_mode_cb = None

    def keys_down(self) -> bool:
        self._prune(time.time())
        return bool(self.down)

    def _prune(self, now):
        for k in [k for k, t in self.down.items() if now - t > self.STALE_AFTER]:
            del self.down[k]

    def _on_press(self, key):
        n = norm_key(key)
        if n is None:
            return
        now = time.time()
        first = n not in self.down
        self.down[n] = now
        self._prune(now)
        rec = self.recorder
        if rec is not None:
            self._record(rec, n, first)
            return
        if self.raw_mode_cb is not None:
            if first:
                self.raw_mode_cb(n)
            return
        if n == "esc" and first and self.on_esc:
            self.on_esc()
        if not first or now < self.ignore_until:
            return
        action = self.bindings.get(frozenset(self.down))
        if action:
            self.on_action(action)

    def _on_release(self, key):
        n = norm_key(key)
        if n is not None:
            self.down.pop(n, None)

    def _record(self, cb, n, first):
        mods = [m for m in MOD_ORDER if m in self.down]
        if n in MODS:
            cb("progress", "+".join(mods))
            return
        if not first:
            return
        self.recorder = None
        if n == "esc" and not mods:
            cb("cancel", "")
        else:
            cb("done", "+".join(mods + [n]))


# ============================== Словник ====================================
HEADERS = {"User-Agent": "Mozilla/5.0"}
SESSION = requests.Session()  # перевикористовує з'єднання — економить ~0.3 с на запит
SESSION.headers.update(HEADERS)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")
STRIP_CHARS = " \t\n.,;:!?\"'()[]{}<>“”‘’«»…—–*_/\\|"

UA_POS = {
    "noun": "іменник", "verb": "дієслово", "adjective": "прикметник",
    "adverb": "прислівник", "pronoun": "займенник", "preposition": "прийменник",
    "conjunction": "сполучник", "interjection": "вигук", "exclamation": "вигук",
    "article": "артикль", "abbreviation": "скорочення", "particle": "частка",
    "numeral": "числівник", "determiner": "детермінатив", "prefix": "префікс",
    "suffix": "суфікс", "phrase": "фраза", "auxiliary verb": "допоміжне дієслово",
    "modal verb": "модальне дієслово", "contraction": "скорочення",
}


def pos_name(pos: str) -> str:
    return UA_POS.get(pos, pos) if cfg["languages"]["target"] == "uk" else pos


class RateLimited(Exception):
    pass


class TranslateError(Exception):
    def __init__(self, message, limited=False):
        super().__init__(message)
        self.limited = limited


def _http_json(url, params, timeout=5):
    r = SESSION.get(url, params=params, timeout=timeout)
    if r.status_code == 429:
        raise RateLimited("HTTP 429")
    r.raise_for_status()
    return r.json()


def _max_tr() -> int:
    return int(clamp(cfg["dictionary"]["max_translations"], 1, 20))


def _p_gtx(text, sl, want_dict):
    """Неофіційний ендпойнт Google Translate (для слів — з частинами мови)."""
    data = _http_json("https://translate.googleapis.com/translate_a/single",
                      {"client": "gtx", "sl": sl, "tl": cfg["languages"]["target"],
                       "dt": ["t", "bd"] if want_dict else ["t"], "q": text})
    main = "".join(seg[0] for seg in (data[0] or []) if seg and seg[0])
    groups = []
    if want_dict and len(data) > 1 and data[1]:
        for entry in data[1]:
            if entry[1]:
                groups.append((pos_name(entry[0]), entry[1][:_max_tr()]))
    return main, groups


def _p_chrome(text, sl, want_dict):
    """Ендпойнт Google-словника для Chrome — окремий ліміт запитів."""
    data = _http_json("https://clients5.google.com/translate_a/t",
                      {"client": "dict-chrome-ex", "sl": sl,
                       "tl": cfg["languages"]["target"], "q": text})
    main, groups = "", []
    if isinstance(data, dict):
        main = "".join(s.get("trans", "") for s in data.get("sentences", []))
        if want_dict:
            for e in data.get("dict") or []:
                if e.get("terms"):
                    groups.append((pos_name(e.get("pos", "")), e["terms"][:_max_tr()]))
    elif isinstance(data, list) and data:
        first = data[0]
        while isinstance(first, list) and first:
            first = first[0]
        main = first if isinstance(first, str) else ""
    if not main:
        raise ValueError("порожня відповідь")
    return main, groups


def _p_deep(text, sl, want_dict):
    """Мобільна сторінка Google з явним тайм-аутом, як в інших джерел."""
    from html.parser import HTMLParser

    text = text.strip()
    if not text or sl == cfg["languages"]["target"]:
        return text, []
    if len(text) > 5000:
        raise ValueError("Завеликий текст для мобільного перекладача")

    class TranslationParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.depth = 0
            self.parts = []
            self.results = []

        def handle_starttag(self, tag, attrs):
            if self.depth:
                if tag == "br":
                    self.parts.append("\n")
                if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                                "link", "meta", "param", "source", "track", "wbr"}:
                    self.depth += 1
            elif tag == "div" and set(dict(attrs).get("class", "").split()) & {"t0", "result-container"}:
                self.depth = 1
                self.parts = []

        def handle_endtag(self, tag):
            if self.depth and tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                                          "link", "meta", "param", "source", "track", "wbr"}:
                self.depth -= 1
                if not self.depth:
                    self.results.append("".join(self.parts).strip())

        def handle_data(self, data):
            if self.depth:
                self.parts.append(data)

    response = SESSION.get("https://translate.google.com/m", params={
        "sl": sl, "tl": cfg["languages"]["target"], "q": text}, timeout=8)
    try:
        if response.status_code == 429:
            raise RateLimited("Google mobile: HTTP 429")
        response.raise_for_status()
        parser = TranslationParser()
        parser.feed(response.text)
        parser.close()
        out = next((item for item in parser.results if item), "")
        if not out:
            raise ValueError("Мобільний перекладач не повернув переклад")
        return out, []
    finally:
        response.close()


def _chunks(text, size=450):
    out, cur = [], ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > size:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return out or [text]


def _p_mymemory(text, sl, want_dict):
    """MyMemory — безкоштовний сервіс на іншій інфраструктурі (запасний варіант)."""
    sl = "en" if sl == "auto" else sl
    tl = cfg["languages"]["target"]
    chunks = _chunks(text)
    parts, groups = [], []
    for chunk in chunks:
        data = _http_json("https://api.mymemory.translated.net/get",
                          {"q": chunk, "langpair": f"{sl}|{tl}"})
        out = (data.get("responseData") or {}).get("translatedText") or ""
        if str(data.get("responseStatus")) == "429" or "MYMEMORY WARNING" in out.upper():
            raise RateLimited("MyMemory: ліміт")
        parts.append(out)
        if want_dict and len(chunks) == 1 and out:
            seen, alts = {out.lower()}, []
            for m in sorted(data.get("matches") or [],
                            key=lambda m: float(m.get("match") or 0), reverse=True):
                t = (m.get("translation") or "").strip()
                if (str(m.get("segment", "")).strip().lower() == text.lower() and t
                        and t.lower() not in seen and len(t) <= 40 and "\n" not in t):
                    seen.add(t.lower())
                    alts.append(t)
            groups = [("", [out] + alts[: _max_tr() - 1])]
    return " ".join(parts).strip(), groups


_PROVIDERS = (("Google", _p_gtx), ("Google (Chrome)", _p_chrome),
              ("Google (mobile)", _p_deep), ("MyMemory", _p_mymemory))
_cooldown: dict = {}  # ім'я -> (до якого часу пропускати, чи це ліміт запитів)


def translate_with_fallback(text: str, sl: str, want_dict: bool):
    """Пробує джерела по черзі; те, що видало ліміт (429), відпочиває 90 с."""
    limited = False
    for name, fn in _PROVIDERS:
        until, was_limit = _cooldown.get(name, (0, False))
        if time.time() < until:
            limited = limited or was_limit
            continue
        try:
            main, groups = fn(text, sl, want_dict)
        except RateLimited:
            _cooldown[name] = (time.time() + 90, True)
            limited = True
            log(f"{name}: ліміт запитів, пауза 90 с")
            continue
        except Exception as e:
            _cooldown[name] = (time.time() + 10, False)
            log(f"{name}: {type(e).__name__}: {e}")
            continue
        if main or groups:
            log(f"Переклад: {name}")
            return main, groups, name
    if limited:
        raise TranslateError("Google тимчасово обмежив запити з твоєї адреси. Спробуй за кілька "
                             "хвилин або зміни мережу (VPN, мобільний інтернет).", limited=True)
    raise TranslateError("Переклад недоступний — перевір інтернет-з'єднання.")


def base_form_candidates(word: str):
    """running -> run, cities -> city, that's -> that."""
    w = word.lower().replace("’", "'")
    out = [w]
    if "'" in w:
        out.append(w.split("'")[0])
    for suf, rep in (("ies", "y"), ("es", ""), ("s", ""), ("ing", ""),
                     ("ed", ""), ("ing", "e"), ("ed", "e")):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            stem = w[: -len(suf)] + rep
            out.append(stem)
            if len(stem) > 3 and stem[-1] == stem[-2]:
                out.append(stem[:-1])
    return list(dict.fromkeys(c for c in out if c))[:5]


DM_POS = {"n": "noun", "v": "verb", "adj": "adjective", "adv": "adverb", "u": ""}


def _datamuse_get(params, timeout=5):
    r = SESSION.get("https://api.datamuse.com/words", params=params, timeout=timeout)
    if r.status_code != 200:
        return []
    try:
        return r.json()
    except ValueError:
        return []


def datamuse_dictionary(word: str) -> dict:
    """Запасне джерело визначень і синонімів на основі WordNet — використовується,
    коли dictionaryapi.dev недоступний. Без транскрипції та аудіо."""
    empty = {"base": "", "phonetic": "", "audios": [], "meanings": []}
    base, defs_by_pos = "", {}
    for cand in base_form_candidates(word)[:3]:
        data = _datamuse_get({"sp": cand, "md": "d", "max": 1})
        if data and data[0].get("word", "").lower() == cand and data[0].get("defs"):
            base = cand
            for entry in data[0]["defs"]:
                pos_code, _, definition = entry.partition("	")
                defs_by_pos.setdefault(DM_POS.get(pos_code, pos_code), []).append(definition.strip())
            break
    if not base:
        return empty

    def rel(kind):
        return [d["word"] for d in _datamuse_get({f"rel_{kind}": base, "max": 15}) if d.get("word")]

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_syn, f_ant = pool.submit(rel, "syn"), pool.submit(rel, "ant")
        syn, ant = f_syn.result(), f_ant.result()

    meanings = [{"pos": pos_name(pos_en) if pos_en else "", "defs": [{"d": d, "e": ""} for d in defs],
                "syn": syn if i == 0 else [], "ant": ant if i == 0 else []}
               for i, (pos_en, defs) in enumerate(defs_by_pos.items())]
    return {"base": base, "phonetic": "", "audios": [], "meanings": meanings}


def _freedictionary_lookup(word: str) -> dict:
    """Транскрипція, аудіо, визначення, приклади, синоніми (dictionaryapi.dev)."""
    empty = {"base": "", "phonetic": "", "audios": [], "meanings": []}
    cands = base_form_candidates(word)[:3]

    def fetch(c):
        try:
            r = SESSION.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{c}", timeout=6)
        except Exception:
            return "err", None
        if r.status_code == 200:
            try:
                return "ok", r.json()
            except ValueError:
                return "err", None
        return ("missing" if r.status_code == 404 else "err"), None

    with ThreadPoolExecutor(max_workers=len(cands)) as pool:  # усі форми одночасно
        results = list(pool.map(fetch, cands))
    errors = [c for c, (status, _p) in zip(cands, results) if status == "err"]
    for cand, (status, payload) in zip(cands, results):
        if status != "ok":
            continue
        phonetic, audios, merged = "", {}, {}
        for entry in payload:
            phonetic = phonetic or entry.get("phonetic") or ""
            for p in entry.get("phonetics", []):
                phonetic = phonetic or p.get("text") or ""
                a = p.get("audio") or ""
                if a:
                    url = "https:" + a if a.startswith("//") else a
                    low = url.lower()
                    label = ("BrE" if ("-uk" in low or "_gb_" in low)
                             else "AmE" if ("-us" in low or "_us_" in low) else "")
                    audios.setdefault(label, url)
            for m in entry.get("meanings", []):
                pos = m.get("partOfSpeech", "")
                slot = merged.setdefault(pos, {"pos": pos, "defs": [], "syn": [], "ant": []})
                for d in m.get("definitions", []):
                    slot["defs"].append({"d": d.get("definition", ""), "e": d.get("example", "")})
                    slot["syn"] += d.get("synonyms", [])
                    slot["ant"] += d.get("antonyms", [])
                slot["syn"] += m.get("synonyms", [])
                slot["ant"] += m.get("antonyms", [])
        meanings = []
        for slot in merged.values():
            slot["syn"] = list(dict.fromkeys(slot["syn"]))
            slot["ant"] = list(dict.fromkeys(slot["ant"]))
            meanings.append(slot)
        labelled = [(lab, audios[lab]) for lab in ("BrE", "AmE") if lab in audios]
        chosen = labelled or list(audios.items())[:1]
        return {"base": cand, "phonetic": phonetic.strip("/ "),
                "audios": chosen, "meanings": meanings}
    if errors:
        raise RuntimeError(f"словник dictionaryapi.dev недоступний (перевірено: {', '.join(errors)})")
    return empty


def _datamuse_spellcheck(word: str):
    """Коли слова немає в жодному словнику — шукає найближче реальне англійське слово
    через нечіткий пошук Datamuse. Рятує від типових помилок OCR на стилізованих
    ігрових шрифтах (напр. «ccpyright» -> «copyright», «diskibution» -> «distribution»)."""
    try:
        data = _datamuse_get({"sp": word, "max": 3, "md": "d"})
    except Exception:
        return None
    for item in data:
        cand = item.get("word", "")
        if cand and cand.lower() != word.lower() and item.get("defs"):
            return cand
    return None


_IRREGULAR_SPEC = """
be: was were been
beat: beaten
become: became
begin: began begun
bend: bent
bind: bound
bite: bit bitten
bleed: bled
blow: blew blown
break: broke broken
breed: bred
bring: brought
build: built
burn: burnt
buy: bought
catch: caught
choose: chose chosen
cling: clung
come: came
creep: crept
deal: dealt
dig: dug
do: did done
draw: drew drawn
dream: dreamt
drink: drank drunk
drive: drove driven
eat: ate eaten
fall: fell fallen
feed: fed
feel: felt
fight: fought
find: found
flee: fled
fling: flung
fly: flew flown
forbid: forbade forbidden
forget: forgot forgotten
forgive: forgave forgiven
freeze: froze frozen
get: got gotten
give: gave given
go: went gone
grind: ground
grow: grew grown
hang: hung
have: had
hear: heard
hide: hid hidden
hold: held
keep: kept
kneel: knelt
know: knew known
lay: laid
lead: led
leap: leapt
leave: left
lend: lent
lie: lay lain
light: lit
lose: lost
make: made
mean: meant
mislead: misled
overcome: overcame
pay: paid
ride: rode ridden
ring: rang rung
rise: rose risen
run: ran
say: said
see: saw seen
seek: sought
sell: sold
send: sent
shake: shook shaken
shine: shone
shoot: shot
show: shown
shrink: shrank shrunk
sing: sang sung
sink: sank sunk
sit: sat
sleep: slept
slide: slid
speak: spoke spoken
spend: spent
spin: spun
spit: spat
spring: sprang sprung
stand: stood
steal: stole stolen
stick: stuck
sting: stung
stink: stank stunk
strike: struck
swear: swore sworn
sweep: swept
swim: swam swum
swing: swung
take: took taken
teach: taught
tear: tore torn
tell: told
think: thought
throw: threw thrown
tread: trod trodden
undergo: underwent undergone
understand: understood
undo: undid undone
wake: woke woken
wear: wore worn
weep: wept
win: won
wind: wound
withdraw: withdrew withdrawn
withstand: withstood
write: wrote written
arise: arose arisen
awake: awoke awoken
bear: bore borne
child: children
man: men
woman: women
foot: feet
tooth: teeth
mouse: mice
goose: geese
life: lives
knife: knives
wife: wives
wolf: wolves
half: halves
shelf: shelves
thief: thieves
loaf: loaves
good: better best
bad: worse worst
"""

IRREGULAR_FORMS = {}
for _line in _IRREGULAR_SPEC.strip().splitlines():
    _base, _, _forms = _line.partition(":")
    for _f in _forms.split():
        if _f != _base.strip():
            IRREGULAR_FORMS.setdefault(_f, _base.strip())

# форми, що є ще й самостійними словами (saw — пилка, left — лівий, found — заснувати…):
# для них спершу дивимось прямий словниковий запис і йдемо до початкової форми лише
# якщо він сам каже «past/plural/comparative … of …»
IRREGULAR_AMBIGUOUS = {"saw", "left", "found", "ground", "wound", "lay", "rose", "felt", "bit",
                       "fell", "bore", "bound", "lit", "stuck", "bent", "better", "best",
                       "worse", "worst", "lives", "men"}
_FORM_OF_RE = re.compile(r"\b(past|participle|plural|comparative|superlative|tense|inflection)\b.{0,60}\bof\b",
                         re.I)


def _looks_like_form_of(info: dict) -> bool:
    """Перший запис слова — це лише «форма іншого слова» (past tense of fight тощо)."""
    for m in info.get("meanings", [])[:2]:
        for d in m.get("defs", [])[:1]:
            if _FORM_OF_RE.search(d.get("d", "")):
                return True
    return False


def english_dictionary(word: str) -> dict:
    """Словникова стаття для слова. Для неправильних форм («fought», «went», «children»)
    бере статтю початкового слова («fight», «go», «child»): сама форма в словниках — лише
    відсилка «past tense of …», без справжнього пояснення."""
    low = word.lower()
    base = IRREGULAR_FORMS.get(low)
    if base and base != low:
        direct = None
        if low in IRREGULAR_AMBIGUOUS:
            try:
                direct = _english_lookup(word)
            except Exception:
                direct = None
            if direct and direct.get("meanings") and not _looks_like_form_of(direct):
                return direct
        try:
            info = _english_lookup(base)
        except Exception:
            info = None
        if info and info.get("meanings"):
            log(f"Словник: «{word}» — неправильна форма слова «{base}»")
            return {**info, "base": base}
        if direct and direct.get("meanings"):
            return direct
    return _english_lookup(word)


def _english_lookup(word: str) -> dict:
    """dictionaryapi.dev, а якщо він недоступний або нічого не знайшов — запасне джерело
    Datamuse. Якщо і те, і те не знайшли ЖОДНОГО реального слова — востаннє пробує
    виправити ймовірну помилку OCR (нечітким пошуком) і повторює пошук для нього."""
    empty = {"base": "", "phonetic": "", "audios": [], "meanings": []}
    err = None
    try:
        primary = _freedictionary_lookup(word)
    except Exception as e:
        primary, err = empty, str(e)
    if primary["meanings"]:
        return primary
    try:
        fallback = datamuse_dictionary(word)
    except Exception:
        fallback = empty
    if fallback["meanings"]:
        log(f"Словник: основне джерело без результату, використано запасне (Datamuse)")
        return fallback
    corrected = _datamuse_spellcheck(word)
    if corrected:
        try:
            retry = datamuse_dictionary(corrected)
        except Exception:
            retry = empty
        if retry["meanings"]:
            log(f"Словник: «{word}» не знайдено, схоже це помилка OCR — використано «{corrected}»")
            retry["corrected_from"] = word
            return retry
    if err:
        raise RuntimeError(err + " Резервне джерело (Datamuse) теж не знайшло визначень.")
    return empty


class DeeplError(Exception):
    pass


def _deepl_active() -> bool:
    d = cfg.get("deepl", {})
    return bool(d.get("enabled") and str(d.get("key", "")).strip())


def _deepl_base() -> str:
    # безкоштовні ключі закінчуються на ":fx" і працюють з іншим сервером
    return ("https://api-free.deepl.com" if cfg["deepl"]["key"].strip().endswith(":fx")
            else "https://api.deepl.com")


def _deepl_headers() -> dict:
    return {"Authorization": f"DeepL-Auth-Key {cfg['deepl']['key'].strip()}"}


def deepl_translate(text: str, sl: str = "auto", context: str = "") -> str:
    """Переклад через офіційний API DeepL. context — сусідній текст, який DeepL враховує
    для вибору значення слова, але сам не перекладає (напр. рядок навколо «chest»)."""
    data = {"text": text, "target_lang": cfg["languages"]["target"].upper()}
    if sl and sl != "auto":
        data["source_lang"] = sl.upper()
    if context:
        data["context"] = context[:500]
    r = SESSION.post(_deepl_base() + "/v2/translate", data=data,
                     headers=_deepl_headers(), timeout=6)
    if r.status_code in (429, 456):  # забагато запитів / вичерпано місячний ліміт
        raise RateLimited(f"DeepL HTTP {r.status_code}")
    if r.status_code in (401, 403):
        raise DeeplError("ключ DeepL недійсний")
    r.raise_for_status()
    out = " ".join(t.get("text", "") for t in (r.json().get("translations") or [])).strip()
    if not out:
        raise DeeplError("порожня відповідь DeepL")
    return out


def deepl_usage():
    """(використано, ліміт) символів за поточний місяць — для перевірки ключа."""
    if not cfg["deepl"]["key"].strip():
        raise DeeplError("ключ не вказано")
    r = SESSION.get(_deepl_base() + "/v2/usage", headers=_deepl_headers(), timeout=6)
    if r.status_code in (401, 403):
        raise DeeplError("ключ недійсний")
    r.raise_for_status()
    j = r.json()
    return int(j.get("character_count", 0)), int(j.get("character_limit", 0))


def _try_deepl(text: str, sl: str = "auto", context: str = ""):
    """Переклад DeepL або None (не налаштовано / збій / ліміт) — тоді працює звичайний
    ланцюжок Google → MyMemory. Після збою DeepL кілька секунд не смикаємо."""
    if not _deepl_active():
        return None
    until, _ = _cooldown.get("DeepL", (0, False))
    if time.time() < until:
        return None
    try:
        return deepl_translate(text, sl, context)
    except RateLimited:
        _cooldown["DeepL"] = (time.time() + 300, True)
        log("DeepL: ліміт запитів або символів, пауза 5 хв")
    except Exception as e:
        _cooldown["DeepL"] = (time.time() + 30, False)
        log(f"DeepL: {type(e).__name__}: {e}")
    return None


def translate_text(text: str) -> str:
    dl = _try_deepl(text, "auto", "")
    if dl:
        return dl
    main, _groups, _name = translate_with_fallback(text, "auto", False)
    return main


_HUD_GLUE_RE = re.compile(r"([A-Za-zА-Яа-яІіЇїЄєҐґ])([:\d])|([:\d])([A-Za-zА-Яа-яІіЇїЄєҐґ])")


def clean_hud_line(text: str) -> str:
    """У «рядок» для перекладу часто потрапляють елементи інтерфейсу гри поруч зі
    звичайним текстом — лічильники, мітки команд тощо, зліплені без пробілу
    (напр. «COMMANDER:0»). Перекладач бачить таке як одне нечитабельне «слово» і
    просто лишає його без перекладу. Ця функція розділяє літери й цифри/двокрапки
    пробілом, щоб реальне слово («COMMANDER») переклалося окремо від цифр."""
    def sep(m):
        a, b, c, d = m.groups()
        if a is not None:
            return a if b == ":" else f"{a} {b}"
        return d if c == ":" else f"{c} {d}"
    prev = None
    while text != prev:  # можуть бути кілька зліплених місць підряд (літера-цифра-літера)
        prev = text
        text = _HUD_GLUE_RE.sub(sep, text)
    return text


def text_card(original: str, translation: str) -> dict:
    return {"kind": "text", "original": original, "word": original,
            "translation": translation, "entry_kind": "phrase", "source": _game_source()}


_card_cache: dict = {}
_cache_lock = threading.RLock()
_cache_new = 0


def _cache_path() -> str:
    return os.path.join(os.path.dirname(config_path()), "cache.json")


def load_card_cache() -> None:
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            limit = int(clamp(cfg["cache"]["max_entries"], 0, 5000))
            valid = [(k, v) for k, v in data.items()
                     if isinstance(v, dict) and v.get("kind") == "word"]
            with _cache_lock:
                _card_cache.clear()
                if limit:
                    _card_cache.update(valid[-limit:])
    except (OSError, ValueError):
        pass


def cache_disk_kb() -> float:
    try:
        return os.path.getsize(_cache_path()) / 1024
    except OSError:
        return 0.0


def save_card_cache() -> None:
    limit = int(clamp(cfg["cache"]["max_entries"], 0, 5000))
    with _cache_lock:
        try:
            if limit == 0:
                data = {}
            else:
                data = dict(list(_card_cache.items())[-limit:])
            atomic_json_write(_cache_path(), data, indent=None)
        except OSError:
            pass


def clear_card_cache() -> None:
    with _cache_lock:
        _card_cache.clear()
        try:
            os.remove(_cache_path())
        except OSError:
            pass


def cached_word_card(key):
    """Картка кожного пошуку незалежна: контекст не протікає в наступний запит."""
    if int(clamp(cfg["cache"]["max_entries"], 0, 5000)) == 0:
        return None
    with _cache_lock:
        card = _card_cache.pop(key, None)
        if card is not None:
            _card_cache[key] = card
            return copy.deepcopy(card)
    return None


# ------------------------------ Збережені слова -----------------------------
def _saved_words_path() -> str:
    return os.path.join(os.path.dirname(config_path()), "saved_words.json")


_saved_words: list = None  # завантажується лише раз, далі тримається в пам'яті


def load_saved_words() -> list:
    """Список збережених слів. Читається з диска лише один раз за сеанс."""
    global _saved_words
    if _saved_words is not None:
        return _saved_words
    try:
        with open(_saved_words_path(), encoding="utf-8") as f:
            data = json.load(f)
        _saved_words = [e for e in data if isinstance(e, dict)
                        and isinstance(e.get("word"), str) and e["word"].strip()] \
            if isinstance(data, list) else []
    except (OSError, ValueError):
        _saved_words = []
    return _saved_words


def _write_saved_words() -> None:
    """Безпечний запис: спершу у тимчасовий файл, потім атомарна заміна —
    щоб раптове закриття програми не пошкодило список."""
    try:
        path = _saved_words_path()
        atomic_json_write(path, _saved_words or [])
        return True
    except OSError as e:
        log(f"Не вдалося зберегти список слів: {e}")
        return False


def saved_entry(word: str):
    for e in load_saved_words():
        if e.get("word", "").lower() == word.lower():
            return e
    return None


def _entry_complete(e: dict) -> bool:
    return bool(e.get("translation") or e.get("definition"))


def is_word_saved(word: str) -> bool:
    return any(e.get("word", "").lower() == word.lower() for e in load_saved_words())


def _card_example(card: dict) -> str:
    """Перший приклад речення з уже наявних визначень картки, якщо є."""
    for m in card.get("meanings", []):
        for d in m.get("defs", []):
            if d.get("e"):
                return d["e"]
    return ""


def _card_definition(card: dict) -> str:
    """Перше визначення (англійською) з уже наявного словника картки, якщо є."""
    for m in card.get("meanings", []):
        for d in m.get("defs", []):
            if d.get("d"):
                pos = m.get("pos", "")
                return f"({pos}) {d['d']}" if pos else d["d"]
    return ""


def _clean_sentence(text: str, limit: int = 300) -> str:
    return " ".join((text or "").split())[:limit]


def _anki_tag(text: str) -> str:
    """Тег Anki не може містити пробілів: «Persona 5 Royal» -> «Persona_5_Royal»."""
    return re.sub(r"[^\w\-]+", "_", (text or "").strip(), flags=re.U).strip("_")[:40]


def _common_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def align_word_span(translation: str, terms):
    """Де в перекладеному реченні стоїть переклад слова — його підсвічуємо (як слово в
    Translator). Порівняння за спільним початком слова, щоб ловити відмінки/дієвідміни
    («опора» ~ «опору»). Терміни йдуть за пріоритетом: головний переклад першим.
    Повертає (початок, кінець) або None."""
    words = [(m.start(), m.end(), m.group().lower())
             for m in re.finditer(r"\w+", translation or "", flags=re.U)]
    for term in terms:
        best = None
        for t in re.findall(r"\w+", (term or "").lower(), flags=re.U):
            if len(t) < 3:
                continue
            for st, en, w in words:
                cp = _common_prefix(t, w)
                shortest = min(len(t), len(w))
                if (cp == len(t) == len(w)) or (cp >= 4 and cp >= 0.55 * shortest):
                    if best is None or cp > best[0]:
                        best = (cp, st, en)
        if best:
            return best[1], best[2]
    return None


def split_around_word(sentence: str, word: str) -> list:
    """[(текст, чи_виділяти), ...] — речення, розрізане навколо слова."""
    m = re.search(rf"\b{re.escape(word)}\b", sentence or "", flags=re.I)
    if not m:
        return [(sentence, False)] if sentence else []
    parts = [(sentence[:m.start()], False), (m.group(), True), (sentence[m.end():], False)]
    return [(t, h) for t, h in parts if t]


def _first_definition_text(card: dict) -> str:
    for m in card.get("meanings", []):
        for d in m.get("defs", []):
            if d.get("d"):
                return d["d"]
    return ""


def _card_primary_translation(card: dict) -> str:
    if card.get("translation"):
        return card["translation"]
    groups = card.get("groups")
    return groups[0][1][0] if groups else ""


def primary_translations(card: dict, limit: int = 4) -> list:
    """Кілька варіантів перекладу для головного рядка картки — не один здогад Google,
    а перші кілька термінів зі словникової групи (зазвичай повніші й точніші, напр.
    містять і «сундук», і «груди» для "chest"), плюс сам переклад Google, якщо він
    ще не входить у цей список. Без дублікатів, порядок зберігається."""
    seen, out = set(), []
    groups = card.get("groups") or []
    for term in (groups[0][1] if groups else []):
        low = term.lower()
        if low not in seen:
            seen.add(low)
            out.append(term)
    translation = card.get("translation") or ""
    if translation:  # контекстний переклад — завжди першим, без дубліката у списку
        out = [t for t in out if t.lower() != translation.lower()]
        out.insert(0, translation)
    return out[:max(1, limit)]


def add_saved_word(card: dict) -> bool:
    """Додає слово зі словникової картки у список. Повертає False, якщо слово
    вже було збережене раніше (без дублікатів)."""
    words = load_saved_words()
    existing = saved_entry(card["word"])
    if existing is not None:
        previous = copy.deepcopy(existing)
        # Слово могли зберегти ще під час довантаження визначень/речення.
        updates = {"translation": _card_primary_translation(card), "definition": _card_definition(card),
                   "base": card.get("base"), "example": _card_example(card),
                   "context": _clean_sentence(card.get("context", "")), "source": card.get("source"),
                   "definition_tr": card.get("definition_tr"), "context_tr": card.get("sentence_tr")}
        changed = False
        for key, value in updates.items():
            if value and not existing.get(key) and key not in existing.get("edited_fields", []):
                existing[key] = value
                changed = True
        details = existing.setdefault("dictionary", {})
        for key in ("phonetic", "audios", "meanings", "groups"):
            if card.get(key) and not details.get(key):
                details[key] = copy.deepcopy(card[key])
                changed = True
        if changed:
            if not _write_saved_words():
                existing.clear()
                existing.update(previous)
                return False
        return changed
    words.append({
        "word": card["word"],
        "kind": card.get("entry_kind", "word"),
        "translation": _card_primary_translation(card),
        "base": card.get("base") or "",
        "example": _card_example(card),
        "definition": _card_definition(card),
        "context": _clean_sentence(card.get("context", "")),
        "source": card.get("source", ""),
        "definition_tr": card.get("definition_tr", ""),
        "context_tr": card.get("sentence_tr", ""),
        "image_file": save_context_image(card.get("_context_image")),
        "dictionary": {k: copy.deepcopy(card[k]) for k in ("phonetic", "audios", "meanings", "groups")
                       if card.get(k)},
        "added": datetime.now().isoformat(timespec="seconds"),
    })
    if not _write_saved_words():
        removed = words.pop()
        path = context_image_path(removed.get("image_file", ""))
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        return False
    return True


def remove_saved_word(word: str) -> bool:
    words = load_saved_words()
    previous = list(words)
    removed = [e for e in words if e.get("word", "").lower() == word.lower()]
    before = len(words)
    words[:] = [e for e in words if e.get("word", "").lower() != word.lower()]
    if len(words) != before:
        if not _write_saved_words():
            words[:] = previous
            return False
        for e in removed:
            name = e.get("image_file", "")
            path = context_image_path(name)
            if path and not any(w.get("image_file") == name for w in words):
                try:
                    os.remove(path)
                except OSError:
                    pass
        return True
    return False


def filter_saved_words(entries, query="", order="newest"):
    """Пошук у слові, перекладі, прикладі, визначенні та джерелі без мережі."""
    terms = query.casefold().split()
    fields = ("word", "translation", "definition", "context", "example", "source", "notes")
    result = [e for e in entries if all(
        term in " ".join(str(e.get(k, "")) for k in fields).casefold() for term in terms)]
    if order == "az":
        return sorted(result, key=lambda e: e["word"].casefold())
    return sorted(result, key=lambda e: str(e.get("added", "")), reverse=order != "oldest")


def saved_word_card(entry):
    """Збережені дані доступні відразу, навіть без інтернету чи після очищення кешу."""
    definition, example = entry.get("definition", ""), entry.get("example", "")
    if entry.get("kind") == "phrase":
        return {**text_card(entry["word"], entry.get("translation", "")),
                "source": entry.get("source", ""), "image_file": entry.get("image_file", "")}
    card = {"kind": "word", "word": entry["word"], "base": entry.get("base", ""),
            "translation": entry.get("translation", ""), "groups": [],
            "phonetic": "", "audios": [], "note": "", "dict_note": "",
            "meanings": [{"pos": "", "defs": [{"d": definition, "e": example}],
                          "syn": [], "ant": []}] if definition or example else [],
            "context": entry.get("context", ""), "sentence": entry.get("context", ""),
            "sentence_tr": entry.get("context_tr", ""),
            "definition_tr": entry.get("definition_tr", ""),
            "source": entry.get("source", ""), "image_file": entry.get("image_file", "")}
    details = entry.get("dictionary", {})
    if isinstance(details, dict):
        for key in ("phonetic", "audios", "meanings", "groups"):
            if details.get(key):
                card[key] = copy.deepcopy(details[key])
    if "definition" in entry.get("edited_fields", []) or "example" in entry.get("edited_fields", []):
        if not card["meanings"]:
            card["meanings"] = [{"pos": "", "defs": [], "syn": [], "ant": []}]
        first = card["meanings"][0]
        if not first["defs"]:
            first["defs"] = [{"d": "", "e": ""}]
        first["defs"][0].update(d=definition, e=example)
    return card


STATUS_LABELS = {"Усі": "all", "Нові": "new", "Вивчаю": "learning", "Знаю": "known"}
EDIT_FIELDS = ("translation", "definition", "definition_tr", "example", "context", "context_tr", "source", "notes")


def update_saved_entry(word, fields):
    entry = saved_entry(word)
    if entry is None:
        raise ValueError("Слово вже видалене з бібліотеки")
    old = copy.deepcopy(entry)
    edited = set(entry.get("edited_fields", []))
    for key, value in fields.items():
        if key in EDIT_FIELDS and isinstance(value, str):
            if value.strip() != entry.get(key, ""):
                edited.add(key)
            entry[key] = value.strip()
        elif key == "status" and value in ("new", "learning", "known"):
            entry[key] = value
        elif key == "image_file" and (not value or context_image_path(value)):
            entry[key] = value
    entry["edited_fields"] = sorted(edited)
    if not _write_saved_words():
        entry.clear()
        entry.update(old)
        raise OSError("Не вдалося записати зміни. Попередні дані збережено в пам'яті.")
    return entry


def review_due(entry, now=None):
    if entry.get("status") == "known":
        return False
    now = now or datetime.now(timezone.utc)
    due = entry.get("review", {}).get("due")
    if not due:
        return True
    try:
        dt = datetime.fromisoformat(due)
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc) <= now
    except (ValueError, TypeError):
        return True


def record_review(entry, known, now=None):
    """Простий розклад 1, 3, 7, 14, 30 днів; помилка — повторити через 10 хв."""
    now = now or datetime.now(timezone.utc)
    old = copy.deepcopy(entry)
    stats = entry.setdefault("review", {})
    streak = int(clamp(stats.get("streak", 0), 0, 1000)) + 1 if known else 0
    interval = (1, 3, 7, 14, 30)[min(streak - 1, 4)] if known else 0
    stats.update(streak=streak, attempts=int(clamp(stats.get("attempts", 0), 0, 1000000)) + 1,
                 last=now.isoformat(), due=(now + (timedelta(days=interval) if known else
                                                 timedelta(minutes=10))).isoformat())
    entry["status"] = "learning"
    if not _write_saved_words():
        entry.clear()
        entry.update(old)
        raise OSError("Прогрес не записано на диск")
    return stats


def context_image_path(name):
    if not isinstance(name, str) or not re.fullmatch(r"context-[a-f0-9]{24}\.jpg", name):
        return None
    return os.path.join(os.path.dirname(config_path()), "media", name)


def save_context_image(image):
    if image is None:
        return ""
    name = "context-" + hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:24] + ".jpg"
    path = context_image_path(name)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        image = image.copy().convert("RGB")
        image.thumbnail((960, 480))
        image.save(path, "JPEG", quality=82)
        return name
    except (OSError, ValueError) as e:
        log(f"Не вдалося зберегти зображення контексту: {e}")
        return ""


def context_photo(name, master, size=(560, 260)):
    path = context_image_path(name)
    if not path or not os.path.isfile(path):
        return None
    try:
        from PIL import Image, ImageTk
        with Image.open(path) as image:
            image.thumbnail(size)
            return ImageTk.PhotoImage(image.copy(), master=master)
    except (OSError, ValueError):
        return None


def _anki_field(text: str) -> str:
    """Anki-текстовий імпорт розбиває поля по табуляції й рядках — прибираємо їх."""
    return " ".join((text or "").split())


def _anki_back(e: dict) -> str:
    """Зворотна сторона для .txt (Basic: 2 поля): переклад — визначення — приклад.
    Якщо геть нічого немає — зрозуміла підказка замість порожньої картки."""
    parts = []
    translation = _anki_field(e.get("translation", ""))
    if translation:
        parts.append(translation)
    definition = _anki_field(e.get("definition", ""))
    if definition:
        parts.append(definition)
    definition_tr = _anki_field(e.get("definition_tr", ""))
    if definition_tr:
        parts.append(definition_tr)
    example = _anki_field(e.get("example", ""))
    if example:
        parts.append(f"«{example}»")
    context = _anki_field(e.get("context", ""))
    if context:
        parts.append(f"з гри: «{context}»")
    if not parts:
        return "(переклад відсутній — видали це слово зі списку й збережи ще раз)"
    return " — ".join(parts)


def export_anki_txt(entries: list, path: str) -> None:
    """Простий текстовий файл для Anki (File -> Import): рядок = слово<TAB>зворотна
    сторона. Службових рядків-директив свідомо мінімум (лише роздільник): зайві
    (#columns тощо) різні версії Anki трактують по-різному."""
    lines = ["#separator:tab", "#html:false"]
    for e in entries:
        front = _anki_field(e.get("word", ""))
        if not front:
            continue
        lines.append(f"{front}\t{_anki_back(e)}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ------------------ дозаповнення старих збережених слів ------------------
def _needs_enrich(e: dict) -> bool:
    if not e.get("word"):
        return False
    edited = e.get("edited_fields", [])
    return ((not e.get("translation") and "translation" not in edited)
            or (e.get("kind") != "phrase" and not e.get("definition")
                and not e.get("enriched") and "definition" not in edited))


def enrich_entry(e: dict) -> bool:
    """Дотягує те, чого бракує у збереженому слові: переклад, визначення, приклад.
    Ходить у мережу — викликати НЕ з потоку інтерфейсу. True, якщо щось змінилось."""
    changed = False
    word = e.get("word", "")
    if not e.get("translation") and "translation" not in e.get("edited_fields", []):
        main, groups, _note, _lim = _translate_word(word)
        tr = main or (groups[0][1][0] if groups else "")
        if tr:
            e["translation"] = tr
            changed = True
    if (e.get("kind") != "phrase" and not e.get("definition") and not e.get("enriched")
            and "definition" not in e.get("edited_fields", [])):
        try:
            info = english_dictionary(word)
        except Exception as ex:  # мережа/словник недоступні — не позначаємо, спробуємо наступного разу
            log(f"Дозаповнення «{word}»: {ex}")
            return changed
        pseudo = {"meanings": info.get("meanings", [])}
        d, ex_sentence = _card_definition(pseudo), _card_example(pseudo)
        if d:
            e["definition"] = d
        if ex_sentence and not e.get("example") and "example" not in e.get("edited_fields", []):
            e["example"] = ex_sentence
        if info.get("base") and not e.get("base"):
            e["base"] = info["base"]
        e["enriched"] = True
        changed = True
    return changed


def enrich_entries(entries: list, timeout: float = 40.0) -> int:
    """Паралельно (4 потоки) дозаповнює всі неповні слова зі списку й зберігає файл.
    Загальний ліміт часу — щоб експорт не чекав вічно. Повертає, скільком слів додано дані."""
    todo = [e for e in entries if _needs_enrich(e)]
    if not todo:
        return 0
    changed = 0
    pool = ThreadPoolExecutor(max_workers=4)
    futs = [pool.submit(enrich_entry, e) for e in todo]
    try:
        for f in as_completed(futs, timeout=timeout):
            try:
                if f.result():
                    changed += 1
            except Exception as ex:
                log(f"Дозаповнення слова: {type(ex).__name__}: {ex}")
    except Exception as ex:  # вичерпано загальний час
        log(f"Дозаповнення: не всі слова встигли ({type(ex).__name__})")
    finally:
        pool.shutdown(wait=False)
    if changed:
        _write_saved_words()
    return changed


def apkg_available() -> bool:
    return importlib.util.find_spec("genanki") is not None


def _emphasize(text: str, words, tag: str) -> str:
    """Екранує HTML і виділяє в тексті саме це слово (та його початкову форму)."""
    out = html.escape(text or "", quote=False)
    for w in sorted({w.lower() for w in words if w and len(w) >= 3}, key=len, reverse=True):
        out = re.sub(rf"\b({re.escape(html.escape(w, quote=False))})\b",
                     rf"<{tag}>\1</{tag}>", out, flags=re.I)
    return out


def _anki_html_fields(e: dict) -> list:
    """Поля картки .apkg: [Word, Translation, Meaning, Example]. У Meaning слово
    курсивом, у Example — жирним (як у звичайних колодах для вивчення мови)."""
    word = _anki_field(e.get("word", ""))
    words = [word, e.get("base", "")]
    tr = _anki_field(e.get("translation", ""))
    meaning = _anki_field(e.get("definition", ""))
    example = _anki_field(e.get("example", ""))
    context = _anki_field(e.get("context", ""))
    if not (tr or meaning or example or context):
        tr = "(переклад відсутній — видали це слово зі списку й збережи ще раз)"
    meaning_html = _emphasize(meaning, words, "i")
    definition_tr = _anki_field(e.get("definition_tr", ""))
    if definition_tr:
        meaning_html += "<br>" + html.escape(definition_tr, quote=False)
    context_html = _emphasize(context, words, "b")
    context_tr = _anki_field(e.get("context_tr", ""))
    if context_tr:
        context_html += "<br><i>" + html.escape(context_tr, quote=False) + "</i>"
    if e.get("notes"):
        context_html += "<br><i>" + html.escape(_anki_field(e["notes"]), quote=False) + "</i>"
    image_path = context_image_path(e.get("image_file", ""))
    if image_path and os.path.isfile(image_path):
        context_html += '<br><img src="' + html.escape(e["image_file"], quote=True) + '">'
    return [html.escape(word, quote=False), html.escape(tr, quote=False),
            meaning_html, _emphasize(example, words, "b"), context_html]


def _entry_tag(e: dict) -> str:
    """Тег гри для картки: назва з налаштувань (для всіх), інакше — з заголовка вікна гри."""
    a = cfg.get("anki", {})
    manual = _anki_tag(a.get("game", ""))
    if manual:
        return manual
    return _anki_tag(e.get("source", "")) if a.get("auto_game_tag", True) else ""


_APKG_CSS = (".card{font-family:arial;font-size:20px;text-align:left;color:#222;background:#fff}"
             ".nightMode .card{color:#eee;background:#2c2c2c}"
             ".word{font-size:30px;text-align:center}"
             ".tr{font-size:26px;font-weight:bold;text-align:center;margin:8px 0}"
             ".lbl{font-size:14px;opacity:.7;margin-top:14px}"
             ".box{background:rgba(127,127,127,.15);border-radius:8px;padding:10px 12px;margin-top:4px}")


class AnkiConnectError(RuntimeError):
    pass


def _anki_identity(word):
    return "wordlookup::" + hashlib.sha256(word.casefold().encode("utf-8")).hexdigest()[:24]


def anki_call(action, **params):
    """Лише локальна Anki; ключі й доступ до зовнішніх серверів не потрібні."""
    try:
        response = requests.post("http://127.0.0.1:8765", json={
            "action": action, "version": 6, "params": params}, timeout=8)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as e:
        raise AnkiConnectError("Anki недоступна. Запусти Anki з доповненням AnkiConnect "
                               "на цьому ж комп'ютері (порт 8765).") from e
    if not isinstance(data, dict) or "result" not in data or "error" not in data:
        raise AnkiConnectError("AnkiConnect повернув некоректну відповідь")
    if data["error"]:
        raise AnkiConnectError(str(data["error"]))
    return data["result"]


def sync_anki_entries(entries, deck_name="Word Lookup"):
    """Додає/оновлює тільки обрані записи; не видаляє нотатки й не запускає хмарний sync."""
    deck_name = deck_name.strip()
    if not deck_name:
        raise ValueError("Укажи назву колоди Anki")
    version = anki_call("version")
    if not isinstance(version, int) or version < 6:
        raise AnkiConnectError("Онови AnkiConnect до версії з підтримкою API 6")
    model = "Word Lookup Card"
    names = ["Word", "Translation", "Meaning", "Example", "Context"]
    if model not in anki_call("modelNames"):
        anki_call("createModel", modelName=model, inOrderFields=names, css=_APKG_CSS,
                  cardTemplates=[{"Name": "Card 1", "Front": '<div class="word">{{Word}}</div>',
                                  "Back": '{{FrontSide}}<hr id=answer><div class="tr">{{Translation}}</div>'
                                          '{{#Meaning}}<div class="box">{{Meaning}}</div>{{/Meaning}}'
                                          '{{#Example}}<div class="box">{{Example}}</div>{{/Example}}'
                                          '{{#Context}}<div class="box">{{Context}}</div>{{/Context}}'}])
    if set(anki_call("modelFieldNames", modelName=model)) != set(names):
        raise AnkiConnectError("Модель Word Lookup Card має інші поля; її не змінено")
    anki_call("createDeck", deck=deck_name)
    result = {"added": 0, "updated": 0, "failed": 0, "errors": []}
    for e in entries:
        word = _anki_field(e.get("word", ""))
        if not word:
            continue
        try:
            tag = _anki_identity(word)
            quote = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
            prefix = 'deck:"' + quote(deck_name) + '" '
            ids = anki_call("findNotes", query=prefix + "tag:" + tag)
            if not ids:
                ids = anki_call("findNotes", query=prefix + '"Word:' + quote(word) + '"')
            info = anki_call("notesInfo", notes=ids) if ids else []
            matches = [n for n in info if n.get("modelName") == model and
                       html.unescape(re.sub(r"<[^>]+>", "", n.get("fields", {}).get("Word", {}).get("value", "")))
                       .casefold() == word.casefold()]
            if len(matches) > 1:
                raise AnkiConnectError("Знайдено кілька однакових нотаток; вибери їх вручну в Anki")
            fields = dict(zip(names, _anki_html_fields(e)))
            image_path = context_image_path(e.get("image_file", ""))
            if image_path and os.path.isfile(image_path):
                with open(image_path, "rb") as f:
                    media = base64.b64encode(f.read()).decode("ascii")
                stored = anki_call("storeMediaFile", filename=e["image_file"], data=media)
                if stored != e["image_file"]:
                    raise AnkiConnectError("AnkiConnect не підтвердив запис зображення")
            tags = [tag] + ([_entry_tag(e)] if _entry_tag(e) else [])
            if matches:
                nid = matches[0]["noteId"]
                anki_call("updateNoteFields", note={"id": nid, "fields": fields})
                anki_call("addTags", notes=[nid], tags=" ".join(tags))
                result["updated"] += 1
            else:
                nid = anki_call("addNote", note={"deckName": deck_name, "modelName": model,
                                               "fields": fields, "tags": tags,
                                               "options": {"allowDuplicate": False, "duplicateScope": "deck"}})
                if nid is None:
                    raise AnkiConnectError("Anki не додала нотатку")
                result["added"] += 1
        except Exception as e:
            result["failed"] += 1
            result["errors"].append(f"{word}: {e}")
    return result


def export_anki_apkg(entries: list, path: str, deck_name: str = "Word Lookup") -> None:
    """Готова колода Anki (.apkg): подвійний клік — і картки вже в Anki. Картка:
    спереду слово; ззаду переклад, окремі блоки Meaning і Example. Потребує
    бібліотеку genanki (pip install genanki)."""
    try:
        import genanki
    except ImportError:
        raise ImportError("Для .apkg потрібна бібліотека: pip install genanki")
    model = genanki.Model(
        1746930003, "Word Lookup Card",
        fields=[{"name": "Word"}, {"name": "Translation"}, {"name": "Meaning"},
                {"name": "Example"}, {"name": "Context"}],
        templates=[{
            "name": "Card 1",
            "qfmt": '<div class="word">{{Word}}</div>',
            "afmt": ('{{FrontSide}}<hr id=answer><div class="tr">{{Translation}}</div>'
                     '{{#Meaning}}<div class="lbl">Meaning</div><div class="box">{{Meaning}}</div>{{/Meaning}}'
                     '{{#Example}}<div class="lbl">Example</div><div class="box">{{Example}}</div>{{/Example}}'
                     '{{#Context}}<div class="lbl">From the game</div><div class="box">{{Context}}</div>{{/Context}}'),
        }],
        css=_APKG_CSS)
    deck_id = int(hashlib.md5(deck_name.encode("utf-8")).hexdigest()[:8], 16)
    deck = genanki.Deck(deck_id, deck_name)
    added = 0
    for e in entries:
        word = _anki_field(e.get("word", ""))
        if not word:
            continue
        # стабільний guid за словом: повторний імпорт ОНОВЛЮЄ картку, а не дублює
        tag = _entry_tag(e)
        extra = {"tags": [_anki_identity(word)] + ([tag] if tag else [])}
        deck.add_note(genanki.Note(model=model, fields=_anki_html_fields(e),
                                   guid=genanki.guid_for(word.lower()), **extra))
        added += 1
    if added == 0:
        raise ValueError("Немає жодного слова з назвою для експорту")
    media_files = {context_image_path(e.get("image_file", "")) for e in entries}
    media_files = sorted(p for p in media_files if p and os.path.isfile(p))
    genanki.Package(deck, media_files=media_files).write_to_file(path)


def _clean_context(word: str, context: str) -> str:
    ctx = " ".join((context or "").split())[:300]
    return "" if ctx.lower() == word.lower() else ctx


def _cache_key(word: str, context: str = "") -> str:
    key = f"{word.lower()}|{cfg['languages']['target']}|{_max_tr()}"
    if _deepl_active():  # переклад залежить від контексту -> окремі записи кешу
        key += "|dl"
        ctx = _clean_context(word, context)
        if ctx:
            key += "|" + hashlib.md5(ctx.lower().encode("utf-8")).hexdigest()[:8]
    return key


def _cache_word_card(key: str, card: dict) -> None:
    """Кладе повну (без note/dict_note) картку в кеш і час від часу зберігає на диск."""
    global _cache_new
    limit = int(clamp(cfg["cache"]["max_entries"], 0, 5000))
    if limit == 0:
        return  # кешування вимкнено
    with _cache_lock:
        _card_cache.pop(key, None)
        _card_cache[key] = copy.deepcopy(card)
        while len(_card_cache) > limit:
            del _card_cache[next(iter(_card_cache))]
        _cache_new += 1
        if _cache_new % 5 == 0:
            save_card_cache()


def _google_word(word: str):
    try:
        # Google-словник (bd) часто дає МЕНШЕ варіантів для слова з великої літери
        # (напр. "Chest" з інтерфейсу гри) — рядкові й прописні там різні статті
        main, groups, _name = translate_with_fallback(word.lower(), "en", True)
        return main, groups, "", False
    except TranslateError as e:
        return "", [], str(e), e.limited
    except Exception as e:
        return "", [], f"Переклад недоступний ({type(e).__name__})", False


def _translate_word(word: str, context: str = ""):
    """(переклад, групи_за_частинами_мови, повідомлення_про_помилку, чи_це_ліміт).
    З ключем DeepL: головний переклад — від DeepL з урахуванням контексту (рядка навколо
    слова), а варіанти за частинами мови — зі словника Google; обидва запити паралельно."""
    if not _deepl_active():
        return _google_word(word)
    ctx = _clean_context(word, context)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_g = pool.submit(_google_word, word)
        f_d = pool.submit(_try_deepl, word, "en", ctx)
        main, groups, note, limited = f_g.result()
        dl = f_d.result()
    if dl:
        return dl, groups, "", False
    return main, groups, note, limited


def build_word_card(word: str, context: str = "") -> dict:
    """Повна картка за один виклик: чекає і переклад, і словник одночасно."""
    key = _cache_key(word, context)
    cached = cached_word_card(key)
    if cached is not None:
        cached["word"] = word
        return cached
    empty = {"base": "", "phonetic": "", "audios": [], "meanings": []}
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        f_t = pool.submit(_translate_word, word, context)
        f_e = pool.submit(english_dictionary, word)
        main, groups, note, limited = f_t.result(timeout=12)
        try:
            info, dict_note = f_e.result(timeout=9), ""
        except Exception as e:
            info, dict_note = empty, (str(e) if isinstance(e, RuntimeError) else
                                      "Англійський словник не відповів за 9 с — можливо, повільна мережа.")
    finally:
        pool.shutdown(wait=False)
    if info.get("corrected_from"):  # OCR, ймовірно, помилився — перекладаємо вже виправлене слово
        main2, groups2, note2, limited2 = _translate_word(info["base"])
        if main2 or groups2:
            main, groups, note, limited = main2, groups2, note2, limited2
    if not main and not groups and not info["meanings"]:
        raise TranslateError(note or dict_note or "Слово не знайдено в словниках", limited=limited)
    card = {"kind": "word", "word": word, "translation": main, "groups": groups,
            "note": note, "dict_note": dict_note, **info}
    if not note and not dict_note:
        _cache_word_card(key, card)
    return card


def build_word_card_fast(word: str, on_partial=None, context: str = ""):
    """Двофазний пошук: спершу лише переклад (зазвичай < 1 с) — викликає on_partial(картка)
    одразу, ще до звернення по визначення й синоніми. Потім довантажує словник і повертає
    ПОВНУ картку (як build_word_card). Якщо слово вже в кеші — on_partial узагалі не викликається,
    бо готова картка і так з'явиться миттєво."""
    key = _cache_key(word, context)
    cached = cached_word_card(key)
    if cached is not None:
        cached["word"] = word
        return cached, False
    empty = {"base": "", "phonetic": "", "audios": [], "meanings": []}
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        # Обидва джерела стартують разом; показ першого перекладу не чекає словника.
        dictionary = pool.submit(english_dictionary, word)
        main, groups, note, limited = _translate_word(word, context)
        partial = {"kind": "word", "word": word, "translation": main, "groups": groups,
                  "note": note, "dict_note": "", "dict_pending": True, **empty}
        if on_partial:
            on_partial(partial)
        try:
            info, dict_note = dictionary.result(timeout=9), ""
        except Exception as e:
            info, dict_note = empty, (str(e) if isinstance(e, RuntimeError) else
                                      "Англійський словник не відповів — можливо, повільна мережа.")
    finally:
        pool.shutdown(wait=False)
    if info.get("corrected_from"):  # OCR, ймовірно, помилився — перекладаємо вже виправлене слово
        main2, groups2, note2, limited2 = _translate_word(info["base"])
        if main2 or groups2:
            main, groups, note, limited = main2, groups2, note2, limited2
    if not main and not groups and not info["meanings"]:
        raise TranslateError(note or dict_note or "Слово не знайдено в словниках", limited=limited)
    full = {**partial, "translation": main, "groups": groups, "note": note,
            "dict_note": dict_note, "dict_pending": False, **info}
    if not note and not dict_note:
        _cache_word_card(key, full)
    return full, True


SAMPLE_CARD = {
    "kind": "word", "word": "understatement", "base": "understatement",
    "phonetic": "ˌʌndəˈsteɪtmənt", "audios": [("BrE", "preview"), ("AmE", "preview")],
    "translation": "применшення",
    "sentence": "To say I am delighted is an understatement.",
    "sentence_tr": "Сказати, що я в захваті, — це применшення.",
    "definition_tr": "Опис чогось як меншого або менш важливого, ніж воно є насправді.",
    "groups": [("іменник", ["применшення", "стриманий вислів", "недомовка"])],
    "meanings": [{
        "pos": "noun",
        "defs": [
            {"d": "The presentation of something as being smaller or less good "
                  "or important than it really is.",
             "e": "to say I am delighted is an understatement"},
            {"d": "A statement that expresses an idea in restrained terms.", "e": ""},
        ],
        "syn": ["minimization", "trivialization", "euphemism", "understatedness",
                "restraint", "litotes", "underplaying"],
        "ant": ["exaggeration", "overstatement"],
    }],
}


def play_audio(url: str) -> None:
    if not url or url == "preview":
        return

    def run():
        try:
            import pygame
            data = requests.get(url, headers=HEADERS, timeout=8).content
            if not pygame.mixer.get_init():
                pygame.mixer.init()
            pygame.mixer.music.load(io.BytesIO(data))
            pygame.mixer.music.play()
        except Exception as e:
            print(f"Не вдалося відтворити аудіо: {e}")
    threading.Thread(target=run, daemon=True).start()


# ============================== OCR ========================================
W = namedtuple("W", "text x0 y0 x1 y1 line")


class OcrError(Exception):
    pass


def _default_tesseract() -> str:
    if sys.platform.startswith("win"):
        for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                  r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
            if os.path.exists(p):
                return p
    return ""


def _configure_tesseract_cmd():
    """Підключає pytesseract і виставляє шлях до tesseract.exe. Не звертається до
    самого tesseract (без subprocess) — це швидко й безпечно викликати часто."""
    try:
        import pytesseract
    except ImportError:
        return None
    cmd = cfg["ocr"]["tesseract_cmd"] or _default_tesseract()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    return pytesseract


def tesseract_ready() -> bool:
    """Чи можна реально запустити tesseract. Виконує subprocess із жорстким
    тайм-аутом 2 с, щоб зламаний чи заблокований (антивірусом) шлях ніколи не
    підвісив програму — раніше тут не було тайм-ауту взагалі."""
    pytesseract = _configure_tesseract_cmd()
    if pytesseract is None:
        return False
    cmd = pytesseract.pytesseract.tesseract_cmd or "tesseract"
    try:
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform.startswith("win") else {}
        subprocess.run([cmd, "--version"], capture_output=True, timeout=2, **kwargs)
        return True
    except Exception:
        return False


def rapid_ready() -> bool:
    return any(importlib.util.find_spec(m) for m in ("rapidocr_onnxruntime", "rapidocr"))


_engine_cache = {"key": None, "val": None, "t": 0.0}


def pick_engine() -> str:
    """Вибір рушія; результат кешується на хвилину (перевірка Tesseract запускає процес)."""
    key = (cfg["ocr"]["engine"], cfg["ocr"]["tesseract_cmd"])
    if _engine_cache["key"] == key and time.time() - _engine_cache["t"] < 60:
        return _engine_cache["val"]
    val = _pick_engine_uncached()
    _engine_cache.update(key=key, val=val, t=time.time())
    return val


def _pick_engine_uncached() -> str:
    eng = cfg["ocr"]["engine"]
    if eng == "tesseract" and tesseract_ready():
        return "tesseract"
    if eng == "rapidocr" and rapid_ready():
        return "rapidocr"
    if eng == "auto":
        if tesseract_ready():
            return "tesseract"
        if rapid_ready():
            return "rapidocr"
    raise OcrError("Не знайдено OCR-рушій. Встанови: pip install rapidocr-onnxruntime "
                   "(або Tesseract + pytesseract).")


def ocr_tesseract(img, scale: float):
    pytesseract = _configure_tesseract_cmd()
    if pytesseract is None:
        raise OcrError("pytesseract не встановлено")
    data = pytesseract.image_to_data(img.convert("L"), lang="eng", config="--psm 6",
                                     output_type=pytesseract.Output.DICT)
    out = []
    for i, t in enumerate(data["text"]):
        t = (t or "").strip()
        if not t:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = 0
        if conf < 15:
            continue
        x, y, w, h = (data[k][i] for k in ("left", "top", "width", "height"))
        line = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        out.append(W(t, x / scale, y / scale, (x + w) / scale, (y + h) / scale, line))
    return out


_NARROW = set("iljtfIr.,;:'\"!|()[]1 ")
_WIDE = set("mwMW@")


def _char_w(ch: str) -> float:
    if ch in _NARROW:
        return 0.45
    if ch in _WIDE:
        return 1.5
    return 1.15 if ch.isupper() else 1.0


def split_line_words(text, x0, y0, x1, y1, line_id):
    """Ділить рядок OCR на слова пропорційно ширині символів."""
    tokens = list(re.finditer(r"\S+", text))
    if not tokens:
        return []
    cum = [0.0]
    for ch in text:
        cum.append(cum[-1] + _char_w(ch))
    k = (x1 - x0) / (cum[-1] or 1)
    return [W(m.group(), x0 + cum[m.start()] * k, y0, x0 + cum[m.end()] * k, y1, line_id)
            for m in tokens]


_rapid_run = None
_rapid_lock = threading.Lock()


def _speed_up_rapid(eng) -> None:
    """Детекція без багаторазового збільшення знімка — набагато швидше (best-effort)."""
    try:
        pre = eng.text_det.preprocess_op
        pre.limit_type, pre.limit_side_len = "max", 960
        log("RapidOCR: швидка детекція увімкнена")
    except Exception:
        log("RapidOCR: швидку детекцію ввімкнути не вдалося (працюватиме повільніше)")


def _rapid_engine():
    global _rapid_run
    with _rapid_lock:
        if _rapid_run is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
                eng = RapidOCR()
                _speed_up_rapid(eng)

                def run(arr):
                    try:  # трохи нижчі пороги — краще ловить ігровий текст
                        res, _ = eng(arr, use_cls=False, text_score=0.3, box_thresh=0.3)
                    except TypeError:
                        res, _ = eng(arr)
                    return [(b, t, s) for b, t, s in (res or [])]
            except ImportError:
                from rapidocr import RapidOCR
                eng = RapidOCR()
                _speed_up_rapid(eng)

                def run(arr):
                    try:
                        out = eng(arr, use_cls=False)
                    except TypeError:
                        out = eng(arr)
                    if out is None or getattr(out, "boxes", None) is None:
                        return []
                    return list(zip(out.boxes, out.txts, out.scores))
            _rapid_run = run
        return _rapid_run


def _gap_segments(img, n, x0, y0, x1, y1, min_gap_frac=0.2):
    """Межі n слів усередині рядка за РЕАЛЬНИМИ проміжками на зображенні (стовпці без
    «чорнила»), а не за оцінкою ширини літер — та накопичує похибку вздовж довгого
    рядка. Повертає [(x_початок, x_кінець), ...] у координатах img або None, якщо
    надійно визначити не вдалось (складний фон, замало проміжків) — тоді
    викликач лишає стару пропорційну оцінку."""
    if n < 2:
        return None
    try:
        import numpy as np
    except ImportError:
        return None
    X0, Y0 = max(int(x0), 0), max(int(y0), 0)
    X1, Y1 = min(int(x1) + 1, img.width), min(int(y1) + 1, img.height)
    if X1 - X0 < 2 * n or Y1 - Y0 < 4:
        return None
    arr = np.asarray(img.crop((X0, Y0, X1, Y1)).convert("L"), dtype=np.float32)
    # «чорнило» стовпця = розкид яскравості (макс − мін): не залежить від кольору фону і
    # не ламається, коли жирний текст займає понад половину рамки (медіана тоді
    # стала б кольором самого тексту й усе інвертувалось)
    ink = arr.max(axis=0) - arr.min(axis=0)
    if ink.max() < 8:  # майже немає контрасту — не довіряємо
        return None
    is_bg = ink <= max(ink.max() * 0.12, 2.0)
    cols = np.flatnonzero(~is_bg)
    if len(cols) == 0:
        return None
    first, last = int(cols[0]), int(cols[-1])
    min_gap = max(2, int((Y1 - Y0) * min_gap_frac))
    runs, i = [], first
    while i <= last:
        if is_bg[i]:
            j = i
            while j <= last and is_bg[j]:
                j += 1
            if j - i >= min_gap:
                runs.append((i, j))
            i = j
        else:
            i += 1
    if len(runs) < n - 1:
        return None
    widest = sorted(sorted(runs, key=lambda r: r[1] - r[0], reverse=True)[:n - 1])
    bounds, start = [], first
    for a, b in widest:
        bounds.append((start, a))
        start = b
    bounds.append((start, last + 1))
    return [(X0 + a, X0 + b) for a, b in bounds]


def ocr_rapid(img, scale: float):
    import numpy as np
    arr = np.ascontiguousarray(np.array(img.convert("RGB"))[:, :, ::-1])  # RGB -> BGR
    out = []
    for i, (box, text, _score) in enumerate(_rapid_engine()(arr)):
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        tokens = list(re.finditer(r"\S+", text))
        segs = _gap_segments(img, len(tokens), min(xs), min(ys), max(xs), max(ys))
        if segs is not None:
            out += [W(m.group(), a / scale, min(ys) / scale, b / scale, max(ys) / scale, i)
                    for m, (a, b) in zip(tokens, segs)]
        else:  # запасний варіант: пропорційна оцінка за шириною літер
            out += split_line_words(text, min(xs) / scale, min(ys) / scale,
                                    max(xs) / scale, max(ys) / scale, i)
    return out


def pick_word(words, cx, cy, tol):
    """Слово, що містить точку (cx, cy) або найближче до неї в межах tol."""
    best, best_d = None, 1e9
    for wd in words:
        dx = max(wd.x0 - cx, 0, cx - wd.x1)
        dy = max(wd.y0 - cy, 0, cy - wd.y1)
        d = math.hypot(dx, dy)
        if d < best_d:
            best, best_d = wd, d
    return best if best_d <= tol else None


def clean_word(raw: str):
    """Повертає (слово, частка_початку, частка_кінця) без пунктуації навколо."""
    t = raw.replace("’", "'")
    lead = len(t) - len(t.lstrip(STRIP_CHARS))
    trail = len(t) - len(t.rstrip(STRIP_CHARS))
    core = t.strip(STRIP_CHARS)
    if not WORD_RE.fullmatch(core):
        m = re.search(r"[A-Za-z][A-Za-z'-]*", core)
        core = m.group(0).strip("'-") if m else ""
    n = max(len(t), 1)
    return core, lead / n, trail / n


LAST_OCR = {"count": 0, "time": 0.0}


# ------------------------------ журнал ------------------------------
_LOG_MAX = 300_000  # байт; при перевищенні файл перейменовується в app.log.1 (максимум ~600 КБ разом)
_log_lock = threading.Lock()
_log_state = {"n": 0}
_SECRET_RE = re.compile(r"(DeepL-Auth-Key\s+)\S+|(['\"]?key['\"]?\s*[:=]\s*['\"])[^'\"]+", re.I)


def log_path() -> str:
    return os.path.join(os.path.dirname(config_path()), "app.log")


def _mask_secrets(text: str) -> str:
    return _SECRET_RE.sub(lambda m: (m.group(1) or m.group(2)) + "***", text)


def _write_log(line: str) -> None:
    try:
        with _log_lock:
            path = log_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _log_state["n"] += 1
            if _log_state["n"] % 25 == 1 and os.path.exists(path) and os.path.getsize(path) > _LOG_MAX:
                os.replace(path, path + ".1")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {_mask_secrets(line)}\n")
    except Exception:
        pass  # журнал ніколи не повинен ламати програму


def log(*args):
    print("[WordLookup]", _mask_secrets(" ".join(str(a) for a in args)), flush=True)
    _write_log(" ".join(str(a) for a in args))


def log_exc(where: str = "") -> None:
    """Повна трасування помилки — у консоль і у файл журналу (викликати всередині except)."""
    log(f"ПОМИЛКА {where}".rstrip() + "\n" + traceback.format_exc().rstrip())


def read_log_tail(max_lines: int = 250) -> list:
    lines = []
    for path in (log_path() + ".1", log_path()):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines += f.read().splitlines()
        except OSError:
            pass
    return lines[-max_lines:]


def diagnostic_report(max_lines: int = 250) -> str:
    """Текст для вставки в чат: система, налаштування (ключ DeepL приховано), останні події."""
    safe = copy.deepcopy(cfg)
    if safe.get("deepl", {}).get("key"):
        safe["deepl"]["key"] = "***"
    head = [f"Word Lookup — звіт {datetime.now():%Y-%m-%d %H:%M:%S}",
            f"Система: {sys.platform}, Python {sys.version.split()[0]}",
            "Налаштування: " + json.dumps(safe, ensure_ascii=False)]
    tail = read_log_tail(max_lines)
    return "\n".join(head + ["", f"Останні події журналу ({len(tail)} рядків):"] + tail)


def _grab(x0, y0, x1, y1, frozen=None):
    """Знімок прямокутника екрана (обрізаний по межах усіх моніторів). Якщо передано
    frozen=(img, ox, oy) — уже готовий «стоп-кадр» (напр. з RegionSelector) — вирізає
    з нього, без нового звернення до екрана. Так виділена рамкою підказка, що зникає
    при русі миші (тултіп у грі), лишається на застиглому зображенні."""
    if frozen is not None:
        fimg, fox, foy = frozen
        left, top = max(fox, int(x0)), max(foy, int(y0))
        right = min(fox + fimg.width, int(x1))
        bottom = min(foy + fimg.height, int(y1))
        if right - left < 4 or bottom - top < 4:
            raise OcrError("Область надто мала або поза межами знімка")
        img = fimg.crop((left - fox, top - foy, right - fox, bottom - foy))
        return img, left, top, right, bottom, 1.0, 1.0
    try:
        import mss
        from PIL import Image
    except ImportError:
        raise OcrError("Встанови бібліотеки: pip install mss pillow")
    try:
        with mss.mss() as sct:
            vs = sct.monitors[0]
            left, top = max(vs["left"], int(x0)), max(vs["top"], int(y0))
            right = min(vs["left"] + vs["width"], int(x1))
            bottom = min(vs["top"] + vs["height"], int(y1))
            if right - left < 4 or bottom - top < 4:
                raise OcrError("Область надто мала або поза екраном")
            shot = sct.grab({"left": left, "top": top,
                             "width": right - left, "height": bottom - top})
    except OcrError:
        raise
    except Exception as e:
        raise OcrError(f"Не вдалося зробити знімок екрана: {e}")
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return (img, left, top, right, bottom,
            shot.width / (right - left), shot.height / (bottom - top))


def _attempts(engine, height_px=None):
    """Спроби OCR по черзі (назва, збільшення, інвертувати) — далі йдемо, лише якщо попередня не знайшла."""
    if engine == "tesseract":
        up = (clamp(cfg["ocr"]["upscale"], 1.0, 4.0) if height_px is None
              else clamp(48 / max(height_px, 1), 1.0, 4.0))
        return [("звичайний", up, False), ("інвертований", up, True)]
    return [("звичайний", 1.0, False), ("інвертований", 1.0, True), ("збільшений", 2.0, False)]


def _run_ocr(engine, img, up, invert):
    from PIL import Image, ImageOps
    work = img
    if up != 1.0:
        work = work.resize((max(1, int(img.width * up)), max(1, int(img.height * up))),
                           Image.LANCZOS)
    if invert:
        work = ImageOps.invert(work.convert("RGB"))
    return (ocr_tesseract if engine == "tesseract" else ocr_rapid)(work, up)


def _find_word_gap(img, box, min_gap_frac=0.26):
    """Шукає в межах box найширшу вертикальну прогалину — можливий пробіл, який OCR
    об'єднав із сусіднім словом. Повертає x-координату розриву (в межах img) або None."""
    x0, y0, x1, y1 = (int(v) for v in box)
    x0, y0 = max(x0, 0), max(y0, 0)
    x1, y1 = min(x1, img.width), min(y1, img.height)
    if x1 - x0 < 14 or y1 - y0 < 5:
        return None
    try:
        import numpy as np
    except ImportError:
        return None
    arr = np.asarray(img.crop((x0, y0, x1, y1)).convert("L"), dtype=np.float32)
    ink = arr.max(axis=0) - arr.min(axis=0)  # розкид яскравості по стовпцях (див. _gap_segments)
    if ink.max() < 8:  # надто мало контрасту — не варто довіряти
        return None
    is_bg = ink <= max(ink.max() * 0.12, 2.0)
    h, n = y1 - y0, len(is_bg)
    min_gap = max(3, int(h * min_gap_frac))
    runs, i = [], 0
    while i < n:
        if is_bg[i]:
            j = i
            while j < n and is_bg[j]:
                j += 1
            if j - i >= min_gap and i > 2 and j < n - 2:
                runs.append((i, j))
            i = j
        else:
            i += 1
    if not runs:
        return None
    a, b = max(runs, key=lambda ab: ab[1] - ab[0])
    return x0 + (a + b) / 2


def _reocr_crop(img, box, engine):
    """Вирізає фрагмент зображення і розпізнає його окремо — для уточнення однієї
    половини слова, яке OCR помилково злив із сусіднім."""
    from PIL import Image
    pad = 4
    x0, y0, x1, y1 = (int(v) for v in box)
    x0, y0 = max(x0 - pad, 0), max(y0 - pad, 0)
    x1, y1 = min(x1 + pad, img.width), min(y1 + pad, img.height)
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None
    up = 3.0
    crop = img.crop((x0, y0, x1, y1))
    big = crop.resize((max(1, int(crop.width * up)), max(1, int(crop.height * up))), Image.LANCZOS)
    try:
        words = (ocr_tesseract if engine == "tesseract" else ocr_rapid)(big, up)
    except Exception:
        return None
    text = " ".join(w.text for w in words).strip()
    return clean_word(text)[0] or None


def _split_merged_word(img, hit, cx, engine):
    """Якщо розпізнане «слово» без пробілу насправді два злиті слова (видно проміжок
    на зображенні), повертає уточнений hit лише з тим словом, де курсор. Інакше — None."""
    if not cfg["ocr"]["split_merged_words"] or " " in hit.text or len(hit.text) < 5:
        return None
    gap_x = _find_word_gap(img, (hit.x0, hit.y0, hit.x1, hit.y1))
    if gap_x is None:
        return None
    side = (hit.x0, hit.y0, gap_x, hit.y1) if cx < gap_x else (gap_x, hit.y0, hit.x1, hit.y1)
    refined = _reocr_crop(img, side, engine)
    if not refined or refined.lower() == hit.text.lower():
        return None
    log(f"OCR: «{hit.text}» виглядає як злиті слова, уточнено до «{refined}»")
    return hit._replace(text=refined, x0=side[0], x1=side[2])


def _find_word_at(x, y, debug, wide, frozen=None):
    LAST_OCR["count"] = 0
    engine = pick_engine()
    O = cfg["ocr"]
    rw = int(clamp(O["region_w"], 200, 2000) * (1.8 if wide else 1.0))
    rh = int(clamp(O["region_h"], 40, 600))
    img, left, top, right, bottom, sx, sy = _grab(x - rw / 2, y - rh / 2, x + rw / 2, y + rh / 2, frozen)
    cx, cy = (x - left) * sx, (y - top) * sy
    if debug is not None:
        debug.update(img=img, cursor=(cx, cy), engine=engine, variants=[],
                     region=(left, top, right, bottom), shot=img.size)
    hit, hit_words = None, []
    try:
        for name, up, invert in _attempts(engine):
            words = _run_ocr(engine, img, up, invert)
            LAST_OCR["count"] = max(LAST_OCR["count"], len(words))
            if debug is not None:
                debug["variants"].append((name, words))
            if hit is None:
                cand = [w for w in words if re.search(r"[A-Za-z]", w.text)]
                found = pick_word(cand, cx, cy, tol=14 * max(sx, 1.0))
                if found:
                    hit, hit_words = found, words
            if hit is not None and debug is None:
                break
    except OcrError:
        raise
    except Exception as e:
        log_exc()
        raise OcrError(f"Помилка OCR: {type(e).__name__}: {e}")
    if hit is None:
        return None

    line_words = next((ln for ln in cluster_lines(hit_words) if hit in ln), [hit])
    refined = _split_merged_word(img, hit, cx, engine)
    if refined is not None:
        hit = refined
    word, lead, trail = clean_word(hit.text)
    if not word:
        return None
    wd = hit.x1 - hit.x0
    x0, x1 = hit.x0 + wd * lead, hit.x1 - wd * trail
    return {
        "word": word,
        "box": (int(left + x0 / sx), int(top + hit.y0 / sy),
                int(left + x1 / sx), int(top + hit.y1 / sy)),
        "line_text": " ".join(w.text for w in line_words),
    }


def find_word_at(x: int, y: int, debug=None, wide=False, frozen=None):
    """Знімок екрана навколо (x, y) -> OCR -> слово під курсором.
    debug (dict) — збирає знімок і результати всіх спроб; wide=True — ширша область (для рядка);
    frozen=(img, ox, oy) — не знімати новий скріншот, а взяти вже готовий «стоп-кадр»
    (напр. якщо в грі підказка (тултіп) зникає при русі миші — її вже «заморожено»)."""
    t0 = time.time()
    try:
        return _find_word_at(x, y, debug, wide, frozen)
    finally:
        LAST_OCR["time"] = time.time() - t0


def cluster_lines(words):
    """Групує слова у рядки за фактичним вертикальним розташуванням (перекриттям
    по Y центру й висоті), а НЕ за 'id рядка' з самого OCR. RapidOCR часто дає
    кожному виявленому текстовому фрагменту власний id, навіть якщо він візуально
    на одному рядку з сусідніми (напр. "Slowdive", "·", "Sleep (2022 Remastered)")
    — тоді групування за id розбиває один рядок на кілька й порядок слів ламається.
    Повертає список рядків (згори вниз), кожен — список слів (зліва направо)."""
    if not words:
        return []
    items = sorted(words, key=lambda w: (w.y0 + w.y1) / 2)
    lines = []
    for w in items:
        wc = (w.y0 + w.y1) / 2
        wh = max(w.y1 - w.y0, 1)
        placed = False
        for line in lines:
            lc = sum((x.y0 + x.y1) / 2 for x in line) / len(line)
            lh = sum(x.y1 - x.y0 for x in line) / len(line)
            if abs(wc - lc) <= max(lh, wh) * 0.55:  # достатньо близько по вертикалі -> той самий рядок
                line.append(w)
                placed = True
                break
        if not placed:
            lines.append([w])
    lines.sort(key=lambda line: min(x.y0 for x in line))
    return [sorted(line, key=lambda w: w.x0) for line in lines]


def ocr_region(x0, y0, x1, y1, debug=None, frozen=None):
    """OCR виділеної рамки. Повертає слова в екранних координатах у порядку читання.
    frozen=(img, ox, oy) — брати дані з уже готового «стоп-кадру», а не з нового знімка."""
    LAST_OCR["count"] = 0
    engine = pick_engine()
    m = 6  # невеликий запас навколо рамки — не відрізати краї літер
    img, L, T, R, B, sx, sy = _grab(x0 - m, y0 - m, x1 + m, y1 + m, frozen)
    sel = ((x0 - L) * sx, (y0 - T) * sy, (x1 - L) * sx, (y1 - T) * sy)
    tol = 3 * max(sx, 1.0)
    inside = []
    try:
        for name, up, invert in _attempts(engine, (y1 - y0) * sy):
            words = _run_ocr(engine, img, up, invert)
            LAST_OCR["count"] = max(LAST_OCR["count"], len(words))
            if debug is not None:
                debug.setdefault("variants", []).append((name, words))
            inside = [w for w in words
                      if sel[0] - tol <= (w.x0 + w.x1) / 2 <= sel[2] + tol
                      and sel[1] - tol <= (w.y0 + w.y1) / 2 <= sel[3] + tol]
            if inside:
                break
    except OcrError:
        raise
    except Exception as e:
        log_exc()
        raise OcrError(f"Помилка OCR: {type(e).__name__}: {e}")
    if not inside:
        return []
    if len(inside) == 1:
        merged = inside[0]
        refined = _split_merged_word(img, merged, (sel[0] + sel[2]) / 2, engine)
        if refined is not None:
            inside = [refined]
    ordered = [w for line in cluster_lines(inside) for w in line]
    return [W(w.text, L + w.x0 / sx, T + w.y0 / sy, L + w.x1 / sx, T + w.y1 / sy, w.line)
            for w in ordered]


# ------------------ повне речення навколо слова ------------------
_ABBR = {"mr", "mrs", "ms", "dr", "st", "vs", "jr", "sr", "prof", "mt"}


def _ends_sentence(tok: str) -> bool:
    """Токен завершує речення: .!? (з можливими лапками/дужками після), але не «Dr.»
    і не три крапки (пауза в субтитрах — речення триває)."""
    t = tok.rstrip("\"'”’»)]}")
    if not t or t[-1] not in ".!?" or t.endswith(("...", "…")):
        return False
    return not (t.endswith(".") and t[:-1].lower() in _ABBR)


def _sentence_around(tokens: list, idx: int, max_tokens: int = 40) -> str:
    """Речення, що містить токен idx. tokens — список рядків."""
    start = idx
    while start > 0 and idx - start < max_tokens and not _ends_sentence(tokens[start - 1]):
        start -= 1
    end = idx
    while end < len(tokens) - 1 and end - idx < max_tokens and not _ends_sentence(tokens[end]):
        end += 1
    seg = tokens[start:end + 1]
    if len(seg) > 1 and seg[0].endswith(":") and len(seg[0]) <= 16:  # «Evelyn:» — не частина речення
        seg = seg[1:]
    return " ".join(seg)


def _ocr_wide(img, engine, invert=False):
    """OCR широкої смуги шматками по ≤900 px із перекриттям: RapidOCR зменшує вхід до ~960 px,
    тож по всій ширині дрібний текст псувався б. Слова з перекриття беруться лише з
    того шматка, де вони в «серцевині» (обрізані на краю відкидаються)."""
    from PIL import ImageOps
    work = ImageOps.invert(img.convert("RGB")) if invert else img
    run = ocr_tesseract if engine == "tesseract" else ocr_rapid
    total = work.width
    if engine == "tesseract" or total <= 960:
        return run(work, 1.0)
    chunk, overlap = 900, 120
    out = []
    for i, st in enumerate(range(0, max(1, total - overlap), chunk - overlap)):
        en = min(total, st + chunk)
        if en - st < 60:
            continue
        lo = st + (overlap // 2 if i > 0 else 0)
        hi = en - (overlap // 2 if en < total else 0)
        for w in run(work.crop((st, 0, en, work.height)), 1.0):
            if lo <= st + (w.x0 + w.x1) / 2 < hi:
                out.append(w._replace(x0=w.x0 + st, x1=w.x1 + st))
    return out


def _pick_anchor(words: list, hit: dict):
    """Слово зі смуги, що відповідає знайденому слову (за текстом і близькістю)."""
    bx0, by0, bx1, by1 = hit["box"]
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    target = hit["word"].lower()
    best, best_d = None, 1e9
    for w in words:
        d = math.hypot((w.x0 + w.x1) / 2 - cx, (w.y0 + w.y1) / 2 - cy)
        if clean_word(w.text)[0].lower() == target and d < best_d and d <= max(60, 2 * (by1 - by0)):
            best, best_d = w, d
    if best:
        return best
    inside = [w for w in words if w.x0 <= cx <= w.x1 and w.y0 <= cy <= w.y1]
    return inside[0] if inside else None


def find_sentence(hit: dict, frozen=None) -> str:
    """Повне речення, у якому стоїть знайдене слово. Знімає смугу екрана навколо слова на
    повну ширину (±1100 px) і висоту ~2 рядки вгору/вниз, розпізнає її, збирає
    підписи, перенесені на кілька рядків, і обрізає по межах речення. Порожній
    рядок — якщо надійно визначити не вдалось (тоді лишається короткий рядок)."""
    engine = pick_engine()
    bx0, by0, bx1, by1 = hit["box"]
    h = max(10.0, by1 - by0)
    cy = (by0 + by1) / 2
    img, left, top, right, bottom, sx, sy = _grab(bx0 - 1100, cy - 2.3 * h, bx1 + 1100, cy + 2.3 * h, frozen)
    anchor, screen = None, []
    for invert in (False, True):  # світлий текст на темному і навпаки
        words = [w for w in _ocr_wide(img, engine, invert) if re.search(r"[A-Za-z0-9]", w.text)]
        screen = [w._replace(x0=left + w.x0 / sx, x1=left + w.x1 / sx,
                             y0=top + w.y0 / sy, y1=top + w.y1 / sy) for w in words]
        anchor = _pick_anchor(screen, hit)
        if anchor is not None:
            break
    if anchor is None:
        return ""
    lines = cluster_lines(screen)
    ai = next(i for i, ln in enumerate(lines) if anchor in ln)

    def height(ln): return sum(w.y1 - w.y0 for w in ln) / len(ln)
    def centre(ln): return sum((w.y0 + w.y1) / 2 for w in ln) / len(ln)
    def near(a, b): return abs(centre(a) - centre(b)) <= 1.9 * height(a) and 0.6 <= height(b) / height(a) <= 1.7
    def starts_lower(ln): return ln[0].text[:1].islower()
    lo = hi = ai
    # рядок продовжує речення, лише якщо попередній не закінчився крапкою, а цей починається
    # з малої літери — так пункти меню («RESUME GAME / OPTIONS») не склеюються в «речення»
    while lo > 0 and near(lines[lo], lines[lo - 1]) and starts_lower(lines[lo]) \
            and not _ends_sentence(lines[lo - 1][-1].text):
        lo -= 1
    while hi < len(lines) - 1 and near(lines[hi], lines[hi + 1]) and starts_lower(lines[hi + 1]) \
            and not _ends_sentence(lines[hi][-1].text):
        hi += 1
    ordered = [w for ln in lines[lo:hi + 1] for w in ln]
    idx = next(i for i, w in enumerate(ordered) if w is anchor)
    sentence = _sentence_around([w.text for w in ordered], idx)
    return sentence if len(sentence.split()) >= 2 else ""


def open_file(path: str) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def diag_report(dbg: dict, hit, err, pos):
    """Текстовий звіт + шлях до знімка з рамками розпізнаних слів."""
    lines = [f"Курсор: {pos}"]
    path = None
    if err:
        lines.append(err)
    if dbg.get("img") is not None:
        lines.append(f"Рушій: {dbg['engine']}; знімок: {dbg['shot'][0]}×{dbg['shot'][1]} px")
        if "time" in dbg:
            lines.append(f"Час OCR (усі спроби): {dbg['time']:.2f} с")
        try:
            from PIL import ImageDraw
            img = dbg["img"].copy()
            d = ImageDraw.Draw(img)
            colors = ["#00c000", "#ff8800"]
            for i, (name, words) in enumerate(dbg["variants"]):
                sample = (": " + " ".join(w.text for w in words[:20])) if words else ""
                lines.append(f"{name}: розпізнано слів — {len(words)}{sample}")
                for w in words:
                    d.rectangle((w.x0, w.y0, w.x1, w.y1), outline=colors[i % 2], width=2)
            cx, cy = dbg["cursor"]
            d.line((cx - 12, cy, cx + 12, cy), fill="#ff0000", width=2)
            d.line((cx, cy - 12, cx, cy + 12), fill="#ff0000", width=2)
            path = os.path.join(os.path.dirname(config_path()), "ocr_debug.png")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            img.save(path)
            if all(len(w) == 0 for _, w in dbg["variants"]):
                lines.append("OCR нічого не розпізнав. Відкрий знімок: якщо він чорний — гра в "
                             "повноекранному режимі (потрібен borderless); якщо текст видно — "
                             "збільш «Збільшення знімка» до 3 або спробуй Tesseract.")
        except Exception as e:
            lines.append(f"Не вдалося зберегти знімок: {e}")
    lines.append(f"Слово під курсором: {hit['word']}" if hit else "Слово під курсором: НЕ знайдено")
    return "\n".join(lines), path


# ============================== Вікна: підсвічування та картка =============
class ScrolledPanel(tk.Frame):
    """Прокручування лише панелі під курсором, без глобального bind_all."""

    def __init__(self, parent, width=560, height=500, bg=None):
        bg = bg or parent.cget("background")
        super().__init__(parent, bg=bg, width=width, height=height)
        self.pack_propagate(False)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg=bg)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = tk.Frame(self.canvas, bg=bg)
        self.item = self.canvas.create_window(0, 0, anchor="nw", window=self.content)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.content.bind("<Configure>", self._sync)
        self.canvas.bind("<Configure>", self._resize)
        self._top = self.winfo_toplevel()
        self._wheel_ids = {seq: self._top.bind(seq, self._wheel, add="+")
                           for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>")}
        self.bind("<Destroy>", self._cleanup)

    def _sync(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize(self, event):
        self.canvas.itemconfigure(self.item, width=max(1, event.width))

    def _wheel(self, event):
        x, y = event.x_root, event.y_root
        if not (self.winfo_rootx() <= x < self.winfo_rootx() + self.winfo_width()
                and self.winfo_rooty() <= y < self.winfo_rooty() + self.winfo_height()):
            return
        if self.canvas.yview() == (0.0, 1.0):
            return
        delta = getattr(event, "delta", 0)
        units = (-max(1, abs(int(delta / 120))) if delta > 0 else
                 max(1, abs(int(delta / 120))) if delta < 0 else
                 -3 if event.num == 4 else 3)
        self.canvas.yview_scroll(units, "units")
        return "break"

    def _cleanup(self, event):
        if event.widget is self:
            for seq, callback in self._wheel_ids.items():
                try:
                    self._top.unbind(seq, callback)
                except tk.TclError:
                    pass


def screen_workarea(root, anchor):
    """Монітор біля слова, включно з моніторами ліворуч від основного."""
    x, y = int((anchor[0] + anchor[2]) / 2), int((anchor[1] + anchor[3]) / 2)
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes
            class MonitorInfo(ctypes.Structure):
                _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                            ("work", wintypes.RECT), ("flags", wintypes.DWORD)]
            user = ctypes.windll.user32
            user.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
            user.MonitorFromPoint.restype = wintypes.HANDLE
            user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            monitor = user.MonitorFromPoint(wintypes.POINT(x, y), 2)
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            if user.GetMonitorInfoW(monitor, ctypes.byref(info)):
                r = info.work
                return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
    try:
        import mss
        with mss.mss() as sct:
            for m in sct.monitors[1:]:
                if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
                    return m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"]
    except Exception:
        pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def place_popup(anchor, width, height, area, toast=False):
    left, top, right, bottom = area
    width, height = min(width, right - left - 16), min(height, bottom - top - 16)
    cx = (anchor[0] + anchor[2]) / 2
    above = not toast and anchor[1] - height - 6 >= top + 8
    x = anchor[0] + 12 if toast else cx - width / 2
    y = anchor[1] + 20 if toast else anchor[1] - height - 6 if above else anchor[3] + 6
    return (int(clamp(x, left + 8, right - width - 8)),
            int(clamp(y, top + 8, bottom - height - 8)), int(width), int(height), above)


class Highlight:
    """Жовте підсвічування слова (як у macOS Look Up)."""

    def __init__(self, app, text, box):
        color = cfg["popup"]["highlight_color"]
        x0, y0, x1, y1 = box
        self.win = w = tk.Toplevel(app.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=color)
        h = max(12, y1 - y0)
        font = tkfont.Font(family=FONT, size=-int(h * 0.95))
        tk.Label(w, text=text, font=font, fg="#000000", bg=color, padx=5, pady=0).pack()
        w.update_idletasks()
        lw, lh = w.winfo_reqwidth(), w.winfo_reqheight()
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        px, py = cx - lw // 2, cy - lh // 2
        w.geometry(f"{lw}x{lh}+{px}+{py}")
        self.rect = (px, py, px + lw, py + lh)

    def close(self):
        try:
            self.win.destroy()
        except Exception:
            pass


class Popup:
    """Картка словника: заокруглена панель зі стрілкою до слова."""

    def __init__(self, app, card, anchor, expanded=(), toast=False, timeout=None, pinned=False):
        self.app, self.card, self.anchor, self.toast = app, card, anchor, toast
        self.expanded = set(expanded)
        self.pinned = pinned
        self._timer = None
        P = cfg["popup"]
        self.th = th = THEMES.get(P["theme"], THEMES["light"])
        self.fs = int(clamp(P["font_size"], 9, 24))
        self.cw = int(clamp(P["width"], 300, 900))
        self.workarea = screen_workarea(app.root, anchor)
        self.cw = min(self.cw, self.workarea[2] - self.workarea[0] - 64)

        self.win = w = tk.Toplevel(app.root)
        w.withdraw()
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        self.rounded = False
        if sys.platform.startswith("win"):
            try:
                w.attributes("-transparentcolor", KEY)
                self.rounded = True
            except tk.TclError:
                pass
        base_bg = KEY if self.rounded else th["bg"]
        w.configure(bg=base_bg)
        alpha = clamp(P["opacity"], 0.4, 1.0)
        if alpha < 1.0:
            try:
                w.attributes("-alpha", alpha)
            except tk.TclError:
                pass

        self.canvas = tk.Canvas(w, bg=base_bg, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.viewport = ScrolledPanel(self.canvas, width=self.cw + 18, bg=th["bg"])
        self.content = self.viewport.content
        tk.Frame(self.content, width=self.cw, height=0, bg=th["bg"]).pack()  # мін. ширина

        if not toast:
            self._toolbar()
            if getattr(app, "_game_pause", None) is not None and app._game_pause.active:
                self.lbl("Гра призупинена · Esc — закрити переклад і відновити гру",
                         size=-2, color=th["dim"], pady=(0, 6))
        if toast:
            self.lbl(card["text"], size=-1)
        elif card["kind"] == "word":
            self._build_word()
        else:
            self._build_text()

        self._layout()
        w.deiconify()
        w.attributes("-topmost", True)
        if timeout is None:
            timeout = 1.8 if toast else float(clamp(P["timeout"], 0, 300))
        self._timeout_ms = int(timeout * 1000)
        self._arm_timeout()

    def _toolbar(self):
        row = tk.Frame(self.content, bg=self.th["bg"])
        row.pack(fill="x", pady=(0, 9))
        self.pin_btn = self._tool_button(row, "Відкріпити" if self.pinned else "Закріпити",
                                         self.toggle_pin)
        self.pin_btn.pack(side="left")
        self._tool_button(row, "Копіювати", self.copy_translation).pack(side="left", padx=5)
        self._tool_button(row, "✕", self.app.dismiss_lookup).pack(side="right")
        self.options_btn = self._tool_button(row, "Вигляд ▾", self._show_options)
        self.options_btn.pack(side="right", padx=5)
        self.win.bind("<Escape>", lambda _e: self.app.dismiss_lookup())
        self.win.bind("<Control-Return>", lambda _e: self.save_current())

    def _tool_button(self, parent, text, command):
        return tk.Button(parent, text=text, command=command, font=(FONT, 9),
                         bg=self.th["bg"], fg=self.th["link"], relief="flat", bd=0,
                         activebackground=self.th["line"], activeforeground=self.th["fg"],
                         padx=5, pady=3, cursor="hand2", takefocus=True)

    def _show_options(self):
        # Під час налаштування картка залишається відкритою, зокрема при кліку в меню.
        if not self.pinned:
            self.toggle_pin()
        menu = getattr(self, "options_menu", None)
        if menu is None:
            self.options_menu = menu = tk.Menu(self.win, tearoff=False, font=(FONT, 9))
            for path, label in CARD_CONTENT_OPTIONS:
                menu.add_checkbutton(label=label, variable=self.app._var(tk.BooleanVar, path,
                                                             after=self.app._refresh_preview))
            menu.add_separator()
            menu.add_command(label="Усі налаштування картки…", command=self.app.open_card_settings)
        try:
            menu.tk_popup(self.options_btn.winfo_rootx(),
                          self.options_btn.winfo_rooty() + self.options_btn.winfo_height())
        finally:
            menu.grab_release()

    def copy_translation(self):
        text = self.card.get("translation") or _card_primary_translation(self.card)
        if text:
            try:
                pyperclip.copy(text)
                self.app.toast("Переклад скопійовано")
            except Exception as e:
                self.app.toast(f"Буфер обміну недоступний: {e}")

    def save_current(self):
        if self.card.get("kind") in ("word", "text") and _entry_complete({
                "translation": _card_primary_translation(self.card),
                "definition": _card_definition(self.card)}):
            add_saved_word(self.card)
            self.app.refresh_saved_count()
            self.app.rerender(self)

    def toggle_pin(self):
        self.pinned = not self.pinned
        self.pin_btn.configure(text="Відкріпити" if self.pinned else "Закріпити")
        self._arm_timeout()

    def _arm_timeout(self):
        if self._timer:
            self.win.after_cancel(self._timer)
            self._timer = None
        if self._timeout_ms > 0 and not self.pinned:
            self._timer = self.win.after(self._timeout_ms, self._on_timeout)

    # ---------- допоміжні віджети ----------
    def lbl(self, text, size=0, bold=False, italic=False, color=None, indent=0, pady=(0, 0)):
        style = " ".join(s for s, f in (("bold", bold), ("italic", italic)) if f)
        font = (FONT, self.fs + size) + ((style,) if style else ())
        lb = tk.Label(self.content, text=text, font=font, fg=color or self.th["fg"],
                      bg=self.th["bg"], justify="left", anchor="w",
                      wraplength=self.cw - indent)
        lb.pack(anchor="w", padx=(indent, 0), pady=pady)
        return lb

    def rich_text(self, parts, color=None, bold=False, size=0, indent=6, pady=(0, 0)):
        """Абзац із підсвіченими фрагментами (слово в реченні, його переклад).
        parts: [(текст, виділити?), ...]."""
        th = self.th
        parts = [(t, h) for t, h in parts if t]
        if not parts:
            return
        font = tkfont.Font(family=FONT, size=self.fs + size, weight="bold" if bold else "normal")
        char_px = max(1, font.measure("0"))
        chars = max(10, (self.cw - indent) // char_px)
        total_px = font.measure("".join(t for t, _ in parts))
        lines = max(1, int(total_px * 1.15 / (chars * char_px)) + 1)
        w = tk.Text(self.content, wrap="word", width=chars, height=lines, bd=0,
                    highlightthickness=0, padx=0, pady=0, bg=th["bg"], fg=color or th["fg"],
                    font=font, cursor="arrow", takefocus=0)
        w.tag_configure("hl", background=cfg["popup"]["highlight_color"], foreground="#000000")
        for t, h in parts:
            w.insert("end", t, "hl" if h else "")
        w.config(state="disabled")
        w.pack(anchor="w", padx=(indent, 0), pady=pady)

    def section(self, title, first):
        th = self.th
        row = tk.Frame(self.content, bg=th["bg"])
        row.pack(fill="x", pady=(0 if first else 12, 4))
        tk.Label(row, text=title, font=(FONT, self.fs + 1), fg=th["head"],
                 bg=th["bg"]).pack(side="left")
        tk.Frame(row, height=1, bg=th["line"]).pack(side="left", fill="x", expand=True, padx=(8, 0))

    def headword(self, word, with_audio, show_phonetic=True, with_save=False):
        th = self.th
        row = tk.Frame(self.content, bg=th["bg"])
        row.pack(fill="x", padx=(6, 0))
        tk.Label(row, text=word, font=(FONT, self.fs + 1, "bold"), fg=th["fg"],
                 bg=th["bg"]).pack(side="left")
        if with_audio and AUDIO_OK and cfg["popup"]["btn_speak"]:
            for label, url in self.card.get("audios", []):
                b = tk.Label(row, text=f"🔊{label}", font=(FONT, self.fs - 2), fg=th["link"],
                             bg=th["bg"], cursor="hand2")
                b.pack(side="left", padx=(8, 0))
                b.bind("<Button-1>", lambda _e, u=url: play_audio(u))
        if with_save:
            self.save_button(row)
        if show_phonetic and self.card.get("phonetic"):
            self.lbl(f"| {self.card['phonetic']} |", size=-1, color=th["dim"], indent=6)

    def save_button(self, row):
        """Кнопка ⭐ Зберегти / ✓ Збережено — праворуч у рядку слова."""
        word = self.card["word"]
        entry = saved_entry(word)
        saved = entry is not None and _entry_complete(entry)
        btn = tk.Label(row, text=("✓ Збережено" if saved else "⭐ Зберегти"),
                       font=(FONT, self.fs - 2),
                       fg=(self.th["dim"] if saved else self.th["link"]),
                       bg=self.th["bg"], cursor=("arrow" if saved else "hand2"))
        btn.pack(side="right", padx=(6, 0))
        if not saved:
            def on_click(_e):
                if not (_card_primary_translation(self.card) or _card_definition(self.card)):
                    if self.card.get("dict_pending"):
                        self.app.toast("Переклад ще не завантажився — зачекай і спробуй ще раз")
                    else:
                        reason = self.card.get("note") or self.card.get("dict_note") or \
                                 "ні переклад, ні визначення не знайдені"
                        self.app.toast(f"Не вдалося зберегти «{self.card['word']}»: {reason}")
                    return
                if add_saved_word(self.card):
                    btn.config(text="✓ Збережено", fg=self.th["dim"], cursor="arrow")
                    btn.unbind("<Button-1>")
                    app = self.app
                    if hasattr(app, "refresh_saved_count"):
                        app.refresh_saved_count()
            btn.bind("<Button-1>", on_click)

    def more(self, key):
        b = tk.Label(self.content, text="Показати ще", font=(FONT, self.fs - 1, "bold"),
                     fg=self.th["link"], bg=self.th["bg"], cursor="hand2")
        b.pack(anchor="w", padx=(6, 0), pady=(3, 0))

        def expand(_e):
            self.expanded.add(key)
            self.app.rerender(self)
        b.bind("<Button-1>", expand)

    # ---------- вміст ----------
    def _build_text(self):
        tl = cfg["languages"]["target"]
        self.section(f"English – {LANG_EN.get(tl, tl)}", True)
        row = tk.Frame(self.content, bg=self.th["bg"])
        row.pack(fill="x", pady=4)
        self.save_button(row)
        if cfg["popup"]["sentence"]:
            self.lbl(self.card["original"], size=-1, color=self.th["dim"], indent=6)
        self.lbl(self.card["translation"], bold=True, indent=6, pady=(4, 0))
        self.context_photo = (context_photo(self.card.get("image_file", ""), self.win,
                                            size=(max(1, self.cw - 12), 220))
                              if cfg["popup"]["context_image"] else None)
        if self.context_photo:
            tk.Label(self.content, image=self.context_photo, bg=self.th["bg"]).pack(anchor="w", padx=6)

    def _build_word(self):
        """Порядок картки: слово -> головний переклад (найпомітніше) -> вимова ->
        основне значення й приклад -> синоніми/антоніми -> додаткові переклади внизу."""
        c, S, D, th = self.card, cfg["sections"], cfg["dictionary"], self.th
        max_def = int(clamp(D["max_definitions"], 1, 6))
        max_syn = int(clamp(D["max_synonyms"], 1, 20))
        meanings = c.get("meanings", [])
        groups = c.get("groups", [])

        # 1) Слово, озвучення, ⭐ Зберегти
        self.headword(c["word"], with_audio=True, show_phonetic=False, with_save=True)

        if c.get("corrected_from"):
            self.lbl(f"Схоже, гра показала «{c['corrected_from']}» — знайдено найближче "
                     f"слово «{c.get('base', '')}»", size=-1, color=th["dim"], indent=6,
                     pady=(2, 0))
        elif c.get("base") and c["base"].lower() != c["word"].lower():
            self.lbl(f"Форма слова «{c['base']}»", size=-1, color=th["dim"], indent=6,
                     pady=(2, 0))

        # речення з гри + його переклад (слово підсвічене в обох) — як у Translator
        sentence = c.get("sentence") or ""
        if sentence:
            if cfg["popup"]["sentence"]:
                self.rich_text(split_around_word(sentence, c["word"]), color=th["dim"], size=-1,
                               pady=(6, 0))
            stl = c.get("sentence_tr") or ""
            if stl and cfg["popup"]["sentence_translation"]:
                span = align_word_span(stl, primary_translations(c))
                parts = ([(stl[:span[0]], False), (stl[span[0]:span[1]], True), (stl[span[1]:], False)]
                         if span else [(stl, False)])
                self.rich_text(parts, pady=(2, 0))
            elif c.get("sentence_pending") and cfg["popup"]["sentence_translation"]:
                self.lbl("Перекладаю речення…", size=-1, color=th["dim"], indent=6, pady=(2, 0))

        # 2) Головний переклад — кілька варіантів одразу, якщо слово має різні значення
        # (напр. "chest" — і "груди", і "сундук"; один-єдиний переклад від Google
        # часто вгадує не той контекст)
        alts = primary_translations(c)
        if S["bilingual"] and alts:
            self.lbl(" / ".join(alts), size=1, bold=True, indent=6, pady=(6, 0))
        elif S["bilingual"] and c.get("note"):
            self.lbl("⚠ " + c["note"], color="#c2410c", indent=6, pady=(2, 0))

        # 3) Вимова і частина мови
        pos0 = meanings[0].get("pos", "") if meanings else ""
        bits = [b for b in ((f"/{c['phonetic']}/" if c.get("phonetic") and cfg["popup"]["phonetic"] else ""), pos0) if b]
        if bits:
            self.lbl("  ·  ".join(bits), size=-1, color=th["dim"], indent=6, pady=(1, 0))

        # 4-5) Основне значення й приклад використання
        if S["definitions"] and meanings:
            exp = "def" in self.expanded
            shown = meanings if exp else meanings[:max_def]
            first_def = True
            for m in shown:
                self.lbl(m["pos"], size=-1, bold=True, color=th["head"], indent=6, pady=(8, 0))
                for d in (m["defs"][:3] if exp else m["defs"][:1]):
                    self.lbl(d["d"], indent=6)
                    if first_def:  # переклад першого визначення — одразу під ним
                        first_def = False
                        if c.get("definition_tr") and cfg["popup"]["translate_definition"]:
                            self.lbl(c["definition_tr"], bold=True, indent=6, pady=(1, 0))
                        elif c.get("def_tr_pending") and cfg["popup"]["translate_definition"]:
                            self.lbl("…", color=th["dim"], indent=6)
                    if d["e"] and cfg["popup"]["examples"]:
                        self.lbl(d["e"], italic=True, color=th["example"], indent=6)
            if not exp and (len(meanings) > max_def or any(len(m["defs"]) > 1 for m in shown)):
                self.more("def")
        elif (S["definitions"] or S["thesaurus"]) and c.get("dict_note"):
            self.lbl("⚠ " + c["dict_note"], color="#c2410c", indent=6, pady=(8, 0))
            self.lbl('Спробуй кнопку «Діагностика словника» на вкладці «Головна».',
                     size=-1, color=th["dim"], indent=6, pady=(2, 0))
        elif (S["definitions"] or S["thesaurus"]) and c.get("dict_pending"):
            self.lbl("Завантажую визначення…", color=th["dim"], indent=6, pady=(8, 0))

        # 6) Синоніми / антоніми
        thes = [m for m in meanings if m["syn"] or m["ant"]]
        if S["thesaurus"] and thes:
            exp = "thes" in self.expanded
            shown = thes if exp else thes[:max_def]
            for m in shown:
                syn = m["syn"][: (15 if exp else max_syn)]
                if syn:
                    self.lbl("Синоніми: " + ", ".join(syn), indent=6, pady=(6, 0))
                if m["ant"]:
                    self.lbl("Антоніми: " + ", ".join(m["ant"][: (10 if exp else 5)]),
                             color=th["dim"], indent=6, pady=(2, 0))
            if not exp and (len(thes) > max_def or any(len(m["syn"]) > max_syn for m in shown)):
                self.more("thes")

        # 7) Додаткові варіанти перекладу — унизу, другорядно
        if S["bilingual"] and groups and cfg["popup"]["alternate_translations"]:
            self.lbl("Інші варіанти перекладу", size=-1, bold=True, color=th["head"],
                     indent=6, pady=(8, 2))
            for pos, terms in groups:
                label = f"{pos}: " if pos else ""
                self.lbl(label + ", ".join(terms), indent=6)
        self.context_photo = (context_photo(c.get("image_file", ""), self.win,
                                            size=(max(1, self.cw - 12), 220))
                              if cfg["popup"]["context_image"] else None)
        if self.context_photo:
            self.lbl("Контекст із гри", size=-1, color=th["dim"], indent=6, pady=(8, 2))
            tk.Label(self.content, image=self.context_photo, bg=th["bg"]).pack(anchor="w", padx=6)

    # ---------- розмітка й позиціювання ----------
    def _layout(self):
        c, th = self.canvas, self.th
        self.content.update_idletasks()
        cw = self.cw + 18
        ch = self.content.winfo_reqheight()
        pad = 14
        R = 14 if self.rounded else 0
        AH = 10 if (self.rounded and not self.toast) else 0
        max_height = self.workarea[3] - self.workarea[1] - 16
        ch = min(ch, max_height - 2 * pad - AH)
        self.viewport.configure(width=cw, height=ch)
        PW, PH = cw + 2 * pad, ch + 2 * pad
        W_, H_ = PW, PH + AH
        ax0, ay0, ax1, ay1 = self.anchor
        cx = (ax0 + ax1) / 2

        x, y, W_, H_, above = place_popup(self.anchor, W_, H_, self.workarea, self.toast)

        c.config(width=W_, height=H_)
        top = 0 if above else AH
        if self.rounded:
            ax = clamp(cx - x, R + AH + 4, PW - R - AH - 4)

            def rrect(x1, y1, x2, y2, r, fill):
                pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                       x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
                c.create_polygon(pts, smooth=True, fill=fill, outline="")

            rrect(0, top, PW, top + PH, R, th["border"])
            rrect(1, top + 1, PW - 1, top + PH - 1, R - 1, th["bg"])
            if AH:
                if above:
                    c.create_polygon(ax - AH, top + PH - 1, ax + AH, top + PH - 1,
                                     ax, top + PH + AH, fill=th["border"], outline="")
                    c.create_polygon(ax - AH + 1, top + PH - 2, ax + AH - 1, top + PH - 2,
                                     ax, top + PH + AH - 2, fill=th["bg"], outline="")
                else:
                    c.create_polygon(ax - AH, top + 1, ax + AH, top + 1,
                                     ax, 0, fill=th["border"], outline="")
                    c.create_polygon(ax - AH + 1, top + 2, ax + AH - 1, top + 2,
                                     ax, 2, fill=th["bg"], outline="")
        else:
            c.create_rectangle(0, 0, W_ - 1, H_ - 1, fill=th["bg"], outline=th["border"])
        c.create_window(pad, top + pad, window=self.viewport, anchor="nw")
        # '+-N' означає абсолютну від'ємну координату, '-N' — відступ від правого краю.
        self.win.geometry(f"{W_}x{H_}+{x}+{y}")

    def contains(self, x, y) -> bool:
        try:
            w = self.win
            return (w.winfo_rootx() <= x <= w.winfo_rootx() + w.winfo_width()
                    and w.winfo_rooty() <= y <= w.winfo_rooty() + w.winfo_height())
        except Exception:
            return False

    def _on_timeout(self):
        self._timer = None
        if not self.toast and (self.pinned or self.contains(*self.win.winfo_pointerxy())):
            self._arm_timeout()
            return
        if self.toast:
            self.close()
        else:
            self.app.dismiss_lookup()

    def close(self):
        try:
            if self._timer:
                self.win.after_cancel(self._timer)
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass


class RegionSelector:
    """Накладка на весь екран: тягни рамку навколо слова чи рядка (як у скріншотері).
    Наведи звичайний курсор на слово — воно підсвітиться, лівий клік шукає саме його;
    можна й обвести рамкою кілька слів. Esc або права кнопка — скасувати.
    Одразу при відкритті робить «стоп-кадр» усього екрана й показує саме його — тому
    підказка (тултіп), яка в грі зникає при русі миші, лишається на місці, поки ти
    спокійно виділяєш потрібне слово."""

    HOVER_DELAY_MS = 150  # слово розпізнається, коли курсор на мить зупинився

    def __init__(self, app, on_done):
        self.on_done, self.start, self.done = on_done, None, False
        self.hover_hit, self.mouse = None, (-9999, -9999)
        self._after_id, self._req, self._busy = None, None, False
        self._lock, self._hq = threading.Lock(), queue.Queue()
        self._prev_hwnd = _foreground_window()  # повернути фокус грі після закриття
        r = app.root
        try:
            x, y = r.winfo_vrootx(), r.winfo_vrooty()
            w, h = r.winfo_vrootwidth(), r.winfo_vrootheight()
            if w < 100 or h < 100:
                raise ValueError
        except Exception:
            x, y, w, h = 0, 0, r.winfo_screenwidth(), r.winfo_screenheight()
        self.w, self.h = w, h
        self.ox, self.oy = x, y  # початок віртуального екрана: екранні координати -> координати полотна
        self.frozen = None  # (img, ox, oy) — заповниться нижче, якщо знімок вдався

        self.win = win = tk.Toplevel(r)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="black")
        win.geometry(f"{w}x{h}+{x}+{y}")
        c = self.canvas = tk.Canvas(win, bg="black", highlightthickness=0, cursor="arrow")
        c.pack(fill="both", expand=True)

        frozen_ok = False
        try:
            from PIL import ImageTk
            shot_img, sx0, sy0, *_ = _grab(x, y, x + w, y + h)
            self._photo = ImageTk.PhotoImage(shot_img)  # тримаємо посилання — інакше Tk «забуде» картинку
            c.create_image(0, 0, image=self._photo, anchor="nw")
            self.frozen = (shot_img, sx0, sy0)
            frozen_ok = True
        except Exception as e:
            log(f"Стоп-кадр не вдався, буде звичайне затемнення: {type(e).__name__}: {e}")
            try:
                win.attributes("-alpha", 0.32)
            except tk.TclError:
                pass

        self.hover_enabled = bool(cfg["freeze"].get("hover_highlight", True)) and self.frozen is not None
        hl_color = cfg["popup"]["highlight_color"]
        self.hl_rect = c.create_rectangle(0, 0, 0, 0, fill=hl_color, stipple="gray50", outline=hl_color,
                                          state="hidden")  # підсвітка слова під курсором
        self.vline = c.create_line(0, 0, 0, h, fill="#00e5ff", state="hidden")  # перехрестя вимкнено:
        self.hline = c.create_line(0, 0, w, 0, fill="#00e5ff", state="hidden")  # курсор — звичайна стрілка
        self.rect = c.create_rectangle(0, 0, 0, 0, outline="#ffee00", width=2, state="hidden")
        self.size = c.create_text(0, 0, text="", fill="#ffee00", anchor="sw",
                                  font=(FONT, 11, "bold"), state="hidden")
        hint = ("⏸ Стоп-кадр — екран заморожено, підказки (тултіпи) нікуди не зникнуть.  "
                if frozen_ok else "") + \
               ("Наведи курсор на слово — воно підсвітиться  ·  клік лівою кнопкою — шукати  ·  "
                "або обведи рамкою кілька слів  ·  Esc / права кнопка — скасувати"
                if self.hover_enabled else
                "Тягни рамку навколо слова чи рядка  ·  клік — одне слово  ·  Esc / права кнопка — скасувати")
        if app._game_pause is not None and app._game_pause.active:
            hint = "⏸ Процес гри призупинений · Esc — відновити.  " + hint
        c.create_text(-x + r.winfo_screenwidth() // 2, -y + 34, fill="#ffffff",
                      font=(FONT, 13, "bold"), text=hint, width=r.winfo_screenwidth() - 80)
        c.bind("<Motion>", self._motion)
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Button-3>", lambda _e: self.finish(None))
        win.bind("<Escape>", lambda _e: self.finish(None))
        win.focus_force()
        c.focus_set()
        if self.hover_enabled:
            win.after(50, self._poll)
        release_cursor_clip()  # гра тримає курсор у центрі — відпускаємо, щоб він рухався
        win.after(100, self._unclip_tick)

    def _unclip_tick(self):
        """Деякі ігри самі повертають обмеження курсора — відпускаємо знову, поки накладка відкрита."""
        if self.done:
            return
        release_cursor_clip()
        try:
            self.win.after(100, self._unclip_tick)
        except Exception:
            pass

    # ---------- підсвітка слова під курсором ----------
    @staticmethod
    def _inside(box, x, y, margin=4) -> bool:
        return box[0] - margin <= x <= box[2] + margin and box[1] - margin <= y <= box[3] + margin

    def _clear_hover(self):
        self.hover_hit = None
        self.canvas.itemconfigure(self.hl_rect, state="hidden")

    def _set_hover(self, hit):
        self.hover_hit = hit
        bx0, by0, bx1, by1 = hit["box"]
        self.canvas.coords(self.hl_rect, bx0 - self.ox - 2, by0 - self.oy - 1,
                           bx1 - self.ox + 2, by1 - self.oy + 1)
        self.canvas.itemconfigure(self.hl_rect, state="normal")

    def _motion(self, e):
        self.mouse = (e.x_root, e.y_root)
        if not self.hover_enabled or self.start:  # під час перетягування рамки не розпізнаємо
            return
        if self.hover_hit and not self._inside(self.hover_hit["box"], e.x_root, e.y_root):
            self._clear_hover()
        if self.hover_hit is None:
            if self._after_id is not None:
                try:
                    self.win.after_cancel(self._after_id)
                except Exception:
                    pass
            x, y = e.x_root, e.y_root
            self._after_id = self.win.after(self.HOVER_DELAY_MS, lambda: self._request(x, y))

    def _request(self, x, y):
        self._after_id = None
        if self.done:
            return
        with self._lock:
            self._req = (x, y)
            spawn = not self._busy
            self._busy = True
        if spawn:
            threading.Thread(target=self._ocr_worker, daemon=True).start()

    def _ocr_worker(self):
        """Один фоновий потік: бере лише НАЙНОВІШИЙ запит, старі пропускає."""
        while True:
            with self._lock:
                req, self._req = self._req, None
                if req is None:
                    self._busy = False
                    return
            try:
                hit = find_word_at(req[0], req[1], frozen=self.frozen)
            except Exception:
                hit = None
            self._hq.put((req, hit))

    def _poll(self):
        if self.done:
            return
        latest = None
        while True:
            try:
                latest = self._hq.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            (rx, ry), hit = latest
            mx, my = self.mouse
            if abs(rx - mx) <= 12 and abs(ry - my) <= 12:  # відповідь ще актуальна
                if hit:
                    self._set_hover(hit)
                else:
                    self._clear_hover()
        try:
            self.win.after(50, self._poll)
        except Exception:
            pass

    def _move(self, e):
        self.canvas.coords(self.vline, e.x, 0, e.x, self.h)
        self.canvas.coords(self.hline, 0, e.y, self.w, e.y)

    def _press(self, e):
        self.start = (e.x, e.y, e.x_root, e.y_root)
        self.canvas.itemconfigure(self.rect, state="normal")

    def _drag(self, e):
        self._move(e)
        if self.start:
            sx, sy = self.start[:2]
            self.canvas.coords(self.rect, sx, sy, e.x, e.y)
            self.canvas.coords(self.size, min(sx, e.x), min(sy, e.y) - 4)
            self.canvas.itemconfigure(self.size, text=f"{abs(e.x - sx)}×{abs(e.y - sy)}",
                                      state="normal")

    def _release(self, e):
        if not self.start:
            return
        x0, x1 = sorted((self.start[2], e.x_root))
        y0, y1 = sorted((self.start[3], e.y_root))
        if x1 - x0 < 6 and y1 - y0 < 6:
            hit = self.hover_hit
            if hit and self._inside(hit["box"], e.x_root, e.y_root):
                self.finish(("word", e.x_root, e.y_root, hit))  # слово вже розпізнано — без повторного OCR
            else:
                self.finish(("point", e.x_root, e.y_root))
        else:
            self.finish(("rect", x0, y0, x1, y1))

    def finish(self, result):
        if self.done:
            return
        self.done = True
        frozen = self.frozen
        if getattr(self, "_after_id", None) is not None:
            try:
                self.win.after_cancel(self._after_id)
            except Exception:
                pass
        try:
            self.win.destroy()
        except Exception:
            pass
        _restore_foreground(self._prev_hwnd)
        self.on_done(result, frozen)


class FreeCursorOverlay:
    """Приціл для ігор, де миша захоплена грою (raw input) і системний курсор не
    рухається (напр. Cyberpunk 2077). Керування йде ЗЗОВНІ (через App): стрілки/WASD —
    з глобального хуку клавіатури, рух — від фізичної миші (метод nudge). Це вікно
    саме по собі НЕ приймає клавіші — звичайні Tk-прив'язки клавіш працюють лише
    коли вікно у фокусі, а повноекранні ігри фокус не віддають, тому раніше приціл
    «не рухався». Глобальний хук працює незалежно від фокусу."""

    STEP, STEP_FAST = 8, 40

    def __init__(self, app, start_pos, on_done):
        self.on_done, self.done = on_done, False
        self._prev_hwnd = _foreground_window()  # повернути фокус грі після закриття
        r = app.root
        try:
            x, y = r.winfo_vrootx(), r.winfo_vrooty()
            w, h = r.winfo_vrootwidth(), r.winfo_vrootheight()
            if w < 100 or h < 100:
                raise ValueError
        except Exception:
            x, y, w, h = 0, 0, r.winfo_screenwidth(), r.winfo_screenheight()
        self.ox, self.oy, self.w, self.h = x, y, w, h
        sx, sy = start_pos
        self.x = min(max(sx, x), x + w - 1)
        self.y = min(max(sy, y), y + h - 1)

        self.win = win = tk.Toplevel(r)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        self.rounded = False
        if sys.platform.startswith("win"):
            try:
                win.attributes("-transparentcolor", KEY)  # майже невидиме вікно — видно лише приціл
                self.rounded = True
            except tk.TclError:
                pass
        bg = KEY if self.rounded else "black"
        if not self.rounded:
            try:
                win.attributes("-alpha", 0.05)
            except tk.TclError:
                pass
        win.configure(bg=bg)
        win.geometry(f"{w}x{h}+{x}+{y}")
        c = self.canvas = tk.Canvas(win, bg=bg, highlightthickness=0)
        c.pack(fill="both", expand=True)

        c.create_text(w // 2, 30, fill="#ffffff", font=(FONT, 13, "bold"),
                      text="Рухай мишею або стрілками/WASD · Enter/Пробіл — шукати слово · "
                           "Esc — скасувати")
        R = 14
        self.h1 = c.create_line(0, 0, 0, 0, fill="#ffee00", width=2)
        self.h2 = c.create_line(0, 0, 0, 0, fill="#ffee00", width=2)
        self.v1 = c.create_line(0, 0, 0, 0, fill="#ffee00", width=2)
        self.v2 = c.create_line(0, 0, 0, 0, fill="#ffee00", width=2)
        self.ring = c.create_oval(0, 0, 0, 0, outline="#ffee00", width=2)
        self._redraw()
        # необов'язковий бонус: якщо вікно все ж отримає фокус (віконний режим гри),
        # Esc спрацює і напряму — але керування НЕ залежить від цього
        win.bind("<Escape>", lambda _e: self.finish(None))
        win.focus_force()
        c.focus_set()
        release_cursor_clip()  # інакше миша «застрягає» в центрі й нічого не рухає
        win.after(100, self._unclip_tick)

    def _unclip_tick(self):
        if self.done:
            return
        release_cursor_clip()
        try:
            self.win.after(100, self._unclip_tick)
        except Exception:
            pass

    def _redraw(self):
        x, y, R, gap = self.x, self.y, 14, 5
        self.canvas.coords(self.h1, x - R, y, x - gap, y)
        self.canvas.coords(self.h2, x + gap, y, x + R, y)
        self.canvas.coords(self.v1, x, y - R, x, y - gap)
        self.canvas.coords(self.v2, x, y + gap, x, y + R)
        self.canvas.coords(self.ring, x - R, y - R, x + R, y + R)

    def nudge(self, dx, dy):
        """dx, dy — уже готове зміщення в пікселях (від фізичної миші або клавіші)."""
        if self.done:
            return
        self.x = min(max(self.x + dx, self.ox), self.ox + self.w - 1)
        self.y = min(max(self.y + dy, self.oy), self.oy + self.h - 1)
        self._redraw()

    def key(self, name, fast):
        """Викликає App при отриманні клавіші з глобального хуку (незалежно від фокусу)."""
        step = self.STEP_FAST if fast else self.STEP
        if name in ("up", "w"):
            self.nudge(0, -step)
        elif name in ("down", "s"):
            self.nudge(0, step)
        elif name in ("left", "a"):
            self.nudge(-step, 0)
        elif name in ("right", "d"):
            self.nudge(step, 0)
        elif name in ("enter", "space"):
            self.finish((self.x, self.y))
        elif name == "esc":
            self.finish(None)

    def finish(self, result):
        if self.done:
            return
        self.done = True
        try:
            self.win.destroy()
        except Exception:
            pass
        _restore_foreground(self._prev_hwnd)
        self.on_done(result)


# ============================== Значок у треї ==============================
def make_tray_icon():
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(59, 130, 246, 255))
    try:
        try:
            font = ImageFont.load_default(size=38)
        except TypeError:
            font = ImageFont.load_default()
        d.text((32, 32), "A", fill="white", font=font, anchor="mm")
    except Exception:
        d.ellipse((20, 20, 44, 44), fill="white")
    return img


# ============================== Програма ===================================
def _get(path):
    d = cfg
    for p in path:
        d = d[p]
    return d


def _put(path, val):
    d = cfg
    for p in path[:-1]:
        d = d[p]
    d[path[-1]] = val


class ReviewWindow:
    """Спочатку згадати, потім перевірити; складні слова — ще раз. Без мережі."""

    def __init__(self, app, entries):
        self.app, self.pending = app, list(entries)
        self.total, self.completed, self.revealed = len(entries), 0, False
        self.win = tk.Toplevel(app.root)
        self.win.title("Повторення слів — Word Lookup")
        self.win.geometry("640x620")
        self.win.minsize(540, 380)
        self.progress = tk.StringVar()
        ttk.Label(self.win, textvariable=self.progress, padding=12).pack(fill="x")
        self.mode = tk.StringVar(value="Англійська → Переклад")
        mode_row = ttk.Frame(self.win, padding=(12, 0, 12, 8))
        mode_row.pack(fill="x")
        mode_box = ttk.Combobox(mode_row, textvariable=self.mode, state="readonly", width=32,
                                values=["Англійська → Переклад", "Переклад → Англійська", "Пропуск у реченні"])
        mode_box.pack(side="left")
        mode_box.bind("<<ComboboxSelected>>", lambda _e: self.next_card())
        self.panel = ScrolledPanel(self.win)
        self.panel.pack(fill="both", expand=True, padx=12)
        self.body = self.panel.content
        buttons = ttk.Frame(self.win, padding=12)
        buttons.pack(fill="x")
        self.reveal_btn = ttk.Button(buttons, text="Показати відповідь (Пробіл)", command=self.reveal)
        self.reveal_btn.pack(fill="x", pady=(0, 8))
        self.again_btn = ttk.Button(buttons, text="1 · Ще раз", command=lambda: self.grade(False))
        self.again_btn.pack(side="left", fill="x", expand=True)
        self.known_btn = ttk.Button(buttons, text="2 · Знаю", command=lambda: self.grade(True))
        self.known_btn.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.win.bind("<space>", lambda _e: self.reveal())
        self.win.bind("<Key-1>", lambda _e: self.grade(False))
        self.win.bind("<Key-2>", lambda _e: self.grade(True))
        self.win.bind("<Escape>", lambda _e: self.win.destroy())
        self.next_card()

    def _label(self, text, font=(FONT, 12)):
        ttk.Label(self.body, text=text, font=font, wraplength=550, justify="left"
                  ).pack(anchor="w", padx=12, pady=8)

    def next_card(self):
        for child in self.body.winfo_children():
            child.destroy()
        self.panel.canvas.yview_moveto(0)
        self.revealed = False
        self.again_btn.state(["disabled"])
        self.known_btn.state(["disabled"])
        self.reveal_btn.state(["!disabled"] if self.pending else ["disabled"])
        self.progress.set(f"Згадано: {self.completed} / {self.total} · Залишилось: {len(self.pending)}")
        if not self.pending:
            self._label("Повторення завершено!", font=(FONT, 22, "bold"))
            self._label("Усі слова цього набору пройдено. Можна повернутися до гри.")
            return
        entry = self.pending[0]
        prompt = entry["word"]
        if self.mode.get() == "Переклад → Англійська":
            prompt = entry.get("translation") or prompt
        elif self.mode.get() == "Пропуск у реченні":
            sentence = entry.get("context") or entry.get("example") or ""
            pattern = r"(?<!\w)(?:" + "|".join(re.escape(t) for t in sorted(
                {entry["word"], entry.get("base") or entry["word"]}, key=len, reverse=True)) + r")(?!\w)"
            prompt, count = re.subn(pattern, "______", sentence, flags=re.I)
            if not count:
                prompt = entry["word"]
        self._label(prompt, font=(FONT, 22, "bold"))
        self._label("Згадай переклад, потім відкрий відповідь.")

    def reveal(self):
        if not self.pending or self.revealed:
            return "break"
        self.revealed = True
        e = self.pending[0]
        if self.mode.get() != "Англійська → Переклад":
            self._label(e["word"], font=(FONT, 20, "bold"))
        self._label(e.get("translation", "") or "Переклад не збережено", font=(FONT, 18, "bold"))
        for key in ("definition", "definition_tr", "example", "context", "context_tr"):
            if e.get(key):
                self._label(e[key])
        self.photo = context_photo(e.get("image_file", ""), self.win)
        if self.photo:
            ttk.Label(self.body, image=self.photo).pack(padx=12, pady=8)
        self.again_btn.state(["!disabled"])
        self.known_btn.state(["!disabled"])
        self.reveal_btn.state(["disabled"])
        return "break"

    def grade(self, known):
        if not self.pending or not self.revealed:
            return "break"
        e = self.pending[0]
        if saved_entry(e["word"]) is None:
            self._label("Цю картку видалено з бібліотеки.")
            self.pending.pop(0)
            self.next_card()
            return "break"
        try:
            record_review(saved_entry(e["word"]), known)
        except OSError as ex:
            self._label(str(ex))
            return "break"
        self.pending.pop(0)
        if known:
            self.completed += 1
        else:
            self.pending.append(e)
        self.next_card()
        self.app.refresh_saved_count()
        return "break"


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        tkfont.nametofont("TkDefaultFont").configure(family=FONT, size=9)
        tkfont.nametofont("TkTextFont").configure(family=FONT, size=10)
        self._setting_vars = {}
        self._config_save_timer = None
        self._preview_timer = None
        self._apply_style()
        self.kb = keyboard.Controller()
        self.ms = mouse.Controller()
        self.popup = self.highlight = self.toast_popup = None
        self.selector = None
        self._game_pause = None
        self._game_pause_hwnd = None
        self.free_cursor = None
        self.free_cursor_pos = None
        self.saved_checks = {}
        self._fc_lock = threading.Lock()
        self._fc_delta = [0, 0]
        self._fc_mouse_listener = None
        self._fc_mouse_pos = None
        self.hl_hit = None
        self.active_token = None
        self.paused = False
        self.last_card = None
        self.busy = threading.Lock()
        self.recording = None
        self.tray = None
        self.press_state = {"pos": (0, 0), "t": 0.0}
        self.auto_state = {"press": (0, 0), "last_release": 0.0}

        self.manager = HotkeyManager(lambda a: events.put(("action", a)))
        self.manager.set_bindings(cfg["hotkeys"])
        self.manager.on_esc = lambda: events.put(("esc",))

        self._build_ui()
        self._refresh_hint()
        self._refresh_status()
        self._refresh_cache_label()
        self.refresh_saved_count()
        self._check_ocr()
        self.manager.start()
        self.mouse_listener = mouse.Listener(on_click=self._on_click)
        self.mouse_listener.daemon = True
        self.mouse_listener.start()
        self._setup_tray()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(40, self._pump)
        if cfg["app"]["start_minimized"]:
            self.root.after(300, self.hide_window)

    def run(self):
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass

    # ---------- допоміжне для налаштувань ----------
    def _schedule_config_save(self):
        if self._config_save_timer is not None:
            self.root.after_cancel(self._config_save_timer)
        self._config_save_timer = self.root.after(300, self._flush_config_save)

    def _flush_config_save(self):
        if self._config_save_timer is not None:
            self.root.after_cancel(self._config_save_timer)
            self._config_save_timer = None
            save_config(cfg)

    def _var(self, cls, path, mapping=None, after=None):
        path = tuple(path)
        if path in self._setting_vars:
            return self._setting_vars[path]
        inv = {v: k for k, v in mapping.items()} if mapping else None
        init = _get(path)
        var = cls(value=inv.get(init, init) if inv else init)

        def cb(*_):
            try:
                val = var.get()
            except tk.TclError:
                return
            if mapping:
                if val not in mapping:
                    return
                val = mapping[val]
            _put(path, val)
            self._schedule_config_save()
            if after:
                after()
        var.trace_add("write", cb)
        self._setting_vars[path] = var
        return var

    def _combo(self, parent, var, mapping, width=26):
        return ttk.Combobox(parent, textvariable=var, values=list(mapping),
                            state="readonly", width=width)

    # ---------- інтерфейс ----------
    def _apply_style(self):
        bg, fg, accent = "#f5f7fb", "#24324a", "#2864dc"
        self.root.configure(bg=bg)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=(FONT, 9), background=bg, foreground=fg)
        style.configure("TButton", background="white", bordercolor="#d7e0ee",
                        padding=(10, 5), focusthickness=1, focuscolor=accent)
        style.map("TButton", background=[("active", "#e8effc"), ("pressed", "#d9e6ff")])
        style.configure("Accent.TButton", foreground="white", background=accent)
        style.map("Accent.TButton", background=[("active", "#1d50bb"), ("pressed", "#18429c")])
        style.configure("TNotebook", borderwidth=0, background=bg)
        style.configure("TNotebook.Tab", padding=(15, 8), background=bg)
        style.map("TNotebook.Tab", background=[("selected", "white")],
                  foreground=[("selected", accent)])
        style.configure("TLabelframe", bordercolor="#dce4ef", relief="solid")
        style.configure("TLabelframe.Label", font=(FONT, 9, "bold"), foreground="#4c6080")
        style.configure("TEntry", fieldbackground="white", padding=4)
        style.configure("Treeview", rowheight=27, fieldbackground="white", background="white")
        style.configure("Treeview.Heading", font=(FONT, 9, "bold"), padding=6)
        style.map("Treeview", background=[("selected", "#e1ebff")],
                  foreground=[("selected", "#1f4ea0")])

    def _build_ui(self):
        self.root.geometry(f"720x{min(740, self.root.winfo_screenheight() - 80)}")
        self.root.minsize(640, 420)
        header = ttk.Frame(self.root, padding=(20, 14, 20, 10))
        header.pack(fill="x")
        title = ttk.Frame(header)
        title.pack(side="left")
        ttk.Label(title, text="Word Lookup", font=(FONT, 16, "bold")).pack(anchor="w")
        ttk.Label(title, text="Перекладай у грі. Зберігай і повторюй слова.",
                  foreground="#61728b").pack(anchor="w", pady=(3, 0))
        ttk.Button(header, text="Бібліотека", style="Accent.TButton",
                   command=self.open_saved_words).pack(side="right")
        self.notebook = nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        tabs = {}
        for key, title in (("main", "Головна"), ("keys", "Кнопки"),
                           ("look", "Картка перекладу"), ("dict", "Словник і OCR")):
            panel = ScrolledPanel(nb, bg=self.root.cget("background"))
            f = ttk.Frame(panel.content, padding=14)
            f.pack(fill="both", expand=True)
            nb.add(panel, text=title)
            tabs[key] = f
            if key == "look":
                self.look_panel = panel
        self._tab_main(tabs["main"])
        self._tab_keys(tabs["keys"])
        self._tab_look(tabs["look"])
        self._tab_dict(tabs["dict"])

    def copy_log(self):
        report = diagnostic_report()
        try:
            pyperclip.copy(report)
        except Exception as e:
            messagebox.showinfo(APP_NAME, f"Не вдалося скопіювати: {e}")
            return
        messagebox.showinfo(APP_NAME, f"Журнал скопійовано в буфер обміну ({report.count(chr(10)) + 1} "
                                      "рядків). Встав його в чат (Ctrl+V).")

    def open_log(self):
        if not os.path.exists(log_path()):
            messagebox.showinfo(APP_NAME, "Журнал ще порожній.")
            return
        open_file(log_path())

    def _tab_main(self, f):
        top = ttk.Frame(f)
        top.pack(fill="x")
        self.status_var = tk.StringVar()
        ttk.Label(top, textvariable=self.status_var, font=(FONT, 11, "bold")).pack(side="left")
        ttk.Button(top, text="Пауза / Продовжити", command=self.toggle_pause).pack(side="right")

        self.hint_var = tk.StringVar()
        ttk.Label(f, textvariable=self.hint_var, wraplength=560, justify="left").pack(
            anchor="w", pady=(12, 0))

        ocr = ttk.LabelFrame(f, text="Розпізнавання тексту (OCR)", padding=10)
        ocr.pack(fill="x", pady=(12, 0))
        self.ocr_var = tk.StringVar()
        ttk.Label(ocr, textvariable=self.ocr_var, wraplength=540, justify="left").pack(anchor="w")
        brow = ttk.Frame(ocr)
        brow.pack(anchor="w", pady=(6, 0))
        ttk.Button(brow, text="Перевірити ще раз", command=self._check_ocr).pack(side="left")
        ttk.Button(brow, text="Діагностика OCR (3 с)", command=self.diagnose_ocr
                   ).pack(side="left", padx=(8, 0))
        ttk.Label(ocr, text="Діагностика: натисни, наведи курсор на текст — за 3 секунди буде "
                            "знімок і звіт, що саме бачить OCR.", foreground="#666",
                  wraplength=540).pack(anchor="w", pady=(4, 0))

        lg = ttk.LabelFrame(f, text="Журнал (щоб розібратись, що пішло не так)", padding=10)
        lg.pack(fill="x", pady=(12, 0))
        lrow = ttk.Frame(lg)
        lrow.pack(anchor="w")
        ttk.Button(lrow, text="📋 Скопіювати журнал", command=self.copy_log).pack(side="left")
        ttk.Button(lrow, text="Відкрити файл журналу", command=self.open_log
                   ).pack(side="left", padx=(8, 0))
        ttk.Label(lg, text="Після збою натисни «Скопіювати» і встав у чат: там події пошуку, помилки й "
                           "налаштування (ключ DeepL приховано). Файл обмежений ~600 КБ.",
                  foreground="#666", wraplength=540).pack(anchor="w", pady=(4, 0))

        dic = ttk.LabelFrame(f, text="Переклад і словник (English – визначення, синоніми)",
                             padding=10)
        dic.pack(fill="x", pady=(12, 0))
        self.dict_var = tk.StringVar(value="Ще не перевірялось.")
        ttk.Label(dic, textvariable=self.dict_var, wraplength=540, justify="left").pack(anchor="w")
        ttk.Button(dic, text="Діагностика словника", command=self.diagnose_dict).pack(
            anchor="w", pady=(6, 0))
        ttk.Label(dic, text="Якщо в грі показується лише переклад без визначень і синонімів "
                            "(секцій English / English Thesaurus), ця перевірка покаже, чи "
                            "доступний dictionaryapi.dev з твоєї мережі.", foreground="#666",
                  wraplength=540).pack(anchor="w", pady=(4, 0))

        test = ttk.LabelFrame(f, text="Спробувати", padding=10)
        test.pack(fill="x", pady=(12, 0), before=ocr)
        self.test_var = tk.StringVar(value="understatement")
        self.test_entry = ttk.Entry(test, textvariable=self.test_var)
        self.test_entry.pack(side="left", fill="x", expand=True)
        self.test_entry.bind("<Return>", lambda _e: self.test_lookup())
        ttk.Button(test, text="Знайти", command=self.test_lookup).pack(side="left", padx=(8, 0))
        self.preview_btn = ttk.Button(test, text="Показати приклад", command=self.show_preview)
        self.preview_btn.pack(side="left", padx=(8, 0))

        ttk.Button(f, text="✂  Виділити область на екрані (як скріншот)",
                   command=self.start_region_select).pack(anchor="w", pady=(12, 0))
        ttk.Label(f, text="Екран одразу заморожується на місці — тому підказка (тултіп), яка "
                        "в грі зникає при русі миші, залишиться на скріншоті, поки ти спокійно "
                        "виділяєш потрібне слово.", foreground="#666", wraplength=520
                  ).pack(anchor="w", pady=(2, 0))
        ttk.Checkbutton(f, text="Підсвічувати слово під курсором на застиглому екрані (клік — шукати)",
                        variable=self._var(tk.BooleanVar, ("freeze", "hover_highlight"))
                        ).pack(anchor="w", pady=(6, 0))
        pause_box = ttk.LabelFrame(f, text="Пауза гри під час вибору й перекладу (Windows)", padding=10)
        pause_box.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(pause_box, text="Призупиняти гру на час вибору й перекладу",
                        variable=self._var(tk.BooleanVar, ("freeze", "pause_game"),
                                           after=self._pause_option_changed)).pack(anchor="w")
        ttk.Checkbutton(pause_box, text="Автоматично визначати активну гру",
                        variable=self._var(tk.BooleanVar, ("freeze", "auto_game"),
                                           after=self._game_mode_changed)).pack(anchor="w", pady=(4, 0))
        process_row = ttk.Frame(pause_box)
        process_row.pack(fill="x", pady=6)
        ttk.Label(process_row, text="Вручну (.exe):").pack(side="left")
        self.game_exe_entry = ttk.Entry(process_row,
            textvariable=self._var(tk.StringVar, ("freeze", "game_exe")), width=28)
        self.game_exe_entry.pack(side="left", padx=6)
        self.game_detection_var = tk.StringVar(value="Гра визначається при натисканні гарячої клавіші у її вікні.")
        ttk.Label(pause_box, textvariable=self.game_detection_var, wraplength=520).pack(anchor="w", pady=4)
        ttk.Button(pause_box, text="Перевірити визначення через 3 с",
                   command=self.schedule_game_detection).pack(anchor="w", pady=(0, 6))
        self._game_mode_changed()
        limit_row = ttk.Frame(pause_box)
        limit_row.pack(fill="x")
        ttk.Label(limit_row, text="Відновити автоматично через, с:").pack(side="left")
        ttk.Spinbox(limit_row, from_=5, to=300, width=6,
                    textvariable=self._var(tk.IntVar, ("freeze", "pause_seconds"))).pack(side="left", padx=6)
        ttk.Button(pause_box, text="Відновити гру зараз",
                   command=lambda: self.do_action("resume_game")).pack(anchor="w", pady=(6, 0))
        ttk.Label(pause_box, text="Повернись у гру й натисни Ctrl+Alt+R або Ctrl+Alt+G. "
                                  "Гра лишається на паузі до закриття перекладу / Esc. "
                                  "Лише для однокористувацьких ігор; сервер онлайн-гри не зупиняється.",
                  wraplength=520, foreground="#666").pack(anchor="w", pady=(6, 0))

        fc = ttk.LabelFrame(f, text="Гра з захопленою мишею (наприклад, Cyberpunk 2077)", padding=10)
        fc.pack(fill="x", pady=(10, 0))
        ttk.Button(fc, text="🎯  Керований курсор (миша + клавіатура)",
                   command=self.start_free_cursor).pack(anchor="w")
        ttk.Label(fc, text="Якщо миша прив'язана до камери в грі й наведення курсора не працює: "
                          "натисни цю кнопку (або гарячу клавішу) — з'явиться приціл, який можна "
                          "рухати фізичною мишею або стрілками/WASD (Shift — швидше), Enter/Пробіл — "
                          "знайти слово, Esc — скасувати. Працює незалежно від того, чи гра тримає "
                          "фокус вікна, і незалежно від системного курсора миші.",
                  foreground="#666", wraplength=520
                  ).pack(anchor="w", pady=(4, 0))

        self.saved_btn = ttk.Button(f, command=self.open_saved_words)
        self.saved_btn.pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(f, text="Зберігати скриншот контексту зі словом (локально, для повторення)",
                        variable=self._var(tk.BooleanVar, ("learning", "capture_context"))
                        ).pack(anchor="w", pady=(6, 0))

        cache = ttk.LabelFrame(f, text="Кеш на диску", padding=10)
        cache.pack(fill="x", pady=(12, 0))
        self.cache_var = tk.StringVar()
        ttk.Label(cache, textvariable=self.cache_var).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(cache, text="Зберігати до").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(cache, from_=0, to=5000, increment=100, width=6,
                    textvariable=self._var(tk.IntVar, ("cache", "max_entries"))
                    ).grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(cache, text="слів (0 — не кешувати)").grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Button(cache, text="Очистити кеш зараз", command=self.clear_cache
                   ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(cache, text="Кожне вже знайдене слово зберігається на диску, щоб наступного "
                             "разу відкриватись миттєво й без інтернету. Розмір обмежений і "
                             "зазвичай не перевищує кілька мегабайтів.", foreground="#666",
                  wraplength=520).grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        ttk.Checkbutton(f, text="Запускати згорнутою",
                        variable=self._var(tk.BooleanVar, ("app", "start_minimized"))
                        ).pack(anchor="w", pady=(12, 0))
        bottom = ttk.Frame(f)
        bottom.pack(fill="x", side="bottom", pady=(14, 0))
        ttk.Button(bottom, text="Згорнути", command=self.hide_window).pack(side="left")
        ttk.Button(bottom, text="Вийти з програми", command=self.quit).pack(side="right")

    def _tab_keys(self, f):
        abox = ttk.LabelFrame(f, text="Автоматичний словник при виділенні тексту", padding=10)
        abox.pack(fill="x")
        ttk.Checkbutton(abox, text="Показувати переклад/словник одразу, щойно виділено текст "
                                   "(без гарячої клавіші)",
                        variable=self._var(tk.BooleanVar, ("auto", "enabled"),
                                           after=self._refresh_hint)
                        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(abox, text="Виділи слово чи фразу мишею (перетягуванням) або клацни двічі по "
                            "слову — і картка з'явиться сама. Працює там, де текст можна "
                            "виділити насправді: у браузері, PDF, Word тощо. Для гри, де текст "
                            "не виділити, лишається пошук по наведенню курсора вище.",
                  foreground="#666", wraplength=540).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        mbox = ttk.LabelFrame(f, text="Кнопка миші для пошуку слова (наведення, як в іграх)", padding=10)
        mbox.pack(fill="x", pady=(12, 0))
        ttk.Label(mbox, text="Кнопка:").grid(row=0, column=0, sticky="w")
        self._combo(mbox, self._var(tk.StringVar, ("mouse", "button"), MOUSE_BTN_NAMES,
                                    self._refresh_hint), MOUSE_BTN_NAMES
                    ).grid(row=0, column=1, padx=8, pady=3, sticky="w")
        ttk.Label(mbox, text="Разом із клавішею:").grid(row=1, column=0, sticky="w")
        self._combo(mbox, self._var(tk.StringVar, ("mouse", "modifier"), MOUSE_MOD_NAMES,
                                    self._refresh_hint), MOUSE_MOD_NAMES
                    ).grid(row=1, column=1, padx=8, pady=3, sticky="w")
        ttk.Label(mbox, text="Клік передається і грі/програмі — вибери кнопку, яка там не потрібна.",
                  foreground="#666").grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        kbox = ttk.LabelFrame(f, text="Гарячі клавіші", padding=10)
        kbox.pack(fill="x", pady=(12, 0))
        ttk.Label(kbox, text="Натисни «Записати», потім комбінацію. Esc — скасувати.",
                  foreground="#666").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        self.key_vars, self.key_btns = {}, {}
        for i, (action, label) in enumerate(ACTIONS, start=1):
            ttk.Label(kbox, text=label).grid(row=i, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=pretty(cfg["hotkeys"][action]))
            self.key_vars[action] = v
            ttk.Label(kbox, textvariable=v, width=20, anchor="center",
                      relief="sunken").grid(row=i, column=1, padx=8)
            b = ttk.Button(kbox, text="Записати", command=lambda a=action: self.start_record(a))
            b.grid(row=i, column=2)
            self.key_btns[action] = b
            ttk.Button(kbox, text="Очистити",
                       command=lambda a=action: self.set_hotkey(a, "")).grid(row=i, column=3, padx=(6, 0))
        ttk.Button(f, text="Скинути гарячі клавіші до стандартних",
                   command=self.reset_hotkeys).pack(anchor="w", pady=(12, 0))

    def _tab_look(self, f):
        after = self._refresh_preview
        ttk.Label(f, text="Збери зручну картку", font=(FONT, 13, "bold")).pack(anchor="w")
        ttk.Label(f, text="Зміни одразу видно у відкритій картці. Власні переклади залишаються збереженими.",
                  foreground="#61728b", wraplength=570).pack(anchor="w", pady=(4, 10))
        presets = ttk.Frame(f)
        presets.pack(fill="x", pady=(0, 12))
        for key, label in (("compact", "Компактна"), ("balanced", "Збалансована"),
                           ("detailed", "Докладна")):
            ttk.Button(presets, text=label, command=lambda k=key: self.apply_card_preset(k)
                       ).pack(side="left", padx=(0, 6))
        g = ttk.LabelFrame(f, text="Розмір і оформлення", padding=10)
        g.pack(fill="x")
        rows = [
            ("Тема", lambda p: self._combo(p, self._var(tk.StringVar, ("popup", "theme"),
                                                        THEME_NAMES, after), THEME_NAMES, 18)),
            ("Розмір шрифту", lambda p: ttk.Spinbox(p, from_=9, to=24, width=6,
                                                   textvariable=self._var(tk.IntVar, ("popup", "font_size"), after=after))),
            ("Ширина картки (px)", lambda p: ttk.Spinbox(p, from_=300, to=900, increment=20, width=6,
                                                         textvariable=self._var(tk.IntVar, ("popup", "width"), after=after))),
            ("Прозорість", lambda p: ttk.Scale(p, from_=0.5, to=1.0, length=160,
                                               variable=self._var(tk.DoubleVar, ("popup", "opacity"), after=after))),
            ("Закривати через (с, 0 — ні)", lambda p: ttk.Spinbox(p, from_=0, to=300, width=6,
                                                                  textvariable=self._var(tk.IntVar, ("popup", "timeout")))),
        ]
        for i, (text, make) in enumerate(rows):
            ttk.Label(g, text=text).grid(row=i, column=0, sticky="w", pady=4)
            make(g).grid(row=i, column=1, sticky="w", padx=10)
        r = len(rows)
        ttk.Label(g, text="Колір підсвічування").grid(row=r, column=0, sticky="w", pady=4)
        self.color_btn = tk.Button(g, width=8, bg=cfg["popup"]["highlight_color"],
                                   command=self.pick_color)
        self.color_btn.grid(row=r, column=1, sticky="w", padx=10)
        content = ttk.LabelFrame(f, text="Що показувати в картці", padding=10)
        content.pack(fill="x", pady=(12, 0))
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        for i, (path, label) in enumerate(CARD_CONTENT_OPTIONS):
            ttk.Checkbutton(content, text=label,
                            variable=self._var(tk.BooleanVar, path, after=after)
                            ).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 10), pady=4)
        ttk.Label(content, text="Приклади й переклад визначення видно, коли увімкнені визначення.",
                  foreground="#61728b", wraplength=540).grid(
                      row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(f, text="Підсвічувати знайдене слово на екрані",
                        variable=self._var(tk.BooleanVar, ("ocr", "highlight"))
                        ).pack(anchor="w", pady=(10, 0))
        ttk.Button(f, text="Показати приклад картки", style="Accent.TButton",
                   command=self.show_preview).pack(
            anchor="w", pady=(14, 0))

    def open_card_settings(self):
        self.show_window()
        self.notebook.select(self.look_panel)

    def apply_card_preset(self, name):
        if name not in ("compact", "balanced", "detailed"):
            return
        compact = name == "compact"
        for path, _label in CARD_CONTENT_OPTIONS:
            visible = not compact or path not in {
                ("sections", "definitions"), ("sections", "thesaurus"),
                ("popup", "alternate_translations"), ("popup", "context_image")}
            self._var(tk.BooleanVar, path, after=self._refresh_preview).set(visible)
        self._var(tk.IntVar, ("popup", "font_size"), after=self._refresh_preview).set(
            11 if name == "detailed" else 10)
        self._var(tk.IntVar, ("popup", "width"), after=self._refresh_preview).set(
            {"compact": 360, "balanced": 400, "detailed": 460}[name])

    def _tab_dict(self, f):
        box = ttk.LabelFrame(f, text="Словник", padding=10)
        box.pack(fill="x")
        ttk.Label(box, text="Переклад на:").grid(row=0, column=0, sticky="w")
        self._combo(box, self._var(tk.StringVar, ("languages", "target"), LANG_NAMES,
                                   _card_cache.clear), LANG_NAMES, 18
                    ).grid(row=0, column=1, sticky="w", padx=10, pady=3)
        ttk.Button(box, text="Налаштувати вміст картки…", command=self.open_card_settings
                   ).grid(row=1, column=0, columnspan=2, sticky="w", pady=6)
        for i, (key, text) in enumerate((("max_translations", "Варіантів перекладу на частину мови"),
                                         ("max_definitions", "Визначень у згорнутому вигляді"),
                                         ("max_synonyms", "Синонімів у згорнутому вигляді")), start=2):
            ttk.Label(box, text=text).grid(row=i, column=0, sticky="w", pady=3)
            ttk.Spinbox(box, from_=1, to=20, width=5,
                        textvariable=self._var(tk.IntVar, ("dictionary", key),
                                               after=_card_cache.clear)
                        ).grid(row=i, column=1, sticky="w", padx=10)

        ob = ttk.LabelFrame(f, text="OCR (розпізнавання тексту на екрані)", padding=10)
        ob.pack(fill="x", pady=(12, 0))
        ttk.Label(ob, text="Рушій:").grid(row=0, column=0, sticky="w")
        self._combo(ob, self._var(tk.StringVar, ("ocr", "engine"), ENGINE_NAMES, self._check_ocr),
                    ENGINE_NAMES, 14).grid(row=0, column=1, sticky="w", padx=10, pady=3)
        ttk.Label(ob, text="Шлях до tesseract:").grid(row=1, column=0, sticky="w")
        row = ttk.Frame(ob)
        row.grid(row=1, column=1, sticky="w", padx=10)
        ttk.Entry(row, width=34, textvariable=self._var(tk.StringVar, ("ocr", "tesseract_cmd"))
                  ).pack(side="left")
        ttk.Button(row, text="…", width=3, command=self.browse_tesseract).pack(side="left", padx=4)
        for i, (key, text, lo, hi, inc) in enumerate((
                ("region_w", "Ширина області пошуку слова (px)", 200, 2000, 50),
                ("region_h", "Висота області пошуку слова (px)", 40, 600, 10)), start=2):
            ttk.Label(ob, text=text).grid(row=i, column=0, sticky="w", pady=3)
            ttk.Spinbox(ob, from_=lo, to=hi, increment=inc, width=7,
                        textvariable=self._var(tk.IntVar, ("ocr", key))
                        ).grid(row=i, column=1, sticky="w", padx=10)
        ttk.Label(ob, text="Збільшення знімка (1–4, лише Tesseract)").grid(row=4, column=0, sticky="w", pady=3)
        ttk.Spinbox(ob, from_=1.0, to=4.0, increment=0.5, width=7,
                    textvariable=self._var(tk.DoubleVar, ("ocr", "upscale"))
                    ).grid(row=4, column=1, sticky="w", padx=10)
        ttk.Label(ob, text="RapidOCR масштабує знімок сам; збільшення потрібне лише для Tesseract.",
                  foreground="#666").grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(ob, text="Розпізнавати злиті слова без пробілу (напр. «Nexttime» → «Next»)",
                        variable=self._var(tk.BooleanVar, ("ocr", "split_merged_words"))
                        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(ob, text="Іноді OCR губить пробіл між двома сусідніми словами. Ця перевірка "
                          "шукає видиму прогалину на зображенні й уточнює лише те слово, де "
                          "курсор. Вимкни, якщо вона починає помилятись на звичайних словах.",
                  foreground="#666", wraplength=520).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(2, 0))

        dl = ttk.LabelFrame(f, text="DeepL — переклад з урахуванням контексту (необов'язково)",
                            padding=10)
        dl.pack(fill="x", pady=(12, 0))
        ttk.Checkbutton(dl, text="Використовувати DeepL, коли ключ вказано",
                        variable=self._var(tk.BooleanVar, ("deepl", "enabled"),
                                           after=self._on_deepl_changed)
                        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(dl, text="API-ключ:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.deepl_entry = ttk.Entry(dl, width=42, show="•",
                                     textvariable=self._var(tk.StringVar, ("deepl", "key"),
                                                            after=self._on_deepl_changed))
        self.deepl_entry.grid(row=1, column=1, sticky="w", padx=8, pady=(6, 0))
        show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(dl, text="показати", variable=show_var,
                        command=lambda: self.deepl_entry.config(show="" if show_var.get() else "•")
                        ).grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Button(dl, text="Перевірити ключ", command=self.check_deepl
                   ).grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.deepl_var = tk.StringVar(value="")
        ttk.Label(dl, textvariable=self.deepl_var, wraplength=380, justify="left"
                  ).grid(row=2, column=1, columnspan=2, sticky="w", padx=8, pady=(8, 0))
        ttk.Label(dl, text="DeepL часто краще розуміє контекст: при пошуку слова по OCR "
                          "програма передає йому весь рядок навколо слова, тож «chest» у "
                          "Minecraft має перекладатися як «сундук». Потрібен безкоштовний ключ "
                          "«DeepL API Free» (deepl.com → API; до 500 000 символів на місяць, "
                          "при реєстрації можуть попросити картку для верифікації). Текст "
                          "надсилається на сервери DeepL; ключ зберігається у файлі налаштувань "
                          "відкритим текстом. Якщо DeepL недоступний або ліміт вичерпано, "
                          "автоматично працюють попередні джерела.",
                  foreground="#666", wraplength=520, justify="left"
                  ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        ak = ttk.LabelFrame(f, text="Anki — тег гри для карток", padding=10)
        ak.pack(fill="x", pady=(12, 0))
        ttk.Label(ak, text="Назва гри:").grid(row=0, column=0, sticky="w")
        ttk.Entry(ak, width=28, textvariable=self._var(tk.StringVar, ("anki", "game"))
                  ).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Checkbutton(ak, text="Якщо порожньо — брати назву з заголовка вікна гри (лише Windows)",
                        variable=self._var(tk.BooleanVar, ("anki", "auto_game_tag"))
                        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(ak, text="Тег додається до карток .apkg: в Anki за ним можна відфільтрувати "
                          "слова з конкретної гри. Назва, вписана тут, ставиться всім "
                          "експортованим карткам. На картці також буде блок «From the game» "
                          "з реченням, у якому ти знайшов слово.",
                  foreground="#666", wraplength=520, justify="left"
                  ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _on_deepl_changed(self):
        _cooldown.pop("DeepL", None)
        if hasattr(self, "deepl_var"):
            self.deepl_var.set("")

    def check_deepl(self):
        self.deepl_var.set("Перевіряю…")

        def work():
            try:
                used, limit = deepl_usage()
                txt = (f"✓ Ключ працює. Використано {used:,} з {limit:,} символів за місяць"
                       ).replace(",", " ")
            except DeeplError as e:
                txt = f"✗ {e}"
            except Exception as e:
                txt = f"✗ Не вдалося перевірити: {type(e).__name__}: {e}"
            events.put(("deepl_status", txt))
        threading.Thread(target=work, daemon=True).start()

    # ---------- статус, підказки ----------
    def _refresh_status(self):
        self.status_var.set("⏸ На паузі" if self.paused else "● Працює")

    def _refresh_hint(self):
        parts = []
        if cfg["auto"]["enabled"]:
            parts.append("Виділи текст мишею (або двічі клацни по слову) — картка з'явиться сама.")
        btn = cfg["mouse"]["button"]
        if btn != "none":
            names = {v: k for k, v in MOUSE_BTN_NAMES.items()}
            mod = cfg["mouse"]["modifier"]
            mod_txt = "" if mod == "none" else f"{mod.capitalize()} + "
            parts.append(f"Наведи курсор на слово і натисни: {mod_txt}{names[btn]}.")
        hk = cfg["hotkeys"]["lookup_word"]
        if hk:
            parts.append(f"Або гаряча клавіша: {pretty(hk)}.")
        tl = cfg["hotkeys"]["translate_line"]
        if tl:
            parts.append(f"Переклад усього рядка: {pretty(tl)}.")
        sr = cfg["hotkeys"]["select_region"]
        if sr:
            parts.append(f"Виділити рамкою слово чи рядок (як скріншот): {pretty(sr)}.")
        if not parts:
            parts.append("Кнопки пошуку не задано — налаштуй їх на вкладці «Кнопки».")
        self.hint_var.set(" ".join(parts))

    def _refresh_cache_label(self):
        kb = cache_disk_kb()
        size = f"{kb:.0f} КБ" if kb < 1024 else f"{kb / 1024:.1f} МБ"
        self.cache_var.set(f"На диску зараз: {size}  ·  слів у кеші: {len(_card_cache)}  ·  "
                           f"файл: {_cache_path()}")

    def clear_cache(self):
        clear_card_cache()
        self._refresh_cache_label()
        self.toast("Кеш очищено")

    # ---------- збережені слова ----------
    def refresh_saved_count(self):
        n = len(load_saved_words())
        if hasattr(self, "saved_btn"):
            self.saved_btn.config(text=f"⭐ Збережені слова ({n})")
        if getattr(self, "saved_win", None) is not None:
            self._render_saved_list()

    def open_saved_words(self):
        if getattr(self, "saved_win", None) is not None:
            self.saved_win.deiconify()
            self.saved_win.lift()
            self._render_saved_list()
            return
        win = self.saved_win = tk.Toplevel(self.root)
        win.title("Бібліотека — Word Lookup")
        win.geometry("1100x700")
        win.minsize(900, 480)
        win.protocol("WM_DELETE_WINDOW", self._hide_saved_window)
        self._editor_word = None
        self._editor_dirty = False
        self._loading_editor = False
        self.saved_query = tk.StringVar()
        self.saved_order = tk.StringVar(value="Спочатку нові")
        self.saved_state_filter = tk.StringVar(value="Усі")
        self.saved_due_only = tk.BooleanVar(value=False)
        search_row = ttk.Frame(win, padding=8)
        search_row.pack(fill="x")
        ttk.Label(search_row, text="Пошук:").pack(side="left")
        search = ttk.Entry(search_row, textvariable=self.saved_query)
        search.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Combobox(search_row, state="readonly", width=16, textvariable=self.saved_order,
                     values=["Спочатку нові", "Спочатку старі", "За абеткою"]).pack(side="left", padx=4)
        ttk.Combobox(search_row, state="readonly", width=10, textvariable=self.saved_state_filter,
                     values=list(STATUS_LABELS)).pack(side="left", padx=4)
        ttk.Checkbutton(search_row, text="Час повторити", variable=self.saved_due_only).pack(side="left")
        for var in (self.saved_query, self.saved_order, self.saved_state_filter, self.saved_due_only):
            var.trace_add("write", lambda *_: self._render_saved_list())
        win.bind("<Control-f>", lambda _e: search.focus_set())
        win.bind("<Escape>", lambda _e: self._hide_saved_window())
        win.bind("<Control-s>", lambda _e: self.save_saved_details())
        search.focus_set()
        controls = ttk.Frame(win, padding=(8, 0, 8, 8))
        controls.pack(fill="x")
        ttk.Button(controls, text="Вибрати знайдені", command=lambda: self.select_all_saved(True)).pack(side="left")
        ttk.Button(controls, text="Зняти все", command=lambda: self.select_all_saved(False)).pack(side="left", padx=4)
        ttk.Button(controls, text="Повторити слова", command=self.review_saved_words).pack(side="left", padx=4)
        ttk.Button(controls, text="Anki .txt", command=self.export_anki_selected).pack(side="left", padx=4)
        ttk.Button(controls, text="Anki .apkg", command=self.export_anki_apkg_selected).pack(side="left", padx=4)
        self.saved_selected_var = tk.StringVar()
        ttk.Label(controls, textvariable=self.saved_selected_var).pack(side="right")
        anki_row = ttk.Frame(win, padding=(8, 0, 8, 8))
        anki_row.pack(fill="x")
        ttk.Label(anki_row, text="Колода:").pack(side="left")
        ttk.Entry(anki_row, width=25, textvariable=self._var(tk.StringVar, ("anki", "deck"))).pack(side="left", padx=6)
        self.anki_sync_btn = ttk.Button(anki_row, text="Передати в Anki", command=self.sync_saved_to_anki)
        self.anki_sync_btn.pack(side="left")
        self.anki_sync_status = tk.StringVar(value="Потрібні запущена Anki та AnkiConnect на цьому ПК.")
        ttk.Label(anki_row, textvariable=self.anki_sync_status, wraplength=570).pack(side="left", padx=8)
        panes = ttk.Panedwindow(win, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        left = ttk.Frame(panes)
        panes.add(left, weight=2)
        self.saved_tree = ttk.Treeview(left, columns=("check", "word", "translation", "state"),
                                       show="headings", selectmode="browse")
        for col, text, width in (("check", "✓", 30), ("word", "Слово / фраза", 130),
                                  ("translation", "Переклад", 150), ("state", "Стан", 70)):
            self.saved_tree.heading(col, text=text)
            self.saved_tree.column(col, width=width, minwidth=30, stretch=col != "check")
        bar = ttk.Scrollbar(left, orient="vertical", command=self.saved_tree.yview)
        self.saved_tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.saved_tree.pack(fill="both", expand=True)
        self.saved_tree.bind("<Button-1>", self._saved_tree_click)
        self.saved_tree.bind("<<TreeviewSelect>>", self._saved_tree_select)
        self.saved_tree.bind("<Double-1>", lambda _e: self._open_selected_popup())
        self.saved_tree.bind("<Return>", lambda _e: self._open_selected_popup())
        detail = ScrolledPanel(panes, bg=self.root.cget("background"))
        panes.add(detail, weight=3)
        self.saved_list_frame = detail.content
        self._saved_iids = {}
        self._render_saved_list()

    def _hide_saved_window(self):
        if self._commit_or_discard_editor():
            self.saved_win.withdraw()

    def _commit_or_discard_editor(self):
        if not getattr(self, "_editor_dirty", False):
            return True
        answer = messagebox.askyesnocancel(APP_NAME, "Зберегти зміни до картки?", parent=self.saved_win)
        if answer is None:
            return False
        if answer:
            return self.save_saved_details()
        self._editor_dirty = False
        return True

    def _saved_tree_click(self, event):
        iid = self.saved_tree.identify_row(event.y)
        if iid and self.saved_tree.identify_column(event.x) == "#1":
            word = self._saved_iids[iid]
            var = self._saved_check_var(word)
            var.set(not var.get())
            self._update_selected_count()
            return "break"

    def _saved_tree_select(self, _event=None):
        selection = self.saved_tree.selection()
        if selection:
            word = self._saved_iids.get(selection[0])
            if word and word != self._editor_word:
                if self._commit_or_discard_editor():
                    self.show_saved_details(word)
                elif self._editor_word:
                    self.saved_tree.selection_set(self._saved_tree_id(self._editor_word))

    def _open_selected_popup(self):
        selection = self.saved_tree.selection()
        if selection:
            self.open_saved_word(self._saved_iids[selection[0]])

    @staticmethod
    def _saved_tree_id(word):
        return hashlib.sha256(word.lower().encode()).hexdigest()

    def show_saved_details(self, word):
        entry = saved_entry(word)
        if not entry:
            return
        self._loading_editor = True
        self._editor_word = word
        self._editor_dirty = False
        for child in self.saved_list_frame.winfo_children():
            child.destroy()
        frame = self.saved_list_frame
        ttk.Label(frame, text=word, font=(FONT, 19, "bold"), wraplength=500).pack(anchor="w", padx=12, pady=8)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", padx=12)
        ttk.Button(buttons, text="Зберегти (Ctrl+S)", command=self.save_saved_details).pack(side="left")
        ttk.Button(buttons, text="Озвучити", command=lambda: self.speak_saved_word(word)).pack(side="left", padx=6)
        ttk.Button(buttons, text="Видалити", command=lambda: self.delete_saved_word(word)).pack(side="right")
        self.editor_message = tk.StringVar(value="Зміни зберігаються кнопкою або Ctrl+S.")
        ttk.Label(frame, textvariable=self.editor_message, wraplength=500).pack(anchor="w", padx=12, pady=6)
        row = ttk.Frame(frame)
        row.pack(fill="x", padx=12, pady=6)
        ttk.Label(row, text="Стан:").pack(side="left")
        states = {k: v for k, v in STATUS_LABELS.items() if v != "all"}
        inverse = {v: k for k, v in states.items()}
        self.editor_status = tk.StringVar(value=inverse.get(entry.get("status", "new"), "Нові"))
        ttk.Combobox(row, textvariable=self.editor_status, values=list(states), width=12,
                     state="readonly").pack(side="left", padx=6)
        self.editor_status.trace_add("write", self._mark_editor_dirty)
        self.editor_fields = {}
        labels = (("translation", "Переклад"), ("definition", "Визначення англійською"),
                  ("definition_tr", "Переклад визначення"), ("example", "Приклад"),
                  ("context", "Речення з гри / контекст"), ("context_tr", "Переклад контексту"),
                  ("source", "Джерело / гра"), ("notes", "Власні нотатки"))
        for key, label in labels:
            ttk.Label(frame, text=label).pack(anchor="w", padx=12, pady=(8, 2))
            text = tk.Text(frame, height=2, width=48, wrap="word", font=(FONT, 11), undo=True)
            text.pack(fill="x", padx=12)
            text.insert("1.0", entry.get(key, ""))
            text.edit_modified(False)
            text.bind("<<Modified>>", self._mark_editor_dirty)
            self.editor_fields[key] = text
        image_buttons = ttk.Frame(frame)
        image_buttons.pack(fill="x", padx=12, pady=10)
        ttk.Button(image_buttons, text="Додати зображення…", command=self.replace_saved_image).pack(side="left")
        ttk.Button(image_buttons, text="Відкрити зображення", command=self.open_saved_image).pack(side="left", padx=6)
        self.editor_photo = context_photo(entry.get("image_file", ""), self.saved_win, size=(480, 250))
        if self.editor_photo:
            label = ttk.Label(frame, image=self.editor_photo, cursor="hand2")
            label.pack(anchor="w", padx=12, pady=8)
            label.bind("<Button-1>", lambda _e: self.open_saved_image())
        self._loading_editor = False
        self._editor_dirty = False
        iid = self._saved_tree_id(word)
        if self.saved_tree.exists(iid):
            self.saved_tree.selection_set(iid)

    def _mark_editor_dirty(self, event=None, *_):
        # Text генерує <<Modified>> і при скиданні прапорця: обробляємо лише зміни.
        if event is not None and hasattr(event, "widget"):
            if not event.widget.edit_modified():
                return
            event.widget.edit_modified(False)
        if not self._loading_editor:
            self._editor_dirty = True
            self.editor_message.set("Є незбережені зміни.")

    def save_saved_details(self):
        if not self._editor_word or not hasattr(self, "editor_fields"):
            return True
        fields = {k: text.get("1.0", "end-1c") for k, text in self.editor_fields.items()}
        fields["status"] = STATUS_LABELS[self.editor_status.get()]
        try:
            update_saved_entry(self._editor_word, fields)
        except (OSError, ValueError) as e:
            self.editor_message.set(str(e))
            return False
        self._editor_dirty = False
        self.editor_message.set("Збережено")
        self._render_saved_list()
        return True

    def replace_saved_image(self):
        if not self._commit_or_discard_editor():
            return
        path = filedialog.askopenfilename(parent=self.saved_win, title="Зображення контексту",
                                          filetypes=[("Зображення", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if not path:
            return
        name = ""
        try:
            from PIL import Image
            with Image.open(path) as image:
                name = save_context_image(image)
            if not name:
                raise OSError("Не вдалося записати зображення")
            entry = saved_entry(self._editor_word)
            old = context_image_path(entry.get("image_file", ""))
            update_saved_entry(self._editor_word, {"image_file": name})
            if old and not any(context_image_path(e.get("image_file", "")) == old for e in load_saved_words()):
                try:
                    os.remove(old)
                except OSError:
                    pass
            self.show_saved_details(self._editor_word)
        except (OSError, ValueError) as e:
            if name and not any(e.get("image_file") == name for e in load_saved_words()):
                try:
                    os.remove(context_image_path(name))
                except OSError:
                    pass
            self.editor_message.set(f"Зображення не додано: {e}")

    def open_saved_image(self):
        entry = saved_entry(self._editor_word) if self._editor_word else None
        path = context_image_path(entry.get("image_file", "")) if entry else None
        if path and os.path.isfile(path):
            open_file(path)
        elif hasattr(self, "editor_message"):
            self.editor_message.set("У цієї картки ще немає зображення.")

    def speak_saved_word(self, word):
        entry = saved_entry(word)
        audios = entry.get("dictionary", {}).get("audios", []) if entry else []
        if audios and AUDIO_OK:
            play_audio(audios[0][1])
        else:
            self.editor_message.set("Озвучення відсутнє або pygame не встановлений.")

    def sync_saved_to_anki(self):
        if not self._commit_or_discard_editor():
            return
        entries = [e for e in load_saved_words() if self._saved_check_var(e["word"]).get()]
        entries = copy.deepcopy(entries or self._visible_saved_words())
        if not entries:
            self.anki_sync_status.set("Немає слів для передавання")
            return
        deck = cfg["anki"]["deck"]
        self.anki_sync_btn.state(["disabled"])
        self.anki_sync_status.set(f"Передаю {len(entries)} записів у Anki…")
        def worker():
            try:
                events.put(("anki_sync_done", sync_anki_entries(entries, deck), ""))
            except Exception as e:
                events.put(("anki_sync_done", None, str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def _saved_check_var(self, word: str):
        key = word.lower()
        var = self.saved_checks.get(key)
        if var is None:
            var = tk.BooleanVar(value=False)
            self.saved_checks[key] = var
        return var

    def _update_selected_count(self):
        n = sum(1 for e in load_saved_words() if self._saved_check_var(e["word"]).get())
        if hasattr(self, "saved_selected_var"):
            shown = len(self._visible_saved_words())
            self.saved_selected_var.set(f"Знайдено: {shown} · Обрано: {n}")
        if hasattr(self, "saved_tree"):
            for iid, word in self._saved_iids.items():
                self.saved_tree.set(iid, "check", "☑" if self._saved_check_var(word).get() else "☐")

    def review_saved_words(self):
        if not self._commit_or_discard_editor():
            return
        selected = [e for e in load_saved_words() if self._saved_check_var(e["word"]).get()]
        entries = selected or self._visible_saved_words()
        if not entries:
            self.toast("Спочатку збережи слова або зміни запит пошуку")
            return
        self.review_window = ReviewWindow(self, entries)

    def _visible_saved_words(self):
        order = {"Спочатку нові": "newest", "Спочатку старі": "oldest", "За абеткою": "az"}
        entries = filter_saved_words(load_saved_words(), self.saved_query.get(),
                                      order.get(self.saved_order.get(), "newest"))
        state = STATUS_LABELS[self.saved_state_filter.get()]
        return [e for e in entries if (state == "all" or e.get("status", "new") == state)
                and (not self.saved_due_only.get() or review_due(e))]

    def select_all_saved(self, value: bool):
        for e in self._visible_saved_words() if value else load_saved_words():
            self._saved_check_var(e.get("word", "")).set(value)
        self._update_selected_count()

    def export_anki_selected(self):
        self._export_saved("txt")

    def export_anki_apkg_selected(self):
        self._export_saved("apkg")

    def _export_saved(self, kind):
        if not self._commit_or_discard_editor():
            return
        selected = [e for e in load_saved_words()
                   if self._saved_check_var(e.get("word", "")).get()]
        if not selected:
            self.toast("Спочатку познач слова прапорцями")
            return
        if kind == "apkg":
            if not apkg_available():
                messagebox.showinfo(
                    APP_NAME,
                    "Для готового файлу колоди (.apkg) потрібна додаткова бібліотека, якої зараз "
                    "немає в цьому Python:\n\n"
                    f'"{sys.executable}" -m pip install genanki\n\n'
                    "Постав її й спробуй ще раз. А поки що можна скористатись кнопкою "
                    "«→ Anki (.txt)» — вона працює без додаткових бібліотек.")
                return
            path = filedialog.asksaveasfilename(
                title="Експорт в Anki (готова колода)", defaultextension=".apkg",
                filetypes=[("Колода Anki", "*.apkg"), ("Усі файли", "*.*")],
                initialfile="word_lookup_anki.apkg")
        else:
            path = filedialog.asksaveasfilename(
                title="Експорт в Anki", defaultextension=".txt",
                filetypes=[("Текстовий файл для Anki", "*.txt"), ("Усі файли", "*.*")],
                initialfile="word_lookup_anki.txt")
        if not path:
            return
        need = sum(1 for e in selected if _needs_enrich(e))
        if need:  # старі слова без пояснень — дотягуємо (мережа), тож у фоновому потоці
            self.toast(f"Підтягую переклад і пояснення для {need} слів — зачекай…", timeout=20)
        threading.Thread(target=self._export_worker, args=(kind, selected, path),
                         daemon=True).start()

    def _export_worker(self, kind, selected, path):
        err = ""
        try:
            enrich_entries(selected)
            (export_anki_apkg if kind == "apkg" else export_anki_txt)(selected, path)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        events.put(("export_done", kind, len(selected), path, err))

    def _export_finished(self, kind, n, path, err):
        if hasattr(self, "saved_list_frame"):  # у списку з'являться дотягнуті переклади
            try:
                self._render_saved_list()
            except tk.TclError:
                pass
        if err:
            self.toast(f"Не вдалося створити файл: {err}", timeout=6)
            return
        if kind == "apkg":
            self.toast(f"Створено колоду з {n} слів — відкрий файл подвійним кліком, "
                       "Anki додасть картки сама", timeout=6)
            return
        try:
            with open(path, encoding="utf-8") as f:
                rows = [ln.rstrip("\n") for ln in f if not ln.startswith("#") and ln.strip()]
        except OSError:
            rows = []
        preview = "\n".join((r if len(r) <= 110 else r[:107] + "…") for r in rows[:3])
        messagebox.showinfo(
            APP_NAME,
            f"Збережено {n} слів:\n{path}\n\nПерші рядки файлу (слово ⇥ зворотна сторона):\n"
            f"{preview}\n\nУ вікні імпорту Anki перевір блок зіставлення полів: «Зворотна "
            "сторона» (Back) має бути прив'язана до стовпця 2. Якщо цих слів у колоді вже "
            "є — видали старі картки або вибери оновлення існуючих нотаток, інакше Anki "
            "може їх пропустити й зворотна сторона лишиться порожньою.")

    def _render_saved_list(self):
        selected = self.saved_tree.selection()
        previous = self._saved_iids.get(selected[0]) if selected else self._editor_word
        self.saved_tree.delete(*self.saved_tree.get_children())
        self._saved_iids = {}
        inverse = {v: k for k, v in STATUS_LABELS.items()}
        for entry in self._visible_saved_words():
            word = entry["word"]
            iid = self._saved_tree_id(word)
            if iid in self._saved_iids:
                continue
            self._saved_iids[iid] = word
            self.saved_tree.insert("", "end", iid=iid, values=(
                "☑" if self._saved_check_var(word).get() else "☐", word,
                entry.get("translation", ""), inverse.get(entry.get("status", "new"), "Нові")))
        if previous and self.saved_tree.exists(self._saved_tree_id(previous)):
            self.saved_tree.selection_set(self._saved_tree_id(previous))
            if previous == self._editor_word and not self._editor_dirty and hasattr(self, "editor_status"):
                self._loading_editor = True
                entry = saved_entry(previous)
                self.editor_status.set(inverse.get(entry.get("status", "new"), "Нові"))
                self._loading_editor = False
        elif self.saved_tree.get_children() and not self._editor_dirty:
            self.saved_tree.selection_set(self.saved_tree.get_children()[0])
        if not load_saved_words():
            for child in self.saved_list_frame.winfo_children():
                child.destroy()
            self._editor_word = None
            self._editor_dirty = False
            ttk.Label(self.saved_list_frame, text="Бібліотека порожня. Натисни ⭐ у картці слова чи фрази.",
                      wraplength=450).pack(padx=12, pady=20)
        self._update_selected_count()

    def open_saved_word(self, word):
        entry = saved_entry(word)
        if entry is not None:
            self._new_token()
            self.show_lookup(saved_word_card(entry), None)

    def delete_saved_word(self, word):
        if not remove_saved_word(word):
            self.toast("Не вдалося видалити запис; перевір журнал і доступ до файлу")
            return
        self.saved_checks.pop(word.lower(), None)
        if getattr(self, "_editor_word", None) == word:
            self._editor_word = None
            self._editor_dirty = False
        self.refresh_saved_count()

    def _check_ocr(self):
        """Перевірка йде у фоновому потоці — tesseract_ready() запускає subprocess,
        і навіть із тайм-аутом краще не тримати на цьому головний потік Tk."""
        _engine_cache["t"] = 0.0
        self.ocr_var.set("Перевіряю…")

        def work():
            t, r = tesseract_ready(), rapid_ready()
            txt = (f"Tesseract: {'✓ знайдено' if t else '✗ не знайдено'}    "
                   f"RapidOCR: {'✓ встановлено' if r else '✗ не встановлено'}")
            txt += f"\nPython: {sys.executable}"
            if not t and not r:
                txt += ("\nПотрібен хоча б один рушій. Ставити саме в цей Python:\n"
                        f'"{sys.executable}" -m pip install rapidocr-onnxruntime')
            events.put(("ocr_status", txt))
        threading.Thread(target=work, daemon=True).start()

    # ---------- запис гарячих клавіш ----------
    def start_record(self, action):
        if self.recording:
            self.finish_record()
        self.recording = action
        self.key_btns[action].config(text="Натисни…")
        self.key_vars[action].set("натисни комбінацію")
        self.manager.start_recording(lambda kind, text: events.put(("rec", kind, text)))

    def finish_record(self):
        action, self.recording = self.recording, None
        self.manager.recorder = None
        if action:
            self.key_btns[action].config(text="Записати")
            self.key_vars[action].set(pretty(cfg["hotkeys"][action]))

    def set_hotkey(self, action, combo) -> bool:
        if combo:
            for a, c in cfg["hotkeys"].items():
                if c == combo and a != action:
                    label = dict(ACTIONS)[a]
                    messagebox.showwarning(APP_NAME, f"«{pretty(combo)}» вже призначено для дії:\n{label}")
                    return False
        cfg["hotkeys"][action] = combo
        save_config(cfg)
        self.manager.set_bindings(cfg["hotkeys"])
        self.key_vars[action].set(pretty(combo))
        self._refresh_hint()
        return True

    def reset_hotkeys(self):
        self.finish_record()
        cfg["hotkeys"] = copy.deepcopy(DEFAULT_CONFIG["hotkeys"])
        save_config(cfg)
        self.manager.set_bindings(cfg["hotkeys"])
        for a, v in self.key_vars.items():
            v.set(pretty(cfg["hotkeys"][a]))
        self._refresh_hint()

    def handle_rec(self, kind, text):
        if not self.recording:
            return
        action = self.recording
        if kind == "progress":
            self.key_vars[action].set((pretty(text) + " + …") if text else "натисни комбінацію")
        elif kind == "cancel":
            self.finish_record()
        elif kind == "done":
            problem = combo_problem(text)
            self.finish_record()
            if problem:
                messagebox.showwarning(APP_NAME, problem)
            else:
                self.set_hotkey(action, text)

    # ---------- різні дії інтерфейсу ----------
    def pick_color(self):
        res = colorchooser.askcolor(color=cfg["popup"]["highlight_color"], title="Колір підсвічування")
        if res and res[1]:
            cfg["popup"]["highlight_color"] = res[1]
            save_config(cfg)
            self.color_btn.config(bg=res[1])

    def browse_tesseract(self):
        path = filedialog.askopenfilename(title="Вкажи tesseract (.exe)")
        if path:
            cfg["ocr"]["tesseract_cmd"] = path
            save_config(cfg)
            self._check_ocr()

    def test_lookup(self):
        word = clean_word(self.test_var.get().strip())[0]
        if not word:
            return
        e = self.test_entry
        anchor = (e.winfo_rootx(), e.winfo_rooty(), e.winfo_rootx() + e.winfo_width(),
                  e.winfo_rooty() + e.winfo_height())
        token = self._new_token()

        def work():
            post_lookup(token, "loading", anchor)

            def on_partial(partial_card, _t=token):
                if _t == self.active_token:
                    events.put(("show", partial_card, None, anchor, _t))

            try:
                card, showed_partial = build_word_card_fast(word, on_partial)
            except Exception as ex:
                if token == self.active_token:
                    post_lookup(token, "toast", f"Помилка: {ex}")
                return
            if token != self.active_token:
                return
            if showed_partial:
                events.put(("update", token, word, card))
            else:
                events.put(("show", card, None, anchor, token))
        threading.Thread(target=work, daemon=True).start()

    def diagnose_ocr(self):
        """Через 3 с робить знімок навколо курсора й показує, що бачить OCR."""
        def work():
            for n in (3, 2, 1):
                events.put(("toast", f"Діагностика OCR: наведи курсор на текст… {n}"))
                time.sleep(1)
            x, y = self.ms.position
            dbg, hit, err = {}, None, None
            try:
                hit = find_word_at(x, y, dbg)
            except OcrError as e:
                err = str(e)
            except Exception as e:
                log_exc()
                err = f"Помилка: {type(e).__name__}: {e}"
            dbg["time"] = LAST_OCR["time"]
            events.put(("diag", diag_report(dbg, hit, err, (x, y))))
        threading.Thread(target=work, daemon=True).start()

    def diagnose_dict(self):
        """Перевіряє переклад і dictionaryapi.dev на слові 'hello' окремо один від одного."""
        events.put(("dict_diag", "Перевіряю…"))

        def work():
            lines = []
            t0 = time.time()
            try:
                main, groups, name = translate_with_fallback("hello", "en", True)
                lines.append(f"✓ Переклад ({name}): {time.time() - t0:.1f} с — "
                             f"«{main}»" + (f", {len(groups)} груп" if groups else ""))
            except Exception as e:
                lines.append(f"✗ Переклад: {type(e).__name__}: {e}")

            t0 = time.time()
            try:
                info = _freedictionary_lookup("hello")
                dt = time.time() - t0
                if info["meanings"]:
                    lines.append(f"✓ dictionaryapi.dev: {dt:.1f} с — знайдено "
                                 f"{len(info['meanings'])} значень, транскрипція "
                                 f"«{info['phonetic'] or '—'}»")
                else:
                    lines.append(f"✗ dictionaryapi.dev: відповів за {dt:.1f} с, але без даних")
            except Exception as e:
                lines.append(f"✗ dictionaryapi.dev: {time.time() - t0:.1f} с — {e}")

            t0 = time.time()
            try:
                info2 = datamuse_dictionary("hello")
                dt = time.time() - t0
                if info2["meanings"]:
                    lines.append(f"✓ Datamuse (резервне джерело): {dt:.1f} с — знайдено "
                                 f"{len(info2['meanings'])} значень")
                else:
                    lines.append(f"✗ Datamuse (резервне джерело): відповів за {dt:.1f} с, без даних")
            except Exception as e:
                lines.append(f"✗ Datamuse (резервне джерело): {time.time() - t0:.1f} с — {e}")

            if not info["meanings"] and not info2["meanings"]:
                lines.append("Обидва джерела визначень недоступні — лишиться тільки переклад. "
                             "Спробуй ще раз пізніше або зміни мережу (VPN/мобільний інтернет).")
            events.put(("dict_diag", "\n".join(lines)))
        threading.Thread(target=work, daemon=True).start()

    def show_preview(self):
        b = self.preview_btn
        anchor = (b.winfo_rootx(), b.winfo_rooty(), b.winfo_rootx() + b.winfo_width(),
                  b.winfo_rooty() + b.winfo_height())
        self.show_lookup(SAMPLE_CARD, None, anchor)

    def _refresh_preview(self):
        if self._preview_timer is not None:
            self.root.after_cancel(self._preview_timer)
        self._preview_timer = self.root.after(80, self._apply_card_appearance)

    def _apply_card_appearance(self):
        self._preview_timer = None
        if self.popup:
            self.rerender(self.popup)

    def inside_main_window(self, x, y) -> bool:
        try:
            r = self.root
            if r.state() in ("withdrawn", "iconic"):
                return False
            return (r.winfo_rootx() <= x <= r.winfo_rootx() + r.winfo_width()
                    and r.winfo_rooty() <= y <= r.winfo_rooty() + r.winfo_height())
        except Exception:
            return False

    # ---------- показ картки ----------
    def close_all(self):
        for name in ("popup", "highlight"):
            obj = getattr(self, name)
            if obj:
                obj.close()
                setattr(self, name, None)

    def dismiss_lookup(self):
        self._new_token()  # запізнілий мережевий результат більше не відкриє картку
        self.close_all()
        if self.toast_popup:
            self.toast_popup.close()
            self.toast_popup = None
        self._resume_game()

    def show_highlight(self, hit):
        """Миттєвий відгук: підсвічування з'являється ще до того, як прийде переклад."""
        self.close_all()
        if self.toast_popup:
            self.toast_popup.close()
            self.toast_popup = None
        self.highlight = Highlight(self, hit["word"], hit["box"])
        self.hl_hit = hit

    def show_lookup(self, card, hit, anchor=None):
        if card.get("kind") == "word" and card is not SAMPLE_CARD and saved_entry(card["word"]):
            if add_saved_word(card):
                self.refresh_saved_count()
        reuse = hit is not None and self.highlight is not None and self.hl_hit is hit
        if self.popup:
            self.popup.close()
            self.popup = None
        if not reuse and self.highlight:
            self.highlight.close()
            self.highlight = None
        if self.toast_popup:
            self.toast_popup.close()
            self.toast_popup = None
        if hit is not None and cfg["ocr"]["highlight"]:
            if not reuse:
                self.highlight = Highlight(self, hit["word"], hit["box"])
                self.hl_hit = hit
            anchor = self.highlight.rect
        elif hit is not None:
            anchor = hit["box"]
        elif anchor is None:
            x, y = self.ms.position
            anchor = (x, y, x, y)
        self.popup = Popup(self, card, anchor)
        if card is not SAMPLE_CARD:
            self.last_card = card
            self._refresh_cache_label()

    def rerender(self, popup):
        card, anchor, exp = popup.card, popup.anchor, popup.expanded
        pinned = popup.pinned
        scroll = popup.viewport.canvas.yview()[0]
        popup.close()
        self.popup = Popup(self, card, anchor, exp, pinned=pinned)
        self.popup.win.update_idletasks()
        self.popup.viewport.canvas.yview_moveto(scroll)

    def toast(self, text, timeout=None):
        if self.toast_popup:
            self.toast_popup.close()
        x, y = self.ms.position
        self.toast_popup = Popup(self, {"kind": "toast", "text": text}, (x, y, x, y),
                                 toast=True, timeout=timeout)

    def show_loading(self, anchor=None):
        """Індикатор «Завантаження…» — з'являється миттєво в тому ж місці, куди потім
        стане картка. Автоматично зникає, коли прийде готова картка або повідомлення
        про помилку (show_lookup/show_highlight/toast самі закривають toast_popup)."""
        if self.toast_popup:
            self.toast_popup.close()
        if anchor is None:
            x, y = self.ms.position
            anchor = (x, y, x, y)
        self.toast_popup = Popup(self, {"kind": "toast", "text": "Завантаження…"}, anchor,
                                 toast=True, timeout=12.0)

    # ---------- пошук ----------
    def _new_token(self):
        """Кожен новий пошук отримує унікальний номер і одразу стає «активним» —
        це дозволяє застарілим (уже неактуальним) результатам, що прийдуть пізніше
        з паралельних потоків, розпізнати себе й не показатись поверх нового пошуку
        («останній запит виграє» замість жорсткого блокування одне одного)."""
        token = next(_token_counter)
        self.active_token = token
        return token

    def _lookup_thread(self, mode, x, y, token, frozen=None, pre_hit=None):
        def stale():
            return token != self.active_token

        if stale():
            return
        if mode == "selection":
            self._lookup_selection(x, y, token)
            return
        context_image = None
        if mode in ("word", "line") and cfg["learning"]["capture_context"]:
            try:
                context_image = _grab(x - 480, y - 240, x + 480, y + 240, frozen)[0]
            except OcrError as e:
                log(f"Скриншот контексту: {e}")
        log(f"Пошук слова біля ({x}, {y}), режим: {mode}")
        source = _game_source()  # заголовок активного вікна (гра) — поки фокус ще в ній
        try:
            hit = pre_hit if pre_hit is not None else find_word_at(x, y, wide=(mode == "line"), frozen=frozen)
        except OcrError as e:
            log(str(e))
            if not stale():
                post_lookup(token, "toast", str(e))
            return
        except Exception as e:
            log_exc()
            if not stale():
                post_lookup(token, "toast", f"Помилка: {type(e).__name__}: {e}")
            return
        if hit is None:
            n = LAST_OCR["count"]
            log(f"Слово не знайдено (OCR розпізнав слів: {n})")
            if not stale():
                post_lookup(token, "toast", "OCR не бачить тексту тут — спробуй «Діагностика OCR»"
                            if n == 0 else
                            f"Курсор не на слові (OCR бачить {n} слів поруч)")
            return
        if stale():  # поки йшло розпізнавання, користувач уже перейшов до іншого слова
            return
        log(f"Знайдено: {hit['word']} (OCR {LAST_OCR['time']:.2f} с)")
        t1 = time.time()
        shown_partial = []
        ctx = _clean_sentence(hit.get("line_text", ""))  # речення з гри — піде на картку Anki
        if ctx.lower() == hit["word"].lower():
            ctx = ""
        has_sentence = bool(cfg["popup"]["sentence"] or cfg["popup"]["sentence_translation"])
        short_ok = 2 <= len(ctx.split()) <= 40  # короткий рядок з першого розпізнавання — запасний варіант
        # поточний стан картки: його змінюють кілька потоків (переклад слова, речення,
        # визначення), тому кожна подія містить ПОВНИЙ знімок — одна не затирає іншу
        state = {"context": ctx, "source": source, "_context_image": context_image,
                 "sentence": ctx if (has_sentence and short_ok) else "", "sentence_pending": has_sentence}
        state_lock = threading.Lock()

        def emit(ev_kind, changes):
            """Оновлює стан і ПІД ТИМ САМИМ замком кладе подію в чергу — інакше знімки з
            різних потоків могли б потрапити туди в зворотному порядку й старий затер би новий."""
            with state_lock:
                state.update(changes)
                c = dict(state)
                if not stale():
                    if ev_kind == "show":
                        events.put(("show", c, hit, None, token))
                    else:
                        events.put(("update", token, hit["word"], c))
                return c

        if mode == "word" and cfg["ocr"]["highlight"]:
            events.put(("hl", hit, token))  # підсвічуємо одразу, не чекаючи мережі
        try:
            if mode == "line":
                line = hit["line_text"]
                card = text_card(line, translate_text(clean_hud_line(line)))
                card.update(source=source, _context_image=context_image)
                if not stale():
                    events.put(("show", card, None, (x, y, x, y), token))
            else:
                def sentence_job():  # повне речення (ширший OCR) і його переклад — паралельно зі словом
                    if stale():
                        return
                    sent = ""
                    try:
                        sent = find_sentence(hit, frozen)
                    except Exception as ex:
                        log(f"Пошук речення: {ex}")
                    if stale():
                        return
                    if not sent and short_ok:
                        sent = ctx
                    log(f"Речення: {len(sent.split())} слів" if sent else "Речення: не знайдено")
                    if sent:  # повне речення видно одразу, ще до перекладу
                        emit("update", {"sentence": sent, "context": sent})
                    tr = ""
                    if sent and 2 <= len(sent.split()) <= 60 and cfg["popup"]["sentence_translation"]:
                        try:
                            tr = translate_text(clean_hud_line(sent))
                        except Exception as ex:
                            log(f"Переклад речення: {ex}")
                    emit("update", {"sentence": sent, "context": sent or ctx,
                                    "sentence_tr": tr, "sentence_pending": False})

                if has_sentence:
                    threading.Thread(target=sentence_job, daemon=True).start()

                def on_partial(partial_card, _hit=hit, _t=token):
                    if _t != self.active_token:
                        return
                    log(f"Переклад: {time.time() - t1:.2f} с (словник ще довантажується)")
                    shown_partial.append(emit("show", partial_card))

                card, showed_partial = build_word_card_fast(hit["word"], on_partial,
                                                            context=hit.get("line_text", ""))
                log(f"Словник/переклад: {time.time() - t1:.2f} с")
                if stale():
                    return
                orig = card  # об'єкт із кешу: лише в нього дописуємо переклад визначення
                def_en = _first_definition_text(orig)
                need_def_tr = bool(def_en) and not orig.get("definition_tr") \
                    and cfg["sections"]["definitions"] and cfg["popup"]["translate_definition"]
                emit("update" if showed_partial else "show", {**orig, "def_tr_pending": need_def_tr})
                if need_def_tr:  # пояснення рідною мовою — додається, щойно перекладеться
                    try:
                        dtr = translate_text(def_en)
                    except Exception as ex:
                        log(f"Переклад визначення: {ex}")
                        dtr = ""
                    if dtr:
                        orig["definition_tr"] = dtr
                        if not orig.get("note") and not orig.get("dict_note"):
                            _cache_word_card(_cache_key(hit["word"], hit.get("line_text", "")), orig)
                    emit("update", {"definition_tr": dtr, "def_tr_pending": False})
        except Exception as e:
            if not stale():
                if shown_partial:  # чернетка вже на екрані — закриваємо її статус, а не лишаємо «Завантажую…»
                    emit("update", {"dict_pending": False, "note": state.get("note") or str(e)})
                else:
                    post_lookup(token, "toast", f"Помилка перекладу: {e}")

    # ---------- виділення області (як скріншот) ----------
    def _game_mode_changed(self):
        automatic = cfg["freeze"]["auto_game"]
        self.game_exe_entry.configure(state="disabled" if automatic else "normal")

    def _game_target_name(self):
        if cfg["freeze"]["auto_game"]:
            return ""
        name = cfg["freeze"]["game_exe"].strip()
        if not name:
            raise RuntimeError("Укажи .exe вручну або увімкни автоматичне визначення гри")
        return name

    def _display_game_detection(self):
        info = _last_game_detection
        name = info.get("name", "")
        title = info.get("title", "") or name
        reason = info.get("reason", "")
        self.game_detection_var.set(f"Визначено: {title[:80]} · {name} · {reason}" if name
                                    else "Активну гру визначено")

    def schedule_game_detection(self):
        timer = getattr(self, "_game_detect_timer", None)
        if timer is not None:
            self.root.after_cancel(timer)
        self.game_detection_var.set("Перемкнися у вікно гри — перевірка через 3 секунди. Пауза не вмикається.")
        self._game_detect_timer = self.root.after(3000, self._check_active_game)

    def _check_active_game(self):
        self._game_detect_timer = None
        try:
            foreground_game(self._game_target_name())
            self._display_game_detection()
            return True
        except Exception as e:
            self.game_detection_var.set(str(e))
            return False

    def _pause_option_changed(self):
        if not cfg["freeze"]["pause_game"]:
            self._resume_game()

    def _begin_game_pause(self):
        if not cfg["freeze"]["pause_game"]:
            return True
        if self._game_pause is not None:
            if self._game_pause.active:
                return True
            self._resume_game()
        guard = None
        try:
            pid, created, hwnd = foreground_game(self._game_target_name())
            self._display_game_detection()
            limit = clamp(cfg["freeze"]["pause_seconds"], 5, 300)
            guard = GamePauseGuard(pid, created, limit)
            self._game_pause, self._game_pause_hwnd = guard, hwnd
            guard.start()
            log(f"Гру призупинено: PID {pid}; ліміт {limit:.0f} с")
            return True
        except Exception as e:
            if guard is not None and not guard.active:
                self._game_pause = self._game_pause_hwnd = None
            self.toast(f"Не вдалося підтвердити паузу гри: {e}", timeout=8)
            return False

    def _resume_game(self):
        guard = self._game_pause
        if guard is None:
            return True
        ok, status = guard.release()
        if not guard.active:
            self._game_pause = None
            if ok:
                _restore_foreground(self._game_pause_hwnd)
            self._game_pause_hwnd = None
        if not ok:
            log(f"Відновлення гри: {status}")
        return ok

    def start_region_select(self):
        if self.selector or self.free_cursor or self.paused:
            return
        if not self._begin_game_pause():
            return
        self.close_all()
        previous_windows = set(self.root.winfo_children())
        try:
            self.selector = RegionSelector(self, self._region_done)
        except Exception as e:
            for child in set(self.root.winfo_children()) - previous_windows:
                try:
                    child.destroy()
                except tk.TclError:
                    pass
            self._resume_game()
            self.toast(f"Не вдалося відкрити вибір слова: {e}")

    def _region_done(self, result, frozen=None):
        self.selector = None
        if result is None:
            self._resume_game()
        if result is not None:
            token = self._new_token()
            threading.Thread(target=self._region_thread, args=(result, frozen, token), daemon=True).start()

    # ---------- керований клавіатурою курсор (для ігор із захопленою мишею) ----------
    def start_free_cursor(self):
        if self.selector or self.free_cursor or self.paused:
            return
        if not self._begin_game_pause():
            return
        self.close_all()
        start = self.free_cursor_pos or (self.root.winfo_screenwidth() // 2,
                                         self.root.winfo_screenheight() // 2)
        previous_windows = set(self.root.winfo_children())
        try:
            self.free_cursor = FreeCursorOverlay(self, start, self._free_cursor_done)
        except Exception as e:
            for child in set(self.root.winfo_children()) - previous_windows:
                try:
                    child.destroy()
                except tk.TclError:
                    pass
            self._resume_game()
            self.toast(f"Не вдалося відкрити керований курсор: {e}")
            return
        self._fc_mouse_pos = None
        self._fc_delta = [0, 0]
        try:
            self._fc_mouse_listener = mouse.Listener(on_move=self._on_fc_mouse_move)
            self._fc_mouse_listener.daemon = True
            self._fc_mouse_listener.start()
            self.manager.start_raw_mode(lambda name: events.put(("fc_key", name)))
        except Exception as e:
            self.free_cursor.finish(None)
            self.toast(f"Не вдалося підключити керований курсор: {e}")

    def _on_fc_mouse_move(self, x, y):
        """Виконується у власному потоці pynput — лише накопичуємо зміщення від
        попереднього положення. Низькорівневий хук зазвичай бачить фізичний рух
        миші, навіть коли гра «захопила» курсор для керування камерою."""
        if self._fc_mouse_pos is not None:
            dx, dy = x - self._fc_mouse_pos[0], y - self._fc_mouse_pos[1]
            with self._fc_lock:
                self._fc_delta[0] += dx
                self._fc_delta[1] += dy
        self._fc_mouse_pos = (x, y)

    def _stop_free_cursor_input(self):
        if self._fc_mouse_listener is not None:
            try:
                self._fc_mouse_listener.stop()
            except Exception:
                pass
            self._fc_mouse_listener = None
        self.manager.stop_raw_mode()

    def _free_cursor_done(self, pos):
        self._stop_free_cursor_input()
        self.free_cursor = None
        if pos is None:
            self._resume_game()
        if pos is not None:
            self.free_cursor_pos = pos
            # невелика затримка, щоб екран встиг перемалюватись після зникнення накладки;
            # root.after — щоб не блокувати головний потік Tk (на відміну від _region_thread,
            # який уже виконується у фоновому потоці)
            token = self._new_token()
            self.root.after(150, lambda: self._start_lookup("word", pos=pos)
                            if token == self.active_token else None)

    def _region_thread(self, result, frozen=None, token=None):
        if token is None:
            token = self._new_token()
        if frozen is None:
            time.sleep(0.15)  # без стоп-кадру — даємо екрану перемалюватись після зникнення накладки
        if token != self.active_token:
            return
        if result[0] == "point":
            post_lookup(token, "loading", (result[1], result[2], result[1], result[2]))
            self._lookup_thread("word", result[1], result[2], token, frozen)
            return
        if result[0] == "word":  # слово вже підсвічене й розпізнане на застиглому екрані
            post_lookup(token, "loading", (result[1], result[2], result[1], result[2]))
            self._lookup_thread("word", result[1], result[2], token, frozen, pre_hit=result[3])
            return

        def stale():
            return token != self.active_token

        _, x0, y0, x1, y1 = result
        source = _game_source()
        context_image = None
        if cfg["learning"]["capture_context"]:
            try:
                context_image = _grab(x0, y0, x1, y1, frozen)[0]
            except OcrError as e:
                log(f"Скриншот контексту: {e}")
        post_lookup(token, "loading", (x0, y0, x1, y1))
        log(f"Виділено область ({x0}, {y0})–({x1}, {y1})")
        try:
            words = ocr_region(x0, y0, x1, y1, frozen=frozen)
        except OcrError as e:
            if not stale():
                post_lookup(token, "toast", str(e))
            return
        except Exception as e:
            log_exc()
            if not stale():
                post_lookup(token, "toast", f"Помилка: {type(e).__name__}: {e}")
            return
        if not words:
            if not stale():
                post_lookup(token, "toast", "У рамці не знайдено тексту" if LAST_OCR["count"] == 0
                            else "У рамці немає розпізнаного тексту — виділи трохи ширше")
            return
        if stale():
            return
        text = " ".join(w.text for w in words)
        core = clean_word(words[0].text)[0] if len(words) == 1 else ""
        try:
            if core:  # одне слово -> словникова картка
                w = words[0]
                _, lead, trail = clean_word(w.text)
                wd = w.x1 - w.x0
                hit = {"word": core, "line_text": text,
                       "box": (int(w.x0 + wd * lead), int(w.y0),
                               int(w.x1 - wd * trail), int(w.y1))}
                if cfg["ocr"]["highlight"]:
                    events.put(("hl", hit, token))

                def on_partial(partial_card, _hit=hit, _t=token):
                    if _t == self.active_token:
                        events.put(("show", {**partial_card, "_context_image": context_image, "source": source}, _hit, None, _t))

                card, showed_partial = build_word_card_fast(core, on_partial)
                card = {**card, "_context_image": context_image, "source": source}
                if stale():
                    return
                if showed_partial:
                    events.put(("update", token, core, card))
                else:
                    events.put(("show", card, hit, None, token))
            elif re.search(r"[A-Za-z]", text):  # кілька слів / рядок -> переклад тексту
                if not stale():
                    card = text_card(text, translate_text(clean_hud_line(text)))
                    card.update(_context_image=context_image, source=source)
                    events.put(("show", card, None, (x0, y0, x1, y1), token))
            else:
                if not stale():
                    post_lookup(token, "toast", "У рамці немає англійського тексту")
        except Exception as e:
            if not stale():
                post_lookup(token, "toast", f"Помилка перекладу: {e}")

    def _lookup_selection(self, x, y, token):
        def stale():
            return token != self.active_token

        text = self._copy_selection()
        if not text:
            if not stale():
                post_lookup(token, "toast", "Нічого не виділено")
            return
        text = " ".join(text.split())[:1500]
        anchor = (x, y, x, y)
        try:
            core = text.strip(STRIP_CHARS)
            if core and WORD_RE.fullmatch(core):
                def on_partial(partial_card, _t=token):
                    if _t == self.active_token:
                        events.put(("show", partial_card, None, anchor, _t))

                card, showed_partial = build_word_card_fast(core, on_partial)
                if stale():
                    return
                if showed_partial:
                    events.put(("update", token, core, card))
                    return
            else:
                card = text_card(text, translate_text(text))
        except Exception as e:
            if not stale():
                post_lookup(token, "toast", f"Помилка перекладу: {e}")
            return
        if not stale():
            events.put(("show", card, None, anchor, token))

    def _copy_selection(self) -> str:
        try:
            old = pyperclip.paste()
        except Exception:
            old = ""
        t0 = time.time()
        while self.manager.keys_down() and time.time() - t0 < 0.7:  # чекаємо відпускання клавіш
            time.sleep(0.02)
        for name in list(self.manager.down):
            if name in MOD_KEYS:
                self.kb.release(MOD_KEYS[name])
        marker = f"__word_lookup_{time.time()}__"
        pyperclip.copy(marker)
        self.manager.ignore_until = time.time() + 0.8
        with self.kb.pressed(COPY_MOD):
            self.kb.press("c")
            self.kb.release("c")
        text = ""
        for _ in range(15):
            time.sleep(0.03)
            try:
                cur = pyperclip.paste()
            except Exception:
                continue
            if cur != marker:
                text = cur
                break
        try:
            pyperclip.copy(old)
        except Exception:
            pass
        return text.strip()

    def _handle_auto_release(self, x, y, btn):
        """Ліва кнопка: перетягування (виділення) або подвійний клік -> показати словник/переклад,
        не чекаючи гарячої клавіші. Працює лише там, де текст можна виділити й скопіювати
        (браузер, PDF, Word) — на відміну від OCR-пошуку, який працює і в іграх."""
        if btn != "left" or not cfg["auto"]["enabled"] or self.paused or self.selector or self.free_cursor:
            return
        if self.popup and self.popup.contains(x, y):
            return  # клік по самій картці (кнопки 🔊, «more») — не запускати повторно
        if self.inside_main_window(x, y):
            return  # клік у вікні налаштувань — не текстовий виділений фрагмент
        px, py = self.auto_state["press"]
        moved = abs(x - px) + abs(y - py) > int(clamp(cfg["auto"]["drag_px"], 2, 100))
        now = time.time()
        double_click = (not moved
                        and now - self.auto_state["last_release"] < clamp(
                            cfg["auto"]["double_click_s"], 0.1, 1.0))
        self.auto_state["last_release"] = now
        if moved or double_click:
            # невелика затримка: додаток під курсором ще фіксує виділення
            token = self._new_token()
            self.root.after(80, lambda: self._start_lookup("selection", pos=(x, y))
                            if token == self.active_token and not self.paused else None)

    def _start_lookup(self, mode, pos=None):
        x, y = pos if pos is not None else self.ms.position
        token = self._new_token()
        post_lookup(token, "loading", (x, y, x, y))
        threading.Thread(target=self._lookup_thread, args=(mode, x, y, token), daemon=True).start()

    # ---------- дії ----------
    def do_action(self, name):
        if name == "resume_game":
            if self.selector:
                self.selector.finish(None)
            if self.free_cursor:
                self.free_cursor.finish(None)
            self.dismiss_lookup()
            return
        if name == "toggle_pause":
            self.toggle_pause()
            return
        if self.paused:
            return
        if name == "lookup_word":
            self._start_lookup("word")
        elif name == "translate_line":
            self._start_lookup("line")
        elif name == "select_region":
            self.start_region_select()
        elif name == "free_cursor":
            self.start_free_cursor()
        elif name == "unlock_cursor":
            self.toast("Курсор відкріплено" if release_cursor_clip()
                       else "Не вдалося (працює лише на Windows)")
        elif name == "translate_selection":
            self._start_lookup("selection")
        elif name == "speak":
            card = self.last_card
            audios = card.get("audios") if card else None
            if audios and AUDIO_OK:
                play_audio(audios[0][1])
            else:
                self.toast("Немає озвучення (потрібен pygame)" if not AUDIO_OK else "Немає озвучення")
        elif name == "copy_translation":
            if self.last_card:
                pyperclip.copy(self.last_card["translation"])
                self.toast("Переклад скопійовано")
        elif name == "close_popup":
            self.dismiss_lookup()

    def toggle_pause(self):
        self.paused = not self.paused
        if self.paused:
            if self.selector:
                self.selector.finish(None)
            if self.free_cursor:
                self.free_cursor.finish(None)
            self.dismiss_lookup()
        self._refresh_status()
        self.toast("Пауза" if self.paused else "Працює")

    def _mouse_match(self, btn_name, mods) -> bool:
        want = cfg["mouse"]["button"]
        if want == "none" or btn_name not in MOUSE_BTN_ALIASES.get(want, ()):
            return False
        mod = cfg["mouse"]["modifier"]
        return mod == "none" or mod in mods

    # ---------- події ----------
    def _on_click(self, x, y, button, pressed):
        kind = "press" if pressed else "release"
        events.put((kind, x, y, button.name, set(self.manager.down)))

    def _pump(self):
        try:
            while True:
                ev = events.get_nowait()
                try:
                    self._handle(ev)
                except Exception:
                    log_exc()
        except queue.Empty:
            pass
        if self.free_cursor is not None:
            with self._fc_lock:
                dx, dy = self._fc_delta
                self._fc_delta[0] = self._fc_delta[1] = 0
            if dx or dy:
                self.free_cursor.nudge(dx, dy)
        if self._game_pause is not None and not self._game_pause.active:
            ok = self._resume_game()
            self.toast("Пауза гри завершена (ліміт часу або закриття процесу)" if ok
                       else "Не підтверджено відновлення гри — перевір журнал", timeout=6)
        self.root.after(40, self._pump)

    def _handle(self, ev):
        kind = ev[0]
        if kind == "lookup_event":
            if ev[1] == self.active_token:
                self._handle(ev[2])
            return
        if kind == "show":
            _, card, hit, anchor, *rest = ev
            token = rest[0] if rest else None
            if token is not None and token != self.active_token:
                return  # новіший пошук уже почався — цей результат застарів
            if token is not None:
                self.active_token = token
            self.show_lookup(card, hit, anchor)
        elif kind == "update":
            _, token, word, full_card = ev
            if (token == self.active_token and self.popup
                    and self.popup.card.get("kind") == "word"
                    and self.popup.card.get("word") == word):
                self.popup.card = full_card
                if saved_entry(word) and add_saved_word(full_card):
                    self.refresh_saved_count()
                self.rerender(self.popup)
                self.last_card = full_card
                self._refresh_cache_label()
        elif kind == "loading":
            self.show_loading(ev[1])
        elif kind == "toast":
            if self._game_pause is not None and not self.popup:
                self._resume_game()
            if self.highlight and not self.popup:  # пошук не вдався — прибираємо підсвічування
                self.highlight.close()
                self.highlight = None
            self.toast(ev[1])
        elif kind == "hl":
            _, hit, hl_token = ev
            if hl_token == self.active_token:
                self.show_highlight(hit)
        elif kind == "esc":
            if self.selector:
                self.selector.finish(None)
            if self.free_cursor:
                self.free_cursor.finish(None)
            self.dismiss_lookup()
        elif kind == "fc_key":
            if self.free_cursor:
                fast = "shift" in self.manager.down
                self.free_cursor.key(ev[1], fast)
        elif kind == "action":
            self.do_action(ev[1])
        elif kind == "rec":
            self.handle_rec(ev[1], ev[2])
        elif kind == "press":
            _, x, y, btn, mods = ev
            keep_preview = (self.popup and self.popup.card is SAMPLE_CARD
                            and self.inside_main_window(x, y))
            if self.popup and not self.popup.pinned and not self.popup.contains(x, y) and not keep_preview:
                self.dismiss_lookup()
            if not self.paused and not self.selector and not self.free_cursor and self._mouse_match(btn, mods):
                token = self._new_token()
                self.show_loading((x, y, x, y))
                threading.Thread(target=self._lookup_thread, args=("word", x, y, token),
                                 daemon=True).start()
            if btn == "left":
                self.auto_state["press"] = (x, y)
        elif kind == "release":
            self._handle_auto_release(*ev[1:4])
        elif kind == "diag":
            text, path = ev[1]
            print(text, flush=True)
            if path:
                open_file(path)
                text += f"\n\nЗнімок із рамками слів: {path}"
            messagebox.showinfo(APP_NAME, text)
        elif kind == "dict_diag":
            self.dict_var.set(ev[1])
        elif kind == "ocr_status":
            self.ocr_var.set(ev[1])
        elif kind == "deepl_status":
            self.deepl_var.set(ev[1])
        elif kind == "export_done":
            self._export_finished(*ev[1:])
        elif kind == "anki_sync_done":
            self.anki_sync_btn.state(["!disabled"])
            result, err = ev[1:]
            if err:
                self.anki_sync_status.set(err)
            else:
                self.anki_sync_status.set(f"Додано: {result['added']} · Оновлено: {result['updated']} · "
                                          f"Помилок: {result['failed']}")
                if result["errors"]:
                    messagebox.showwarning(APP_NAME, "\n".join(result["errors"][:10]), parent=self.saved_win)
        elif kind == "tray":
            if ev[1] == "show":
                self.show_window()
            elif ev[1] == "pause":
                self.toggle_pause()
            elif ev[1] == "quit":
                self.quit()

    # ---------- вікно та трей ----------
    def hide_window(self):
        if self.tray:
            self.root.withdraw()
        else:
            self.root.iconify()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def on_close(self):
        self.hide_window()

    def _setup_tray(self):
        if not TRAY_OK:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Відкрити налаштування",
                                 lambda icon, item: events.put(("tray", "show")), default=True),
                pystray.MenuItem("Пауза / продовжити",
                                 lambda icon, item: events.put(("tray", "pause"))),
                pystray.MenuItem("Вийти", lambda icon, item: events.put(("tray", "quit"))))
            self.tray = pystray.Icon("WordLookup", make_tray_icon(), APP_NAME, menu)
            self.tray.run_detached()
        except Exception as e:
            print(f"Трей недоступний: {e}")
            self.tray = None

    def quit(self):
        if not self._commit_or_discard_editor():
            return
        self._flush_config_save()
        if self._preview_timer is not None:
            self.root.after_cancel(self._preview_timer)
            self._preview_timer = None
        self._new_token()
        self._resume_game()
        save_card_cache()
        self._stop_free_cursor_input()
        for stop in (lambda: self.tray and self.tray.stop(),
                     lambda: self.manager.listener.stop(),
                     lambda: self.mouse_listener.stop()):
            try:
                stop()
            except Exception:
                pass
        self.close_all()
        self.root.destroy()


def main():
    log(f"Запуск: {sys.platform}, Python {sys.version.split()[0]}")
    load_card_cache()
    App().run()


if __name__ == "__main__":
    main()
