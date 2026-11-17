"""Game-profile snapshots across UI actions, worker threads and saved cards."""
import copy
import threading
import time
from types import SimpleNamespace

import pytest
from PIL import Image

import word_lookup as w
from word_lookup_profiles import normalize_game_profiles


PROFILE_A = {'selection_mode': 'keyboard', 'region_w': 900, 'region_h': 180,
             'pause_game': False, 'anki_tag': 'Game_A', 'source': 'Captured game A', 'exe': 'gamea.exe'}
PROFILE_B = {**PROFILE_A, 'region_w': 500, 'anki_tag': 'Game_B', 'source': 'Captured game B', 'exe': 'gameb.exe'}


def wait_for(root, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), 'Profile lookup did not settle before timeout'


def test_profile_normalization_accepts_windows_case_and_drops_invalid_entries():
    cleaned = normalize_game_profiles({'GameA.EXE': PROFILE_A, '../bad.exe': PROFILE_A,
                                      'wrong.exe': {**PROFILE_A, 'region_w': True}})
    assert set(cleaned) == {'gamea.exe'}
    assert cleaned['gamea.exe']['region_w'] == 900
    assert 'source' not in cleaned['gamea.exe']


def test_capture_confirms_foreground_exe_then_snapshots_profile(app, monkeypatch):
    w.cfg['game_profiles'] = {'gamea.exe': copy.deepcopy(PROFILE_A)}
    calls = []
    monkeypatch.setattr(w, 'foreground_process_info', lambda: {'name': 'GameA.EXE'})
    def confirm(name):
        calls.append(name)
        w._last_game_detection = {'name': name, 'title': 'Verified title'}
        return 1234, 100, 99
    monkeypatch.setattr(w, 'foreground_game', confirm)
    app._prepare_game_profile()
    w.cfg['game_profiles']['gamea.exe']['region_w'] = 600
    assert calls == ['GameA.EXE']
    assert app._lookup_profile['source'] == 'Verified title'
    assert app._lookup_profile['region_w'] == 900
    monkeypatch.setattr(w, 'foreground_process_info', lambda: {'name': 'other.exe'})
    app._prepare_game_profile()
    assert app._lookup_profile == {}
    assert len(calls) == 1


def test_failed_foreground_confirmation_discards_previous_profile(app, monkeypatch):
    app._lookup_profile = copy.deepcopy(PROFILE_A)
    w.cfg['game_profiles'] = {'gamea.exe': PROFILE_A}
    monkeypatch.setattr(w, 'foreground_process_info', lambda: {'name': 'gamea.exe'})
    def changed(*args):
        raise RuntimeError('Foreground changed')
    monkeypatch.setattr(w, 'foreground_game', changed)
    app._prepare_game_profile()
    assert app._lookup_profile == {}


def test_profile_can_disable_global_pause_without_touching_a_process(app, monkeypatch):
    w.cfg['freeze']['pause_game'] = True
    app._lookup_profile = dict(PROFILE_A, pause_game=False)
    def forbidden(*args):
        raise AssertionError('Profile explicitly disables suspension')
    monkeypatch.setattr(w, 'foreground_game', forbidden)
    monkeypatch.setattr(w, 'GamePauseGuard', forbidden)
    assert app._begin_game_pause()
    assert app._game_pause is None


def test_profile_can_enable_pause_only_for_its_confirmed_exe(app, monkeypatch):
    w.cfg['freeze'].update(pause_game=False, auto_game=False, game_exe='unrelated.exe')
    app._lookup_profile = dict(PROFILE_A, pause_game=True)
    calls = []
    monkeypatch.setattr(w, 'foreground_game', lambda name: (calls.append(name) or (1234, 100, 99)))
    monkeypatch.setattr(app, '_display_game_detection', lambda: None)
    class Guard:
        active = False
        def __init__(self, *args):
            calls.append(args)
        def start(self):
            self.active = True
        def release(self):
            self.active = False
            return True, 'RESUMED'
    monkeypatch.setattr(w, 'GamePauseGuard', Guard)
    assert app._begin_game_pause()
    assert calls == ['gamea.exe', (1234, 100, w.cfg['freeze']['pause_seconds'])]
    assert app._game_pause.active
    assert app._resume_game()


def test_worker_uses_token_snapshot_and_restores_outer_context(app, monkeypatch):
    app._lookup_profile = copy.deepcopy(PROFILE_A)
    token = app._new_token()
    app._lookup_profile = copy.deepcopy(PROFILE_B)
    observed = []
    monkeypatch.setattr(w, '_foreground_title', lambda: 'Different current window')
    def find(*args, **kwargs):
        observed.append((w.ocr_settings()['region_w'], w._game_source()))
        return None
    monkeypatch.setattr(w, 'find_word_at', find)
    with w.profile_scope({'source': 'Outer scope'}):
        app._lookup_thread('word', 200, 200, token, frozen=(object(), 0, 0))
        assert w.current_profile()['source'] == 'Outer scope'
    assert observed == [(900, 'Captured game A')]
    assert w.current_profile() == {}


def test_sentence_child_thread_inherits_lookup_profile(app, card, monkeypatch):
    app._lookup_profile = copy.deepcopy(PROFILE_A)
    token = app._new_token()
    w.cfg['popup'].update(sentence=True, sentence_translation=False, translate_definition=False)
    observed, ready = [], threading.Event()
    def sentence(*args):
        observed.append((w.ocr_settings()['region_w'], w.current_profile().get('exe')))
        ready.set()
        return 'Hello wonderful world.'
    monkeypatch.setattr(w, 'find_sentence', sentence)
    monkeypatch.setattr(w, 'build_word_card_fast', lambda *a, **kw: (copy.deepcopy(card), False))
    app._lookup_thread('word', 200, 200, token, frozen=(object(), 0, 0),
                       pre_hit={'word': 'hello', 'box': (10, 10, 70, 30), 'line_text': 'Hello world'})
    assert ready.wait(2)
    assert observed == [(900, 'gamea.exe')]


@pytest.mark.parametrize('kind', ['region', 'free_cursor'])
def test_overlay_result_uses_frame_profile_after_another_action_changes_game(app, monkeypatch, kind):
    app._lookup_profile = copy.deepcopy(PROFILE_B)
    frozen = (Image.new('RGB', (1280, 800)), 0, 0)
    observed, ready = [], threading.Event()
    @w.profile_worker
    def worker(self, *args, token=None, **kwargs):
        # The real methods accept token positionally; wrappers below name it.
        observed.append(w.current_profile())
        ready.set()
    if kind == 'region':
        app.selector = SimpleNamespace(profile=copy.deepcopy(PROFILE_A))
        def run(result, frame, token):
            worker(app, token=token)
        monkeypatch.setattr(app, '_region_thread', run)
        app._region_done(('point', 200, 200), frozen)
    else:
        app.free_cursor = SimpleNamespace(profile=copy.deepcopy(PROFILE_A))
        def run(mode, x, y, token, frame):
            worker(app, token=token)
        monkeypatch.setattr(app, '_lookup_thread', run)
        app._free_cursor_done((200, 200), frozen)
    wait_for(app.root, ready.is_set)
    assert observed == [PROFILE_A]
    assert app._lookup_profile == PROFILE_B


def test_correcting_saved_card_keeps_original_tag_after_other_game_lookup(app, card, monkeypatch):
    previous = dict(card, word='changin', anki_tag='Game_A', source='Captured game A')
    app._lookup_profile = copy.deepcopy(PROFILE_B)
    w.cfg['popup']['translate_definition'] = False
    def build(word, partial, context=''):
        return dict(card, word=word), False
    monkeypatch.setattr(w, 'build_word_card_fast', build)
    app.retry_word('changing', previous, (200, 200, 300, 230))
    wait_for(app.root, lambda: app.popup and app.popup.card.get('word') == 'changing')
    assert app.popup.card['anki_tag'] == 'Game_A'
    assert app.popup.card['source'] == 'Captured game A'
    assert app._lookup_profile == PROFILE_B


def test_open_and_tk_escape_invalidate_results_from_before_the_frozen_frame(app, card, monkeypatch):
    previous = app._new_token()
    monkeypatch.setattr(w, '_grab', lambda *a, **kw: (Image.new('RGB', (1280, 800)), 0, 0))
    monkeypatch.setattr(w, 'ocr_region', lambda *a, **kw: [])
    app.start_region_select()
    overlay = app.selector
    assert overlay is not None
    try:
        app.root.update()
        assert app.active_token != previous
        overlay.win.event_generate('<Escape>')
        wait_for(app.root, lambda: app.selector is None)
        assert app.manager.raw_mode_cb is None
        app._handle(('show', card, None, None, previous))
        assert app.popup is None
    finally:
        overlay.finish(None)
