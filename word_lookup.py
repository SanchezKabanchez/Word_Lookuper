"""Application composition and compatibility exports for existing integrations.

Production views/controllers use explicit domain objects. Importing this facade
creates only in-memory defaults; configuration and protected keys load in main.
"""
import word_lookup_platform as _platform
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
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import requests
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk
import tkinter.font as tkfont
import pyperclip
from wordlookup.network.budget import (REQUEST_GATE, TEXT_CACHE, LookupCancelled, check_cancelled,
                        correct_ocr_word as _explicit_ocr_word, network_scope, scoped_task, cancellable_lookup, network_job,
                        translation_scope, current_translation_language, scoped_task_until)
from pynput import keyboard, mouse
from word_lookup_profiles import (current_profile, normalize_game_profiles, profile_scope, profile_worker)
from wordlookup.network.policy import (NetworkDisabled, remote_request, mobile_translation,
                                 network_allowed as _network_allowed, LockedSession)
from word_lookup_lexicon import (IRREGULAR_FORMS, IRREGULAR_AMBIGUOUS,
                                 correct_ocr as _local_correct_ocr, base_forms as _local_base_forms,
                                 manual_word_scope, manual_word_active)
from word_lookup_paths import config_path
from word_lookup_version import __version__
from word_lookup_config import DEFAULT_CONFIG, merge_config as _merge, load_settings, atomic_json_write
from word_lookup_credentials import CredentialPreferences, safe_settings
from wordlookup.network.official import OfficialTranslators, ProviderRateLimited as RateLimited
from word_lookup_offline import OfflineTranslator
from word_lookup_models import (TranslationOutcome, LAST_TRANSLATOR, ServiceStatus, Notice,
    as_event, ScopedEvent, ShowCard, UpdateCard, LookupFailure, ShowLoading, HighlightReady,
    EscapePressed, FreeCursorKey, ActionRequested, RecordingStatus, MouseInput,
    DiagnosticReady, ControlStatus, ExportFinished, AnkiSyncFinished, TrayCommand)
from word_lookup_diagnostics import DiagnosticsRecorder, environment_info
from word_lookup_logging import AppLog, install_exception_hooks
from word_lookup_pause import GamePauseGuard, recover_paused_games
import word_lookup_storage as _word_lookup_storage
from word_lookup_preferences import Settings
from word_lookup_storage import Library
from word_lookup_cache import CardCache
from wordlookup.network.providers import Providers, TranslateError, DeeplError, HEADERS, UA_POS
from word_lookup_anki import Anki, AnkiConnectError
from word_lookup_selection import RegionSelector
from word_lookup_ocr import Ocr, OcrError
from word_lookup_geometry import W, WORD_RE, STRIP_CHARS
import word_lookup_ui as _word_lookup_ui
from word_lookup_ui import (
    ScrolledPanel,
    screen_workarea,
    place_popup,
    Highlight,
    Popup,
    FreeCursorOverlay,
    ReviewWindow,
    AppViewMixin,
)
import word_lookup_jobs as _word_lookup_jobs
from word_lookup_jobs import run_lookup_result
from word_lookup_support import Support
from word_lookup_application import Application
from word_lookup_compat import expose, install_compatibility
import word_lookup_input as _input
import importlib as _compat_importlib
from word_lookup_theme import (APP_NAME, FONT, KEY, ACTIONS, THEMES, THEME_NAMES, CARD_CONTENT_OPTIONS, LANG_NAMES, LANG_EN, MOUSE_BTN_NAMES, MOUSE_MOD_NAMES, ENGINE_NAMES, MOUSE_BTN_ALIASES, STATUS_LABELS, SAMPLE_CARD, AUDIO_OK, TRAY_OK)
from word_lookup_input import MOD_ORDER, MODS, MOD_KEYS, COPY_MOD, PRETTY, RESERVED
from word_lookup_storage import EDIT_FIELDS
from word_lookup_anki import _APKG_CSS
from word_lookup_geometry import _NARROW, _WIDE, _ABBR
from word_lookup_debug import diag_report as _diagnostic_ocr_report
from word_lookup_window_controller import make_tray_icon

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
cfg = copy.deepcopy(DEFAULT_CONFIG)
events = queue.Queue()
_token_counter = itertools.count(1)
_app_log = AppLog(lambda: _support.log_path(), lambda message: _support._mask_secrets(message))
_extra_credentials = CredentialPreferences(lambda: os.path.abspath(config_path()), _app_log.write)
_settings = Settings(cfg, lambda: config_path(), _app_log, writer=atomic_json_write, credentials=_extra_credentials)
_support = Support(_settings, _app_log)
_diagnostics = DiagnosticsRecorder(lambda: _settings.data, _support._mask_secrets)
_library = Library(_settings, _app_log)
_cache = CardCache(_settings, _app_log, TEXT_CACHE)
_providers = Providers(_settings, _cache, _app_log, _platform._game_source)
_ocr = Ocr(_settings, _diagnostics, _app_log)
_anki = Anki(_settings, _library, _providers, _app_log)


def log(*args):
    _app_log.write(" ".join(str(arg) for arg in args))


def log_exc(where="", exc_info=None):
    _app_log.exception(where, exc_info)


def post_lookup(token, kind, *args):
    events.put(as_event(("lookup_event", token, (kind, *args))))


def clamp(value, low, high):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, value))


def diag_report(dbg, hit, err, pos):
    return _diagnostic_ocr_report(_settings, dbg, hit, err, pos)


class App(Application):
    def __init__(self, instance=None):
        super().__init__(settings=_settings, cache=_cache, library=_library, providers=_providers,
            ocr=_ocr, anki=_anki, logger=_app_log, diagnostics=_diagnostics, support=_support,
            events=events, instance=instance)


def main(instance=None, *, startup_started=None):
    global cfg
    started = time.perf_counter() if startup_started is None else startup_started
    imported = time.perf_counter()
    from word_lookup_instance import SingleInstance
    from word_lookup_paths import data_dir
    own_instance = instance is None
    instance = instance or SingleInstance(data_dir())
    try:
        if own_instance and not instance.acquire():
            instance.request_activation()
            return
        install_exception_hooks(_app_log)
        cfg = _settings.load_config()
        _settings.data = cfg
        for recovery_status in recover_paused_games():
            log(recovery_status)
        if any(cfg.get(name, {}).get("key") for name in ("deepl", "googlecloud", "libretranslate")):
            _settings.save_config(cfg)
        configured = time.perf_counter()
        log(f"Запуск Word Lookup {__version__}: {sys.platform}, Python {sys.version.split()[0]}")
        _cache.load_card_cache()
        cached = time.perf_counter()
        application = App(instance=instance)
        reported = False
        def report_ready(event):
            nonlocal reported
            if event.widget is application.root and not reported:
                reported = True
                def record():
                    log(f"Вікно готове за {time.perf_counter() - started:.3f} с: "
                        f"імпорт {imported - started:.3f}, "
                        f"налаштування/відновлення {configured - imported:.3f}, "
                        f"кеш {cached - configured:.3f}")
                application.root.after_idle(record)
        application.root.bind("<Map>", report_ready, add=True)
        if _settings.notice:
            application.toast(_settings.notice, timeout=8)
        application.run()
    finally:
        if own_instance:
            instance.close()

_COMPAT_BINDINGS = {}
expose(globals(), _COMPAT_BINDINGS, _settings, ('load_config', 'save_config', '_key_digest'))
expose(globals(), _COMPAT_BINDINGS, _cache, ('_cache_path', 'load_card_cache', 'cache_disk_kb', 'save_card_cache', 'clear_card_cache', 'cached_word_card', '_clean_context', '_cache_key', '_cache_word_card', '_dictionary_required', '_dictionary_fetch_enabled', '_dictionary_sources', '_cached_card_for_lookup'))
expose(globals(), _COMPAT_BINDINGS, _library, ('_saved_words_path', '_file_revision', '_read_saved_file', 'read_saved_words_file', 'saved_words_error', 'load_saved_words', 'reload_saved_words', '_adopt_saved_words', '_remember_saved_revision', '_preserve_before_restore', 'restore_saved_words', '_saved_backups', '_backup_image_names', 'context_image_in_use', '_prune_saved_backups', '_write_saved_words', 'saved_entry', '_entry_complete', 'is_word_saved', '_card_example', '_card_definition', '_clean_sentence', '_anki_tag', '_common_prefix', 'align_word_span', 'split_around_word', '_first_definition_text', '_card_primary_translation', 'primary_translations', 'add_saved_word', 'remove_saved_word', 'filter_saved_words', 'saved_word_card', 'update_saved_entry', 'merge_saved_enrichments', 'review_due', 'record_review', 'context_image_path', 'save_context_image', 'context_photo'))
_COMPAT_BINDINGS['cfg'] = [(_settings, 'data')]
_COMPAT_BINDINGS['atomic_json_write'] = [(_settings, 'writer')]
_COMPAT_BINDINGS['_credential_state'] = [(_settings, 'credential_state')]
_COMPAT_BINDINGS['_credential_status'] = [(_settings, 'credential_status')]
_COMPAT_BINDINGS['_config_file_state'] = [(_settings, 'file_state')]
_COMPAT_BINDINGS['_configuration_notice'] = [(_settings, 'notice')]
_COMPAT_BINDINGS['_extra_credentials'] = [(_settings, 'credentials')]
_COMPAT_BINDINGS['_card_cache'] = [(_cache, 'cards')]
_COMPAT_BINDINGS['_cache_file_state'] = [(_cache, 'file_state')]
_COMPAT_BINDINGS['_cache_lock'] = [(_cache, 'lock')]
_COMPAT_BINDINGS['_cache_new'] = [(_cache, 'new_count')]
_COMPAT_BINDINGS['_card_cache_generation'] = [(_cache, 'generation')]
_COMPAT_BINDINGS['_saved_words'] = [(_library, 'words')]
_COMPAT_BINDINGS['_saved_words_state'] = [(_library, 'state')]
expose(globals(), _COMPAT_BINDINGS, _providers, ('_target_language', 'pos_name', 'network_allowed', '_remote_get', '_remote_post', '_http_json', '_max_tr', '_p_gtx', '_p_chrome', '_p_deep', '_chunks', '_p_mymemory', '_p_argos', 'provider_cooldown', 'set_provider_cooldown', 'clear_provider_cooldown', 'translate_with_fallback', 'correct_ocr_word', 'base_form_candidates', '_datamuse_get', 'datamuse_dictionary', '_freedictionary_lookup', '_datamuse_spellcheck', '_looks_like_form_of', 'english_dictionary', '_english_lookup', '_deepl_configured', '_deepl_active', '_deepl_base', '_deepl_headers', 'deepl_translate', 'deepl_usage', '_try_deepl', 'translate_text', 'clean_hud_line', 'text_card', '_google_word', '_translate_word', 'build_word_card', 'build_word_card_fast', 'play_audio'))
expose(globals(), _COMPAT_BINDINGS, _ocr, ('_default_tesseract', '_configure_tesseract_cmd', 'tesseract_ready', 'rapid_ready', 'pick_engine', '_pick_engine_uncached', 'ocr_tesseract', '_char_w', 'split_line_words', '_speed_up_rapid', '_rapid_engine', '_line_gaps', '_gap_segments', '_ctc_words', 'ocr_rapid', 'pick_word', 'clean_word', '_grab', '_attempts', '_run_ocr', '_find_word_gap', '_reocr_crop', '_split_merged_word', '_find_word_at', 'find_word_at', 'cluster_lines', 'ocr_region', '_ends_sentence', '_sentence_around', '_ocr_wide', '_pick_anchor', 'find_sentence', '_sentence_from_words', 'ocr_settings'))
expose(globals(), _COMPAT_BINDINGS, _anki, ('_snapshot_entries', '_anki_field', '_anki_back', 'export_anki_txt', '_needs_enrich', 'enrich_entry', 'enrich_entries', 'apkg_available', '_emphasize', '_anki_html_fields', '_entry_tag', '_anki_identity', 'anki_call', '_upgrade_anki_tts', 'sync_anki_entries', 'export_anki_apkg'))
_COMPAT_BINDINGS['SESSION'] = [(_providers, 'session')]
globals()['SESSION'] = _providers.session
_COMPAT_BINDINGS['_PROVIDERS'] = [(_providers, 'chain')]
globals()['_PROVIDERS'] = _providers.chain
_COMPAT_BINDINGS['_cooldown'] = [(_providers, 'cooldowns')]
globals()['_cooldown'] = _providers.cooldowns
_COMPAT_BINDINGS['_official_translators'] = [(_providers, 'official')]
globals()['_official_translators'] = _providers.official
_COMPAT_BINDINGS['_offline_translator'] = [(_providers, 'offline')]
globals()['_offline_translator'] = _providers.offline
_COMPAT_BINDINGS['_engine_cache'] = [(_ocr, 'engine_cache')]
globals()['_engine_cache'] = _ocr.engine_cache
_COMPAT_BINDINGS['_rapid_run'] = [(_ocr, 'rapid_run')]
globals()['_rapid_run'] = _ocr.rapid_run
_COMPAT_BINDINGS['_rapid_lock'] = [(_ocr, 'rapid_lock')]
globals()['_rapid_lock'] = _ocr.rapid_lock
_COMPAT_BINDINGS['LAST_OCR'] = [(_ocr, 'last')]
globals()['LAST_OCR'] = _ocr.last

expose(globals(), _COMPAT_BINDINGS, _platform, ('_foreground_window', '_restore_foreground', 'release_cursor_clip', '_foreground_title', '_game_source', 'windows_exe_path', 'non_game_process', 'registered_game_paths', 'game_detection_reason', 'foreground_process_info', 'foreground_game', 'open_file'))
expose(globals(), _COMPAT_BINDINGS, _input, ('pretty', 'combo_problem', 'norm_key', 'stop_input_listener', 'HotkeyManager'))
expose(globals(), _COMPAT_BINDINGS, _support, ('log_path', '_mask_secrets', 'read_log_tail', 'clear_log', 'diagnostic_report'))
_COMPAT_BINDINGS['_last_game_detection'] = [(_platform, '_last_game_detection')]
_COMPAT_BINDINGS['_game_registry_cache'] = [(_platform, '_game_registry_cache')]
for _module_name in ['wordlookup.network.providers', 'word_lookup_anki', 'word_lookup_ocr', 'word_lookup_geometry', 'word_lookup_application', 'word_lookup_search_controller', 'word_lookup_input_controller', 'word_lookup_pause_controller', 'word_lookup_event_controller', 'word_lookup_window_controller', 'word_lookup_ui', 'word_lookup_selection']:
    _module = _compat_importlib.import_module(_module_name)
    for _name in ("ThreadPoolExecutor", "GamePauseGuard", "RegionSelector", "FreeCursorOverlay", "Popup", "Highlight", "run_lookup_result"):
        if hasattr(_module, _name):
            _COMPAT_BINDINGS.setdefault(_name, []).append((_module, _name))
_COMPAT_BINDINGS['_get'] = [(_settings, 'get')]
_COMPAT_BINDINGS['_put'] = [(_settings, 'put')]
install_compatibility(sys.modules[__name__], _COMPAT_BINDINGS)

__all__ = ['ACTIONS', 'APP_NAME', 'AUDIO_OK', 'ActionRequested', 'Anki', 'AnkiConnectError', 'AnkiSyncFinished', 'AppLog', 'AppViewMixin', 'Application', 'CARD_CONTENT_OPTIONS', 'COPY_MOD', 'CardCache', 'ControlStatus', 'CredentialPreferences', 'DEFAULT_CONFIG', 'DeeplError', 'DiagnosticReady', 'DiagnosticsRecorder', 'EDIT_FIELDS', 'ENGINE_NAMES', 'EscapePressed', 'ExportFinished', 'FONT', 'FreeCursorKey', 'FreeCursorOverlay', 'GamePauseGuard', 'HEADERS', 'Highlight', 'HighlightReady', 'IRREGULAR_AMBIGUOUS', 'IRREGULAR_FORMS', 'KEY', 'LANG_EN', 'LANG_NAMES', 'LAST_TRANSLATOR', 'Library', 'LockedSession', 'LookupCancelled', 'LookupFailure', 'MODS', 'MOD_KEYS', 'MOD_ORDER', 'MOUSE_BTN_ALIASES', 'MOUSE_BTN_NAMES', 'MOUSE_MOD_NAMES', 'MouseInput', 'NetworkDisabled', 'Notice', 'Ocr', 'OcrError', 'OfficialTranslators', 'OfflineTranslator', 'PRETTY', 'Popup', 'Providers', 'REQUEST_GATE', 'RESERVED', 'RateLimited', 'RecordingStatus', 'RegionSelector', 'ReviewWindow', 'SAMPLE_CARD', 'STATUS_LABELS', 'STRIP_CHARS', 'ScopedEvent', 'ScrolledPanel', 'ServiceStatus', 'Settings', 'ShowCard', 'ShowLoading', 'Support', 'TEXT_CACHE', 'THEMES', 'THEME_NAMES', 'TRAY_OK', 'ThreadPoolExecutor', 'TranslateError', 'TranslationOutcome', 'TrayCommand', 'UA_POS', 'UpdateCard', 'W', 'WORD_RE', '_ABBR', '_APKG_CSS', '_NARROW', '_WIDE', '__version__', '_compat_importlib', '_diagnostic_ocr_report', '_explicit_ocr_word', '_input', '_local_base_forms', '_local_correct_ocr', '_merge', '_network_allowed', '_platform', '_word_lookup_jobs', '_word_lookup_storage', '_word_lookup_ui', 'as_completed', 'as_event', 'atomic_json_write', 'base64', 'cancellable_lookup', 'check_cancelled', 'colorchooser', 'config_path', 'copy', 'current_profile', 'current_translation_language', 'datetime', 'environment_info', 'expose', 'filedialog', 'hashlib', 'html', 'importlib', 'install_compatibility', 'install_exception_hooks', 'io', 'itertools', 'json', 'keyboard', 'load_settings', 'make_tray_icon', 'manual_word_active', 'manual_word_scope', 'math', 'messagebox', 'mobile_translation', 'mouse', 'namedtuple', 'network_job', 'network_scope', 'normalize_game_profiles', 'ntpath', 'os', 'place_popup', 'profile_scope', 'profile_worker', 'pyperclip', 'queue', 're', 'recover_paused_games', 'remote_request', 'requests', 'run_lookup_result', 'safe_settings', 'scoped_task', 'scoped_task_until', 'screen_workarea', 'subprocess', 'sys', 'tempfile', 'threading', 'time', 'timedelta', 'timezone', 'tk', 'tkfont', 'traceback', 'translation_scope', 'ttk']

if __name__ == "__main__":
    main()
