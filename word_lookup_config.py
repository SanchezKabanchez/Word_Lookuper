"""Validated settings and migrations, with no GUI or credential I/O on import."""
import copy
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import tempfile
import uuid
from typing import Any

from word_lookup_profiles import normalize_game_profiles

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 9,
    "hotkeys": {
        "lookup_word": "ctrl+alt+d", "select_region": "ctrl+alt+r", "free_cursor": "ctrl+alt+g",
        "unlock_cursor": "ctrl+alt+u", "translate_line": "ctrl+alt+l", "translate_selection": "ctrl+alt+t",
        "speak": "ctrl+alt+s", "copy_translation": "", "toggle_pause": "", "close_popup": "",
        "resume_game": "ctrl+alt+p",
    },
    "mouse": {"button": "middle", "modifier": "none"},
    "languages": {"target": "uk"},
    "sections": {"bilingual": True, "definitions": True, "thesaurus": False},
    "dictionary": {"max_translations": 8, "max_definitions": 1, "max_synonyms": 4},
    "ocr": {"engine": "auto", "tesseract_cmd": "", "region_w": 480, "region_h": 100,
            "upscale": 2.0, "highlight": True, "split_merged_words": True, "cpu_threads": 2,
            "max_side": 1600, "tesseract_timeout": 8},
    "performance": {"hover_delay_ms": 250, "capture_active_monitor": True, "idle_poll_ms": 120},
    "popup": {"theme": "dark", "font_size": 10, "width": 400, "opacity": 1.0, "timeout": 60,
              "highlight_color": "#ffd43b", "btn_speak": True, "sentence": True,
              "sentence_translation": True, "translate_definition": True, "phonetic": True,
              "examples": False, "alternate_translations": False, "context_image": False},
    "cache": {"max_entries": 1000}, "deepl": {"enabled": True, "key": ""},
    "network": {"offline": False, "providers": {"google": True, "mymemory": True,
                "datamuse": True, "dictionaryapi": True, "deepl": True,
                "googlecloud": False, "libretranslate": False}},
    "googlecloud": {"key": ""},
    "libretranslate": {"url": "", "key": ""},
    "offline_translation": {"enabled": False, "cpu_threads": 2},
    "diagnostics": {"enabled": False, "include_ocr_image": False},
    "game_profiles": {}, "anki": {"game": "", "auto_game_tag": True, "deck": "Word Lookup"},
    "freeze": {"selection_mode": "keyboard", "hover_highlight": True, "pause_game": False,
               "auto_game": True, "game_exe": "", "pause_seconds": 60, "pause_mode": "lookup"},
    "auto": {"enabled": False, "drag_px": 12, "double_click_s": 0.35},
    "app": {"start_minimized": False}, "learning": {"capture_context": False},
}

LIMITS = {
    ("dictionary", "max_translations"): (1, 20), ("dictionary", "max_definitions"): (1, 6),
    ("dictionary", "max_synonyms"): (1, 20), ("ocr", "region_w"): (200, 2000),
    ("ocr", "region_h"): (40, 600), ("ocr", "upscale"): (1., 4.), ("ocr", "cpu_threads"): (1, 4),
    ("ocr", "max_side"): (640, 3200), ("ocr", "tesseract_timeout"): (1, 30),
    ("performance", "hover_delay_ms"): (100, 1000), ("performance", "idle_poll_ms"): (80, 500),
    ("popup", "font_size"): (9, 24), ("popup", "width"): (300, 900),
    ("popup", "opacity"): (.4, 1.), ("popup", "timeout"): (0, 300),
    ("cache", "max_entries"): (0, 5000), ("freeze", "pause_seconds"): (5, 300),
    ("auto", "drag_px"): (2, 100), ("auto", "double_click_s"): (.1, 1.),
    ("offline_translation", "cpu_threads"): (1, 4),
}
CHOICES = {
    ("mouse", "button"): {"none", "middle", "x1", "x2"},
    ("mouse", "modifier"): {"none", "ctrl", "alt", "shift"},
    ("languages", "target"): {"uk", "pl", "de", "fr", "es", "it", "cs", "tr"},
    ("ocr", "engine"): {"auto", "rapidocr", "tesseract"},
    ("popup", "theme"): {"dark", "light", "sepia"},
    ("freeze", "selection_mode"): {"keyboard", "hover"},
    ("freeze", "pause_mode"): {"lookup", "snapshot"},
}


def merge_config(base: dict, saved: dict, path: tuple = ()) -> None:
    for key, default in base.items():
        if key not in saved:
            continue
        value, field = saved[key], path + (key,)
        if isinstance(default, dict):
            if isinstance(value, dict):
                merge_config(default, value, field)
            continue
        if type(default) is float:
            valid = type(value) in (int, float) and math.isfinite(value)
            if valid:
                value = float(value)
        else:
            valid = type(value) is type(default)
        if not valid or (field in CHOICES and value not in CHOICES[field]):
            continue
        if field == ("popup", "highlight_color") and not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            continue
        if field in LIMITS:
            low, high = LIMITS[field]
            value = min(high, max(low, value))
        base[key] = value


def migrate_v1_to_v2(saved: dict) -> None:
    if isinstance(saved.get("ocr"), dict):
        for field in ("region_w", "region_h", "upscale"):
            saved["ocr"].pop(field, None)


def migrate_v2_to_v3(saved: dict) -> None:
    if isinstance(saved.get("popup"), dict) and saved["popup"].get("timeout") == 0:
        saved["popup"].pop("timeout", None)


def migrate_v3_to_v4(saved: dict) -> None:
    popup = saved.get("popup")
    if isinstance(popup, dict):
        if popup.get("font_size") == 12:
            popup["font_size"] = 10
        if "sentence_translation" not in popup and type(popup.get("sentence")) is bool:
            popup["sentence_translation"] = popup["sentence"]


def migrate_v4_to_v5(saved: dict) -> None:
    if isinstance(saved.get("freeze"), dict) and saved["freeze"].get("pause_seconds") == 120:
        saved["freeze"]["pause_seconds"] = 20


MIGRATIONS = ((2, migrate_v1_to_v2), (3, migrate_v2_to_v3),
              (4, migrate_v3_to_v4), (5, migrate_v4_to_v5))


@dataclass(frozen=True)
class LoadedSettings:
    values: dict
    writable: bool = True
    notice: str = ""


def load_settings(path: str, defaults: dict | None = None) -> LoadedSettings:
    defaults = DEFAULT_CONFIG if defaults is None else defaults
    config, file = copy.deepcopy(defaults), Path(path)
    try:
        with file.open(encoding="utf-8-sig") as stream:
            saved = json.load(stream)
        if not isinstance(saved, dict):
            raise ValueError("Invalid configuration root")
    except FileNotFoundError:
        return LoadedSettings(config)
    except (ValueError, UnicodeError):
        quarantine = file.with_name(file.name + ".corrupt")
        if quarantine.exists():
            quarantine = file.with_name(file.name + ".corrupt." + uuid.uuid4().hex)
        try:
            file.rename(quarantine)
        except OSError:
            return LoadedSettings(config, False, "Конфіг пошкоджений; файл збережено, але запис заблоковано.")
        return LoadedSettings(config, True, f"Пошкоджений конфіг збережено як {quarantine.name}.")
    except OSError:
        return LoadedSettings(config, False, "Не вдалося прочитати конфіг; запис заблоковано, щоб зберегти дані.")
    version = saved.get("version", 1)
    version = version if type(version) is int else 1
    if version > defaults["version"]:
        return LoadedSettings(config, False, "Конфіг створено новішою версією; цей файл не буде перезаписано.")
    for target, migrate in MIGRATIONS:
        if version < target:
            migrate(saved)
    saved["version"] = defaults["version"]
    merge_config(config, saved)
    config["game_profiles"] = normalize_game_profiles(saved.get("game_profiles", {}))
    hotkeys = saved.get("hotkeys", {})
    if isinstance(hotkeys, dict) and "resume_game" not in hotkeys:
        resume = config["hotkeys"]["resume_game"]
        if resume and any(combo == resume for action, combo in config["hotkeys"].items() if action != "resume_game"):
            config["hotkeys"]["resume_game"] = ""
    return LoadedSettings(config)


def atomic_json_write(path: str, data, indent: int | None = 2) -> None:
    directory = Path(path).absolute().parent
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".wordlookup-", delete=False) as stream:
            temporary = stream.name
            json.dump(data, stream, ensure_ascii=False, indent=indent, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)
