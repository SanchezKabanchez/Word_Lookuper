"""Adjacent title words are picked at the actual click, never a stale hover."""
import time
from types import SimpleNamespace

import pytest
from PIL import Image

import word_lookup as w
import word_lookup_selection as selection


OFFICIAL = {'word': 'Official', 'box': (100, 200, 160, 230),
            'line_text': 'GTA 6 - Official Main Menu Music'}
MENU = {'word': 'Menu', 'box': (162, 200, 220, 230),
        'line_text': OFFICIAL['line_text']}


def point(x, y=210):
    return SimpleNamespace(x=x, y=y, x_root=x, y_root=y)


def wait_for(root, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), 'Selection state did not settle before timeout'


@pytest.fixture
def hover_overlay(app, monkeypatch):
    app._lookup_profile = {'selection_mode': 'hover'}
    monkeypatch.setattr(selection, '_active_monitor_bounds', lambda *args: (0, 0, 1280, 800))
    monkeypatch.setattr(w, '_grab', lambda *args, **kwargs: (Image.new('RGB', (1280, 800)), 0, 0))
    completed = []
    overlay = w.RegionSelector(app, lambda *args: completed.append(args))
    yield overlay, completed
    overlay.finish(None)


@pytest.mark.parametrize('with_motion', [False, True])
@pytest.mark.parametrize('menu_cached', [False, True])
def test_quick_click_on_menu_cannot_reuse_official_hover(hover_overlay, with_motion, menu_cached):
    overlay, completed = hover_overlay
    overlay.mouse = (155, 210)
    overlay._set_hover(dict(OFFICIAL))
    if menu_cached:
        overlay._hover_cache.append(((185, 210), dict(MENU)))
    # Only 3 px outside Official: the former 4px tolerance selected the old word.
    clicked = point(163)
    if with_motion:
        overlay._motion(clicked)
    overlay._press(clicked)
    overlay._release(clicked)
    result = completed[0][0]
    assert result[1:3] == (163, 210)
    if menu_cached:
        assert result[0] == 'word' and result[3]['word'] == 'Menu'
    else:
        assert result == ('point', 163, 210)  # fresh OCR at this exact click


def test_old_response_near_new_pointer_must_contain_the_pointer(hover_overlay):
    overlay, completed = hover_overlay
    overlay.mouse = (164, 210)
    # The answer is only 9 px from its requested point, but belongs to Official.
    overlay._hq.put(('hover', (155, 210), dict(OFFICIAL)))
    overlay._poll()
    assert overlay.hover_hit is None
    clicked = point(164)
    overlay._press(clicked)
    overlay._release(clicked)
    assert completed[0][0] == ('point', 164, 210)


def test_late_foreign_response_cannot_replace_valid_menu_result_in_same_poll(hover_overlay):
    overlay, completed = hover_overlay
    overlay.mouse = (164, 210)
    overlay._hq.put(('hover', (164, 210), dict(MENU)))
    overlay._hq.put(('hover', (155, 210), dict(OFFICIAL)))
    overlay._poll()
    assert overlay.hover_hit['word'] == 'Menu'
    overlay._press(point(164))
    overlay._release(point(164))
    assert completed[0][0][3]['word'] == 'Menu'


def test_marquee_ignores_late_hover_and_keeps_its_screen_rectangle(hover_overlay):
    overlay, completed = hover_overlay
    overlay._set_hover(dict(OFFICIAL))
    overlay._press(point(150))
    overlay._drag(point(240, 260))
    overlay._hq.put(('hover', (150, 210), dict(OFFICIAL)))
    overlay._poll()
    assert overlay.hover_hit is None
    overlay._release(point(240, 260))
    assert completed[0][0] == ('rect', 150, 210, 240, 260)


def test_a_cached_word_box_wins_over_an_inconsistent_point_miss(hover_overlay):
    overlay, _ = hover_overlay
    overlay._hover_cache.extend([((180, 210), dict(MENU)), ((165, 210), None)])
    cached, hit = overlay._cached_hover(165, 210)
    assert cached and hit['word'] == 'Menu'
    assert overlay._cached_hover(221, 210) == (False, None)


def test_shared_box_boundary_belongs_to_the_word_starting_at_that_pixel():
    words = [w.W('Official', 100, 200, 160, 230, 0),
             w.W('Menu', 160, 200, 220, 230, 0)]
    navigator = selection.WordNavigator(words)
    assert navigator.hit_at(159, 210)['word'] == 'Official'
    assert navigator.hit_at(160, 210)['word'] == 'Menu'
    assert navigator.hit_at(220, 210) is None


def test_overlapping_ocr_boxes_choose_the_word_nearest_the_actual_point():
    words = [w.W('Official', 100, 200, 180, 230, 0),
             w.W('Menu', 160, 200, 220, 230, 0)]
    navigator = selection.WordNavigator(words)
    assert navigator.hit_at(170, 210)['word'] == 'Menu'


@pytest.mark.parametrize('scaling', [5 / 3, 2.0])
def test_font_dpi_does_not_rescale_physical_click_or_negative_monitor_origin(app, monkeypatch, scaling):
    previous = app.root.tk.call('tk', 'scaling')
    app.root.tk.call('tk', 'scaling', scaling)
    monkeypatch.setattr(selection, '_active_monitor_bounds', lambda *args: (-640, 0, 0, 480))
    monkeypatch.setattr(w, '_grab', lambda *args, **kwargs: (Image.new('RGB', (640, 480)), -640, 0))
    words = [w.W('Official', -620, 200, -560, 230, 0),
             w.W('Menu', -560, 200, -500, 230, 0)]
    monkeypatch.setattr(w, 'ocr_region', lambda *args, **kwargs: words)
    completed = []
    overlay = w.RegionSelector(app, lambda *args: completed.append(args))
    try:
        wait_for(app.root, lambda: overlay.navigator is not None)
        clicked = SimpleNamespace(x=80, y=210, x_root=-560, y_root=210)
        overlay._press(clicked)
        overlay._release(clicked)
        result = completed[0][0]
        assert result[1:3] == (-560, 210)
        assert result[3]['word'] == 'Menu'
        assert result[3]['box'] == (-560, 200, -500, 230)
    finally:
        overlay.finish(None)
        app.root.tk.call('tk', 'scaling', previous)


def test_clipped_capture_is_drawn_at_its_actual_screen_origin(app, monkeypatch):
    monkeypatch.setattr(selection, '_active_monitor_bounds', lambda *args: (-640, 0, 0, 480))
    monkeypatch.setattr(w, '_grab', lambda *args, **kwargs: (Image.new('RGB', (630, 480)), -630, 0))
    monkeypatch.setattr(w, 'ocr_region', lambda *args, **kwargs: [])
    overlay = w.RegionSelector(app, lambda *args: None)
    try:
        picture = next(item for item in overlay.canvas.find_all() if overlay.canvas.type(item) == 'image')
        assert overlay.canvas.coords(picture) == [10, 0]
        assert overlay.frozen[1:] == (-630, 0)
    finally:
        overlay.finish(None)


def test_identical_hover_results_do_not_repeat_canvas_layout(hover_overlay, monkeypatch):
    overlay, _ = hover_overlay
    overlay.mouse = (140, 210)
    overlay._set_hover(dict(OFFICIAL))
    calls = []
    original = overlay.canvas.coords
    def coords(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(overlay.canvas, 'coords', coords)
    for _ in range(100):
        overlay._hq.put(('hover', (140, 210), dict(OFFICIAL)))
        overlay._poll()
    assert calls == []


def test_overlapping_hover_cache_uses_menu_box_nearest_click(hover_overlay):
    overlay, completed = hover_overlay
    official = dict(OFFICIAL, box=(100, 200, 180, 230))
    overlay._hover_cache.extend([((190, 210), dict(MENU)), ((140, 210), official)])
    overlay._set_hover(official)
    overlay._press(point(170))
    overlay._release(point(170))
    assert completed[0][0][3]['word'] == 'Menu'


def test_broad_late_official_box_does_not_override_closer_menu_box(hover_overlay):
    overlay, _ = hover_overlay
    overlay.mouse = (170, 210)
    overlay._set_hover(dict(MENU))
    overlay._hq.put(('hover', (140, 210), dict(OFFICIAL, box=(100, 200, 180, 230))))
    overlay._poll()
    assert overlay.hover_hit['word'] == 'Menu'
