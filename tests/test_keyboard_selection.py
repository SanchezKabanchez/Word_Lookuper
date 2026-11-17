"""Keyboard-only frozen-screen selection, without a game or live translation."""
import threading
import time

from PIL import Image

import word_lookup as w
from word_lookup_selection import WordNavigator


def sample_words():
    # Shuffled OCR output and unrelated engine line IDs exercise visual grouping.
    return [w.W('world!', 180, 60, 245, 80, 7),
            w.W('Hello', 10, 10, 70, 30, 8),
            w.W('wonderful', 95, 10, 205, 30, 9),
            w.W('The', 10, 60, 45, 80, 1),
            w.W('...', 60, 60, 80, 80, 2)]


def wait_for(root, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), 'Overlay state did not settle before timeout'


def test_visual_reading_order_and_nearest_word_between_uneven_lines():
    nav = WordNavigator(sample_words())
    assert nav.current['word'] == 'Hello'
    assert nav.move('left')['word'] == 'Hello'
    assert nav.move('right')['word'] == 'wonderful'
    assert nav.move('down')['word'] == 'world'
    assert nav.current['line_text'] == 'The ... world!'
    assert nav.move('down')['word'] == 'world'
    assert nav.move('up')['word'] == 'wonderful'
    assert nav.move('left')['word'] == 'Hello'
    assert nav.move('down')['word'] == 'The'


def test_empty_navigation_is_safe():
    nav = WordNavigator([w.W('123', 0, 0, 10, 10, 0)])
    assert nav.current is None
    assert nav.move('up') is None


def test_global_keys_select_from_one_frozen_capture_without_mouse(app, monkeypatch):
    image = Image.new('RGB', (1280, 800), '#111318')
    captures, scans, completed = [], [], []
    w.cfg['freeze']['selection_mode'] = 'keyboard'
    app._lookup_profile = {'selection_mode': 'keyboard', 'region_w': 720}

    def grab(*args, **kwargs):
        captures.append(args)
        return image, 0, 0

    def scan(*args, **kwargs):
        scans.append((kwargs['frozen'], w.ocr_settings()['region_w']))
        return sample_words()

    def mouse_forbidden():
        raise AssertionError('Keyboard selection must not manipulate the captured game cursor')

    monkeypatch.setattr(w, '_grab', grab)
    monkeypatch.setattr(w, 'ocr_region', scan)
    monkeypatch.setattr(w, 'release_cursor_clip', mouse_forbidden)
    overlay = w.RegionSelector(app, lambda *args: completed.append(args))
    try:
        wait_for(app.root, lambda: overlay.navigator is not None)
        assert overlay.hover_hit['word'] == 'Hello'
        for key, expected in [(w.keyboard.Key.right, 'wonderful'), (w.keyboard.Key.down, 'world')]:
            app.manager._on_press(key)
            app.manager._on_release(key)
            wait_for(app.root, lambda: overlay.hover_hit['word'] == expected)
        app.manager._on_press(w.keyboard.Key.enter)
        app.manager._on_release(w.keyboard.Key.enter)
        wait_for(app.root, lambda: bool(completed))
        result, frozen = completed[0]
        assert result[0] == 'word' and result[3]['word'] == 'world'
        assert result[3]['line_text'] == 'The ... world!'
        assert frozen is overlay.frozen
        assert scans == [(frozen, 720)]
        assert len(captures) == 1
        assert app.manager.raw_mode_cb is None
    finally:
        overlay.finish(None)


def test_escape_cancels_during_ocr_and_late_result_cannot_reopen_overlay(app, monkeypatch):
    blocked, release = threading.Event(), threading.Event()
    completed = []
    monkeypatch.setattr(w, '_grab', lambda *a, **kw: (Image.new('RGB', (1280, 800)), 0, 0))

    def scan(*args, **kwargs):
        blocked.set()
        assert release.wait(3)
        return sample_words()

    monkeypatch.setattr(w, 'ocr_region', scan)
    overlay = w.RegionSelector(app, lambda *args: completed.append(args))
    try:
        assert blocked.wait(2)
        app.manager._on_press(w.keyboard.Key.esc)
        app.manager._on_release(w.keyboard.Key.esc)
        wait_for(app.root, lambda: overlay.done)
        assert completed == [(None, overlay.frozen)]
        assert app.manager.raw_mode_cb is None
        release.set()
        overlay._poll()
        assert completed == [(None, overlay.frozen)]
    finally:
        release.set()
        overlay.finish(None)


def test_hover_profile_keeps_pointer_overlay_and_does_not_scan_full_screen(app, monkeypatch):
    app._lookup_profile = {'selection_mode': 'hover'}
    monkeypatch.setattr(w, '_grab', lambda *a, **kw: (Image.new('RGB', (1280, 800)), 0, 0))
    def no_scan(*args, **kwargs):
        raise AssertionError('Hover mode should defer OCR until pointer movement')
    monkeypatch.setattr(w, 'ocr_region', no_scan)
    overlay = w.RegionSelector(app, lambda *args: None)
    try:
        assert not overlay.keyboard_enabled
        assert overlay.hover_enabled
        assert app.manager.raw_mode_cb is None
    finally:
        overlay.finish(None)


def test_resume_game_shortcut_still_cancels_keyboard_overlay(app, monkeypatch):
    completed = []
    monkeypatch.setattr(w, '_grab', lambda *a, **kw: (Image.new('RGB', (1280, 800)), 0, 0))
    monkeypatch.setattr(w, 'ocr_region', lambda *a, **kw: [])
    overlay = w.RegionSelector(app, lambda *args: completed.append(args))
    try:
        for key in (w.keyboard.Key.ctrl, w.keyboard.Key.alt, w.keyboard.KeyCode.from_char('p')):
            app.manager._on_press(key)
        wait_for(app.root, lambda: overlay.done)
        assert completed == [(None, overlay.frozen)]
        assert app.manager.raw_mode_cb is None
    finally:
        app.manager.down.clear()
        overlay.finish(None)


def test_missing_words_keeps_escape_available(app, monkeypatch):
    completed = []
    monkeypatch.setattr(w, '_grab', lambda *a, **kw: (Image.new('RGB', (1280, 800)), 0, 0))
    monkeypatch.setattr(w, 'ocr_region', lambda *a, **kw: [])
    overlay = w.RegionSelector(app, lambda *args: completed.append(args))
    try:
        wait_for(app.root, lambda: overlay.navigator is not None)
        assert not overlay.navigator.current
        overlay.key('enter')
        assert not completed
        assert 'Слів не знайдено' in overlay.canvas.itemcget(overlay.hint, 'text')
        overlay.key('esc')
        assert completed == [(None, overlay.frozen)]
    finally:
        overlay.finish(None)
