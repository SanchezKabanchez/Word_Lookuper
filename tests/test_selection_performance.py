"""Frozen selection limits repeated OCR work without changing text resolution."""
import threading
import time
from types import SimpleNamespace

import pytest
from PIL import Image

import word_lookup as w
import word_lookup_selection as selection


def wait_for(root, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), 'OCR worker did not settle before timeout'


@pytest.fixture
def make_overlay(app, monkeypatch):
    overlays = []
    monkeypatch.setattr(selection, '_active_monitor_bounds', lambda app, hwnd, fallback: (0, 0, 1280, 800))
    monkeypatch.setattr(w, '_grab', lambda *args, **kwargs: (Image.new('RGB', (1280, 800)), 0, 0))
    def make(mode='hover', callback=lambda *args: None):
        app._lookup_profile = {'selection_mode': mode}
        overlay = w.RegionSelector(app, callback)
        overlays.append(overlay)
        return overlay
    yield make
    for overlay in overlays:
        overlay.finish(None)


def test_monitor_selection_preserves_negative_coordinates_and_boundaries():
    monitors = [{'left': -1920, 'top': 0, 'width': 1920, 'height': 1080},
                {'left': 0, 'top': 0, 'width': 2560, 'height': 1440},
                {'left': 0, 'top': -1200, 'width': 1600, 'height': 1200}]
    assert selection._monitor_at(monitors, (-1500, 200)) == (-1920, 0, 0, 1080)
    assert selection._monitor_at(monitors, (0, 200)) == (0, 0, 2560, 1440)
    assert selection._monitor_at(monitors, (400, -500)) == (0, -1200, 1600, 0)
    assert selection._monitor_at(monitors, (9000, 200)) is None


def test_capture_and_navigation_use_only_selected_monitor_with_screen_coordinates(app, monkeypatch):
    captured, scans, completed = [], [], []
    monkeypatch.setattr(selection, '_active_monitor_bounds', lambda *args: (-640, 0, 0, 480))
    def grab(*args):
        captured.append(args)
        return Image.new('RGB', (640, 480)), -640, 0
    def scan(*args, **kwargs):
        scans.append(args)
        return [w.W('hello', -620, 220, -560, 250, 0)]
    monkeypatch.setattr(w, '_grab', grab)
    monkeypatch.setattr(w, 'ocr_region', scan)
    overlay = w.RegionSelector(app, lambda *args: completed.append(args))
    try:
        wait_for(app.root, lambda: overlay.navigator is not None)
        assert captured == [(-640, 0, 0, 480)]
        assert scans == [(-640, 0, 0, 480)]
        assert (overlay.w, overlay.h, overlay.ox, overlay.oy) == (640, 480, -640, 0)
        assert overlay.win.winfo_x() == -640
        assert overlay.canvas.coords(overlay.hl_rect)[0] == 17
        overlay.key('enter')
        assert completed[0][0][1] == -590
        assert completed[0][0][3]['box'] == (-620, 220, -560, 250)
    finally:
        overlay.finish(None)


def test_all_monitor_capture_can_be_selected_explicitly(app, monkeypatch):
    w.cfg['performance']['capture_active_monitor'] = False
    def forbidden(*args):
        raise AssertionError('Explicit all-display setting must bypass monitor selection')
    monkeypatch.setattr(selection, '_active_monitor_bounds', forbidden)
    monkeypatch.setattr(w, '_grab', lambda *args: (Image.new('RGB', (1280, 800)), 0, 0))
    monkeypatch.setattr(w, 'ocr_region', lambda *args, **kwargs: [])
    overlay = w.RegionSelector(app, lambda *args: None)
    overlay.finish(None)


def test_same_word_and_exact_empty_point_reuse_frozen_ocr_results(app, make_overlay, monkeypatch):
    calls = []
    def find(x, y, **kwargs):
        calls.append((x, y))
        return {'word': 'hello', 'box': (100, 200, 180, 230), 'line_text': 'hello'} if x < 200 else None
    monkeypatch.setattr(w, 'find_word_at', find)
    overlay = make_overlay()
    overlay._request(120, 210)
    wait_for(app.root, lambda: not overlay._busy)
    for x in range(125, 175, 5):
        overlay._request(x, 210)
    assert calls == [(120, 210)]
    overlay._request(400, 300)
    wait_for(app.root, lambda: not overlay._busy)
    for _ in range(20):
        overlay._request(400, 300)
    assert calls == [(120, 210), (400, 300)]
    overlay._request(402, 300)
    wait_for(app.root, lambda: not overlay._busy)
    assert calls[-1] == (402, 300)


def test_hover_burst_runs_one_worker_and_only_latest_pending_position(app, make_overlay, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls, concurrent, maximum = [], 0, 0
    lock = threading.Lock()
    def find(x, y, **kwargs):
        nonlocal concurrent, maximum
        with lock:
            concurrent += 1
            maximum = max(maximum, concurrent)
            calls.append((x, y))
        if len(calls) == 1:
            started.set()
            assert release.wait(3)
        with lock:
            concurrent -= 1
        return None
    monkeypatch.setattr(w, 'find_word_at', find)
    overlay = make_overlay()
    try:
        overlay._request(100, 200)
        assert started.wait(2)
        for _ in range(50):
            overlay._request(100, 200)
        for x in (200, 250, 300, 400):
            overlay._request(x, 200)
        release.set()
        wait_for(app.root, lambda: not overlay._busy)
        assert calls == [(100, 200), (400, 200)]
        assert maximum == 1
    finally:
        release.set()


def test_stationary_motion_does_not_restart_the_debounce(app, make_overlay, monkeypatch):
    overlay = make_overlay()
    calls = []
    def request(x, y):
        overlay._after_id = None
        calls.append((x, y))
    monkeypatch.setattr(overlay, '_request', request)
    overlay._motion(SimpleNamespace(x_root=200, y_root=200))
    scheduled = overlay._after_id
    for index in range(100):
        overlay._motion(SimpleNamespace(x_root=200 + index % 2, y_root=200))
        assert overlay._after_id == scheduled
    wait_for(app.root, lambda: bool(calls))
    assert calls == [(201, 200)]


@pytest.mark.parametrize('mode', ['keyboard', 'hover'])
def test_close_cancels_inflight_scan_and_discards_pending_hover(app, make_overlay, monkeypatch, mode):
    started, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def scan(*args, **kwargs):
        calls.append(args)
        started.set()
        assert release.wait(3)
        try:
            w.check_cancelled()
        except w.LookupCancelled:
            cancelled.set()
            raise
        raise AssertionError('A closed selector must cancel its OCR worker')
    monkeypatch.setattr(w, 'ocr_region', scan)
    monkeypatch.setattr(w, 'find_word_at', scan)
    overlay = make_overlay(mode)
    try:
        if mode == 'hover':
            overlay._request(100, 200)
        assert started.wait(2)
        if mode == 'hover':
            overlay._request(400, 200)
        overlay.finish(None)
        release.set()
        assert cancelled.wait(2)
        if mode == 'hover':
            wait_for(app.root, lambda: not overlay._busy)
        assert len(calls) == 1
        assert overlay._req is None
        assert overlay._hq.empty()
    finally:
        release.set()


def test_clicking_another_keyboard_word_reuses_frame_words(app, make_overlay, monkeypatch):
    completed, calls = [], []
    words = [w.W('alpha', 100, 200, 180, 230, 0), w.W('beta', 220, 200, 280, 230, 0)]
    def scan(*args, **kwargs):
        calls.append(args)
        return words
    monkeypatch.setattr(w, 'ocr_region', scan)
    overlay = make_overlay('keyboard', lambda *args: completed.append(args))
    wait_for(app.root, lambda: overlay.navigator is not None)
    point = SimpleNamespace(x=230, y=210, x_root=230, y_root=210)
    overlay._press(point)
    overlay._release(point)
    result = completed[0][0]
    assert result[0] == 'word' and result[3]['word'] == 'beta'
    assert result[3]['_frame_words'] == tuple(words)
    assert result[3]['_frame_words'] is overlay.navigator.words
    assert len(calls) == 1


def test_starting_a_marquee_cancels_pending_hover_ocr(app, make_overlay, monkeypatch):
    calls, settled = [], threading.Event()
    monkeypatch.setattr(w, 'find_word_at', lambda *args, **kwargs: calls.append(args))
    overlay = make_overlay()
    point = SimpleNamespace(x=100, y=200, x_root=100, y_root=200)
    overlay._motion(point)
    assert overlay._after_id is not None
    overlay._press(point)
    assert overlay._after_id is None
    overlay.win.after(w.cfg['performance']['hover_delay_ms'] + 50, settled.set)
    wait_for(app.root, settled.is_set)
    assert calls == []
