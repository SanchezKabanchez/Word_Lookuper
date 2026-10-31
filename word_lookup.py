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
if __name__ == "__main__" and sys.argv[1:2] == ["--pause-watchdog"]:
    from word_lookup_pause import run_watchdog
    run_watchdog(sys.argv[2:])
    raise SystemExit(0)

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk
import tkinter.font as tkfont

import pyperclip
import requests
from deep_translator import GoogleTranslator
from wl_network import (REQUEST_GATE, TEXT_CACHE, LookupCancelled, check_cancelled,
                        correct_ocr_word, network_scope, scoped_task, cancellable_lookup, network_job)
from pynput import keyboard, mouse

def config_path() -> str:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "WordLookup", "config.json")


# ------------------------------ журнал ------------------------------
_LOG_MAX = 300_000  # байт; при перевищенні файл перейменовується в app.log.1 (максимум ~600 КБ разом)
_log_lock = threading.Lock()
_log_state = {"n": 0}
_SECRET_RE = re.compile(r"(DeepL-Auth-Key\s+)\S+|(['\"]?key['\"]?\s*[:=]\s*['\"])[^'\"]+", re.I)


def log_path() -> str:
    return os.path.join(os.path.dirname(config_path()), "app.log")


def _mask_secrets(text: str) -> str:
    key = globals().get("cfg", {}).get("deepl", {}).get("key", "")
    if key:
        text = text.replace(key, "***")
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
        print("[WordLookup] Не вдалося записати журнал.", file=sys.stderr)


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
        except FileNotFoundError:
            continue  # no rotated log on a fresh installation
        except OSError:
            log_exc('word_lookup.read_log_tail')
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
        log_exc('word_lookup._restore_foreground')


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


from word_lookup_pause import GamePauseGuard, recover_paused_games


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
        log_exc('word_lookup.registered_game_paths')
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
        log_exc('word_lookup.startup')

TRAY_OK = False
if sys.platform != "darwin":
    try:
        import pystray  # noqa: F401
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
        TRAY_OK = True
    except Exception:
        log_exc('word_lookup.startup')

# ============================== Конфігурація ===============================
DEFAULT_CONFIG = {
    "version": 5,
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
    "popup": {"theme": "dark", "font_size": 11, "width": 480, "opacity": 1.0,
              "timeout": 60, "highlight_color": "#ffd43b", "btn_speak": True,
              "sentence": True, "sentence_translation": True, "translate_definition": True,
              "phonetic": True, "examples": True, "alternate_translations": True,
              "context_image": True},
    "cache": {"max_entries": 1000},
    "deepl": {"enabled": True, "key": ""},
    "anki": {"game": "", "auto_game_tag": True, "deck": "Word Lookup"},
    "freeze": {"hover_highlight": True, "pause_game": False, "auto_game": True,
               "game_exe": "", "pause_seconds": 20},
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
    "dark": dict(bg="#1d2029", fg="#f1f1f5", dim="#a2a8b8", head="#a2a8b8",
                 line="#2b2f3b", link="#ffd43b", example="#b8becc", border="#303542"),
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
            if saved_version < 5:
                freeze = saved.get("freeze")
                if isinstance(freeze, dict) and freeze.get("pause_seconds") == 120:
                    freeze["pause_seconds"] = 20
            saved["version"] = DEFAULT_CONFIG["version"]
        if isinstance(saved, dict):
            _merge(cfg, saved)
            old_hotkeys = saved.get("hotkeys", {})
            if isinstance(old_hotkeys, dict) and "resume_game" not in old_hotkeys:
                resume = cfg["hotkeys"]["resume_game"]
                if resume and any(combo == resume for action, combo in cfg["hotkeys"].items()
                                  if action != "resume_game"):
                    cfg["hotkeys"]["resume_game"] = ""
    except FileNotFoundError:
        saved = None  # a fresh installation uses defaults and the OS vault
    except (OSError, ValueError):
        log_exc('word_lookup.load_config')
    try:
        from word_lookup_secrets import load_key
        secure_key = load_key()
        if secure_key:
            cfg["deepl"]["key"] = secure_key
    except Exception:
        # Backend messages can include credentials. Report only a safe diagnosis.
        print("[WordLookup] Windows Credential Manager недоступний; ключ не прочитано.")
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


_credential_status = ""


def save_config(cfg: dict) -> bool:
    """Persist settings, with the API key stored only in the OS credential vault."""
    global _credential_status
    from word_lookup_secrets import store_key
    _credential_status = ""
    key = cfg.get("deepl", {}).get("key", "").strip()
    secure = False
    try:
        secure = store_key(key)
    except Exception:
        # Never include a backend exception or its arguments in the log.
        print("[WordLookup] Не вдалося записати ключ у Windows Credential Manager.")
    safe = copy.deepcopy(cfg)
    safe["deepl"]["key"] = ""
    try:
        atomic_json_write(config_path(), safe)
    except OSError:
        log_exc("Збереження налаштувань")
        return False
    if key and not secure:
        _credential_status = ("Ключ працює лише в цьому сеансі: "
                              "не вдалося зберегти у Windows Credential Manager.")
        log(_credential_status)
    elif key:
        _credential_status = "Ключ збережено у Windows Credential Manager."
    return True


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


def stop_input_listener(listener, timeout=1.0):
    """Stop an owned pynput listener and release its OS resources within a bound.

    pynput 1.8.2's XRecord backend sends disable on its recording connection;
    that connection is blocked by RecordEnableContext. Send the same request on
    its existing control connection and flush it so the backend's own finally
    block closes both displays. Never close a running listener's displays here.
    """
    if listener is None or not listener.is_alive():
        return True
    deadline = time.monotonic() + timeout
    # pynput.wait has no timeout and can hang if backend initialization failed.
    while listener.is_alive() and not getattr(listener, "_ready", False):
        if time.monotonic() >= deadline:
            log("Системний слухач не завершив ініціалізацію вчасно")
            return False
        time.sleep(0.005)
    if not listener.is_alive():
        return True
    try:
        listener.stop()
        if type(listener).__module__.endswith("._xorg") and listener.is_alive():
            control = getattr(listener, "_display_stop", None)
            context = getattr(listener, "_context", None)
            if control is not None and context is not None:
                try:
                    control.record_disable_context(context)
                    control.flush()
                except Exception:
                    # The backend may finish and close this control connection
                    # between the liveness check and flush after stop().
                    if listener.is_alive():
                        raise
                    return True
        if listener is not threading.current_thread():
            listener.join(timeout=max(0.0, deadline - time.monotonic()))
    except Exception:
        log_exc("Завершення системного слухача")
        return False
    stopped = not listener.is_alive()
    if not stopped:
        log("Системний слухач не завершився вчасно")
    return stopped


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


def _remote_get(url, **kwargs):
    REQUEST_GATE.wait()
    return SESSION.get(url, **kwargs)


def _remote_post(url, **kwargs):
    REQUEST_GATE.wait()
    return SESSION.post(url, **kwargs)


def _http_json(url, params, timeout=5):
    r = _remote_get(url, params=params, timeout=timeout)
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

    response = _remote_get("https://translate.google.com/m", params={
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
        check_cancelled()
        until, was_limit = _cooldown.get(name, (0, False))
        if time.time() < until:
            limited = limited or was_limit
            continue
        try:
            main, groups = fn(text, sl, want_dict)
        except LookupCancelled:
            raise
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
    base = IRREGULAR_FORMS.get(w) if w not in IRREGULAR_AMBIGUOUS else None
    out = [base, w] if base else [w]
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
    r = _remote_get("https://api.datamuse.com/words", params=params, timeout=timeout)
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
        f_syn, f_ant = pool.submit(scoped_task(rel, "syn")), pool.submit(scoped_task(rel, "ant"))
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
            r = _remote_get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{c}", timeout=6)
        except LookupCancelled:
            raise
        except Exception:
            return "err", None
        if r.status_code == 200:
            try:
                return "ok", r.json()
            except ValueError:
                return "err", None
        return ("missing" if r.status_code == 404 else "err"), None

    # Stop at the first real entry: do not send speculative suffix requests in parallel.
    results = []
    for candidate in cands:
        check_cancelled()
        result = fetch(candidate)
        results.append(result)
        if result[0] in {"ok", "err"}:
            break
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
    except LookupCancelled:
        raise
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
    word = correct_ocr_word(word)
    low = word.lower()
    base = IRREGULAR_FORMS.get(low)
    if base and base != low:
        direct = None
        if low in IRREGULAR_AMBIGUOUS:
            try:
                direct = _english_lookup(word)
            except LookupCancelled:
                raise
            except Exception:
                direct = None
            if direct and direct.get("meanings") and not _looks_like_form_of(direct):
                return direct
        try:
            info = _english_lookup(base)
        except LookupCancelled:
            raise
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
    except LookupCancelled:
        raise
    except Exception as e:
        primary, err = empty, str(e)
    if primary["meanings"]:
        return primary
    try:
        fallback = datamuse_dictionary(word)
    except LookupCancelled:
        raise
    except Exception:
        fallback = empty
    if fallback["meanings"]:
        log(f"Словник: основне джерело без результату, використано запасне (Datamuse)")
        return fallback
    corrected = _datamuse_spellcheck(word)
    if corrected:
        try:
            retry = datamuse_dictionary(corrected)
        except LookupCancelled:
            raise
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
    r = _remote_post(_deepl_base() + "/v2/translate", data=data,
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
    r = _remote_get(_deepl_base() + "/v2/usage", headers=_deepl_headers(), timeout=6)
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
    except LookupCancelled:
        raise
    except Exception as e:
        _cooldown["DeepL"] = (time.time() + 30, False)
        log(f"DeepL: {type(e).__name__}: {e}")
    return None


def translate_text(text: str) -> str:
    """Reuse sentence/definition translations across lookups, languages and launches."""
    text = " ".join(text.split())
    if not text:
        return ""
    def compute():
        dl = _try_deepl(text, "auto", "")
        if dl:
            return dl
        main, _groups, _name = translate_with_fallback(text, "auto", False)
        return main
    key = json.dumps([cfg["languages"]["target"], bool(_deepl_active()), text], ensure_ascii=False)
    limit = int(clamp(cfg["cache"]["max_entries"], 0, 1000))
    path = os.path.join(os.path.dirname(config_path()), "translations.json")
    return TEXT_CACHE.translate(key, compute, limit=limit, path=path,
                                on_error=lambda error: log(f"Кеш перекладів: {error}"))


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


import word_lookup_storage as _word_lookup_storage
_word_lookup_storage.configure(sys.modules[__name__])
from word_lookup_storage import (
    _cache_path,
    load_card_cache,
    cache_disk_kb,
    save_card_cache,
    clear_card_cache,
    cached_word_card,
    _saved_words_path,
    load_saved_words,
    _write_saved_words,
    saved_entry,
    _entry_complete,
    is_word_saved,
    _card_example,
    _card_definition,
    _clean_sentence,
    _anki_tag,
    _common_prefix,
    align_word_span,
    split_around_word,
    _first_definition_text,
    _card_primary_translation,
    primary_translations,
    add_saved_word,
    remove_saved_word,
    filter_saved_words,
    saved_word_card,
    update_saved_entry,
    review_due,
    record_review,
    context_image_path,
    save_context_image,
    context_photo,
)


# ------------------------------ Збережені слова -----------------------------


_saved_words: list = None  # завантажується лише раз, далі тримається в пам'яті


STATUS_LABELS = {"Усі": "all", "Нові": "new", "Вивчаю": "learning", "Знаю": "known"}
EDIT_FIELDS = ("translation", "definition", "definition_tr", "example", "context", "context_tr", "source", "notes")


import word_lookup_anki as _word_lookup_anki
_word_lookup_anki.configure(sys.modules[__name__])
from word_lookup_anki import (
    _anki_field,
    _anki_back,
    export_anki_txt,
    _needs_enrich,
    enrich_entry,
    enrich_entries,
    apkg_available,
    _emphasize,
    _anki_html_fields,
    _entry_tag,
    AnkiConnectError,
    _anki_identity,
    anki_call,
    sync_anki_entries,
    export_anki_apkg,
)


# ------------------ дозаповнення старих збережених слів ------------------


_APKG_CSS = (".card{font-family:arial;font-size:20px;text-align:left;color:#222;background:#fff}"
             ".nightMode .card{color:#eee;background:#2c2c2c}"
             ".word{font-size:30px;text-align:center}"
             ".tr{font-size:26px;font-weight:bold;text-align:center;margin:8px 0}"
             ".lbl{font-size:14px;opacity:.7;margin-top:14px}"
             ".box{background:rgba(127,127,127,.15);border-radius:8px;padding:10px 12px;margin-top:4px}")


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
    """Prefer configured DeepL; only ask Google when DeepL is unavailable.

    Avoid a second translation request merely to obtain alternative word groups.
    Dictionary definitions still load independently for the word card.
    """
    if _deepl_active():
        dl = _try_deepl(word, "en", _clean_context(word, context))
        if dl:
            return dl, [], "", False
    return _google_word(word)


def build_word_card(word: str, context: str = "") -> dict:
    """Повна картка за один виклик: чекає і переклад, і словник одночасно."""
    word = correct_ocr_word(word)
    key = _cache_key(word, context)
    cached = cached_word_card(key)
    if cached is not None:
        cached["word"] = word
        return cached
    empty = {"base": "", "phonetic": "", "audios": [], "meanings": []}
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        f_t = pool.submit(scoped_task(_translate_word, word, context))
        f_e = pool.submit(scoped_task(english_dictionary, word))
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
    word = correct_ocr_word(word)
    key = _cache_key(word, context)
    cached = cached_word_card(key)
    if cached is not None:
        cached["word"] = word
        return cached, False
    empty = {"base": "", "phonetic": "", "audios": [], "meanings": []}
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        # Обидва джерела стартують разом; показ першого перекладу не чекає словника.
        dictionary = pool.submit(scoped_task(english_dictionary, word))
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


import word_lookup_ocr as _word_lookup_ocr
_word_lookup_ocr.configure(sys.modules[__name__])
from word_lookup_ocr import (
    OcrError,
    _default_tesseract,
    _configure_tesseract_cmd,
    tesseract_ready,
    rapid_ready,
    pick_engine,
    _pick_engine_uncached,
    ocr_tesseract,
    _char_w,
    split_line_words,
    _speed_up_rapid,
    _rapid_engine,
    _gap_segments,
    ocr_rapid,
    pick_word,
    clean_word,
    _grab,
    _attempts,
    _run_ocr,
    _find_word_gap,
    _reocr_crop,
    _split_merged_word,
    _find_word_at,
    find_word_at,
    cluster_lines,
    ocr_region,
    _ends_sentence,
    _sentence_around,
    _ocr_wide,
    _pick_anchor,
    find_sentence,
)


_engine_cache = {"key": None, "val": None, "t": 0.0}


_NARROW = set("iljtfIr.,;:'\"!|()[]1 ")
_WIDE = set("mwMW@")


_rapid_run = None
_rapid_lock = threading.Lock()


LAST_OCR = {"count": 0, "time": 0.0}


# ------------------ повне речення навколо слова ------------------
_ABBR = {"mr", "mrs", "ms", "dr", "st", "vs", "jr", "sr", "prof", "mt"}


def open_file(path: str) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        log_exc('word_lookup.open_file')


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
import word_lookup_ui as _word_lookup_ui
_word_lookup_ui.configure(sys.modules[__name__])
from word_lookup_ui import (
    ScrolledPanel,
    screen_workarea,
    place_popup,
    Highlight,
    Popup,
    RegionSelector,
    FreeCursorOverlay,
    _get,
    _put,
    ReviewWindow,
    AppViewMixin,
)


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


class App(AppViewMixin):
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
            log_exc('word_lookup.run')

    # ---------- допоміжне для налаштувань ----------


    # ---------- інтерфейс ----------


    # ---------- статус, підказки ----------


    # ---------- збережені слова ----------


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
        word = correct_ocr_word(clean_word(self.test_var.get().strip())[0])
        if not word:
            return
        e = self.test_entry
        anchor = (e.winfo_rootx(), e.winfo_rooty(), e.winfo_rootx() + e.winfo_width(),
                  e.winfo_rooty() + e.winfo_height())
        token = self._new_token()

        @network_job(lambda: token != self.active_token)
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

    def retry_word(self, word, previous, anchor):
        """Correct OCR text without taking another screenshot or pausing the game."""
        word = correct_ocr_word(word)
        token = self._new_token()
        self.close_all()
        self.show_loading(anchor)
        preserved = {key: previous[key] for key in (
            "context", "source", "sentence", "sentence_tr", "_context_image", "image_file") if key in previous}

        def work():
            if token != self.active_token:
                return
            shown = False

            def partial(card):
                nonlocal shown
                if token == self.active_token:
                    shown = True
                    events.put(("show", {**card, **preserved}, None, anchor, token))

            try:
                card, _ = build_word_card_fast(word, partial, context=preserved.get("context", ""))
                if token != self.active_token:
                    return
                card = {**card, **preserved}
                definition = _first_definition_text(card)
                need_translation = bool(definition and not card.get("definition_tr")
                                        and cfg["sections"]["definitions"] and cfg["popup"]["translate_definition"])
                card["def_tr_pending"] = need_translation
                if shown:
                    events.put(("update", token, word, dict(card)))
                else:
                    events.put(("show", dict(card), None, anchor, token))
                if need_translation and token == self.active_token:
                    try:
                        card["definition_tr"] = translate_text(definition)
                    except Exception:
                        log_exc("Переклад визначення після виправлення OCR")
                    card["def_tr_pending"] = False
                    if token == self.active_token:
                        events.put(("update", token, word, card))
            except Exception:
                log_exc("Повторний пошук слова")
                post_lookup(token, "toast", f"Не вдалося знайти «{word}». Спробуй ще раз.")
        def run():
            with network_scope(lambda: token != self.active_token):
                work()
        threading.Thread(target=run, daemon=True).start()

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
        editing = hasattr(popup, "correction_row") and popup.correction_row.winfo_ismapped()
        draft = popup.word_var.get() if editing else None
        cursor = popup.word_editor.index("insert") if editing else 0
        popup.close()
        self.popup = Popup(self, card, anchor, exp, pinned=pinned)
        if editing:
            self.popup.edit_word()
            self.popup.word_var.set(draft)
            self.popup.word_editor.selection_clear()
            self.popup.word_editor.icursor(cursor)
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

    @cancellable_lookup
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
        hit = dict(hit)
        raw_word = hit["word"]
        hit["word"] = correct_ocr_word(raw_word)
        if raw_word != hit["word"]:
            hit["ocr_word"] = raw_word
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
                    threading.Thread(target=network_job(stale)(sentence_job), daemon=True).start()

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
                    log_exc('word_lookup.start_region_select')
            self._resume_game()
            self.toast(f"Не вдалося відкрити вибір слова: {e}")

    def _region_done(self, result, frozen=None):
        self.selector = None
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
                    log_exc('word_lookup.start_free_cursor')
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
            stop_input_listener(self._fc_mouse_listener)
            self._fc_mouse_listener = None
        self.manager.stop_raw_mode()

    def _free_cursor_done(self, pos, frozen=None):
        self._stop_free_cursor_input()
        self.free_cursor = None
        self._resume_game()
        if pos is not None:
            self.free_cursor_pos = pos
            # невелика затримка, щоб екран встиг перемалюватись після зникнення накладки;
            # root.after — щоб не блокувати головний потік Tk (на відміну від _region_thread,
            # який уже виконується у фоновому потоці)
            token = self._new_token()
            self.root.after(0, lambda: threading.Thread(
                target=self._lookup_thread, args=("word", pos[0], pos[1], token, frozen),
                daemon=True).start() if token == self.active_token else None)

    @cancellable_lookup
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
        core = correct_ocr_word(clean_word(words[0].text)[0]) if len(words) == 1 else ""
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

    @cancellable_lookup
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
            core = correct_ocr_word(text.strip(STRIP_CHARS))
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
            log_exc('word_lookup._copy_selection')
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
            # Poll exhaustion is expected; schedule the next tick below.
            ev = None
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
            if self.popup and hasattr(self.popup, "correction_row") and self.popup.correction_row.winfo_ismapped():
                self.popup.cancel_word_edit()
                return
            if time.monotonic() < getattr(self, "_word_edit_escape_until", 0):
                self._word_edit_escape_until = 0
                return
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
            log_exc('word_lookup.show_window')

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
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                log_exc("Завершення трея")
        for listener in (self.manager.listener, self.mouse_listener):
            stop_input_listener(listener)
        self.close_all()
        # Cancel each job through the widget that registered its Tcl command.
        # Cancelling a Toplevel job through root would leave a duplicate entry
        # in the child's command list and break destruction of that child.
        owners = {}
        def collect(widget):
            owners.update((name, widget) for name in (widget._tclCommands or ()))
            for child in widget.winfo_children():
                collect(child)
        collect(self.root)
        for timer in self.root.tk.call("after", "info"):
            script = self.root.tk.call("after", "info", timer)[0]
            command = self.root.tk.splitlist(script)[0]
            owner = owners.get(command)
            if owner is not None:
                owner.after_cancel(timer)
        self.root.destroy()
        self.kb = self.ms = None


def main():
    for recovery_status in recover_paused_games():
        log(recovery_status)
    if cfg.get("deepl", {}).get("key"):
        save_config(cfg)  # migrate legacy JSON credentials to the OS vault
    log(f"Запуск: {sys.platform}, Python {sys.version.split()[0]}")
    load_card_cache()
    App().run()


if __name__ == "__main__":
    main()
