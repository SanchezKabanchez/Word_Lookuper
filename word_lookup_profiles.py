"""Validated game settings and immutable per-lookup profile context."""
import copy
from contextlib import contextmanager
from contextvars import ContextVar
import functools
import inspect
import ntpath
from typing import Any


_profile: ContextVar[dict[str, Any] | None] = ContextVar("word_lookup_game_profile", default=None)


def normalize_profile(name, values):
    name = str(name).strip().casefold()
    if (not name.endswith(".exe") or len(name) <= 4 or ntpath.basename(name) != name
            or ntpath.splitdrive(name)[0] or any(c in name for c in '/\\:*?"<>|')):
        raise ValueError("Укажи назву .exe без шляху, наприклад Cyberpunk2077.exe")
    if not isinstance(values, dict):
        raise ValueError("Неправильний формат профілю")
    mode = values.get("selection_mode", "keyboard")
    if mode not in ("keyboard", "hover"):
        raise ValueError("Вибери клавіатуру або наведення")
    result = {"selection_mode": mode}
    for key, default, low, high in (("region_w", 480, 200, 2000), ("region_h", 100, 40, 600)):
        value = values.get(key, default)
        if isinstance(value, bool):
            raise ValueError("Розмір OCR має бути числом")
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("Розмір OCR має бути цілим числом") from None
        if str(value).strip() != str(number) or not low <= number <= high:
            raise ValueError(f"{key}: допустимо {low}–{high} px")
        result[key] = number
    pause = values.get("pause_game", False)
    if not isinstance(pause, bool):
        raise ValueError("Параметр паузи має бути перемикачем")
    result["pause_game"] = pause
    result["anki_tag"] = str(values.get("anki_tag", "")).strip()[:80]
    return name, result


def normalize_game_profiles(data):
    profiles = {}
    if isinstance(data, dict):
        for name, values in data.items():
            try:
                key, profile = normalize_profile(name, values)
                profiles[key] = profile
            except ValueError:
                continue
    return profiles


def current_profile():
    return _profile.get() or {}


@contextmanager
def profile_scope(profile):
    token = _profile.set(copy.deepcopy(profile or {}))
    try:
        yield
    finally:
        _profile.reset(token)


def profile_worker(fn):
    """Use the profile captured when the UI issued this lookup's token."""
    signature = inspect.signature(fn)
    @functools.wraps(fn)
    def run(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        token = bound.arguments.get("token")
        profile = getattr(self, "_lookup_profiles", {}).get(token, getattr(self, "_lookup_profile", {}))
        with profile_scope(profile):
            return fn(self, *args, **kwargs)
    return run
