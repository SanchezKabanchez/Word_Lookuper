"""Rendered selection remains readable on light scenes and smaller displays."""
import time

import mss
import pytest
from PIL import Image, ImageDraw, ImageFont

import word_lookup as w


def wait_for(root, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), 'Selection render did not settle before timeout'


@pytest.fixture
def light_selection(app, monkeypatch):
    image = Image.new('RGB', (1280, 800), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=36)
    box = draw.textbbox((140, 230), 'changing', font=font)
    draw.text((140, 230), 'changing', font=font, fill='black')
    monkeypatch.setattr(w, '_grab', lambda *args, **kwargs: (image, 0, 0))
    monkeypatch.setattr(w, 'ocr_region', lambda *args, **kwargs: [w.W('changing', *box, 0)])
    overlay = w.RegionSelector(app, lambda *args: None)
    wait_for(app.root, lambda: overlay.navigator is not None)
    yield overlay, box
    overlay.finish(None)


def canvas_pixels(overlay):
    overlay.win.update()
    canvas = overlay.canvas
    with mss.MSS() as capture:
        shot = capture.grab({'left': canvas.winfo_rootx(), 'top': canvas.winfo_rooty(),
                             'width': canvas.winfo_width(), 'height': canvas.winfo_height()})
    return Image.frombytes('RGB', shot.size, shot.bgra, 'raw', 'BGRX')


def assert_hint_fits(overlay):
    canvas = overlay.canvas
    plate = canvas.coords(overlay.hint_backplate)
    text = canvas.bbox(overlay.hint)
    assert 0 <= plate[0] < text[0] < text[2] < plate[2] <= canvas.winfo_width()
    assert 0 <= plate[1] < text[1] < text[3] < plate[3] <= canvas.winfo_height()
    assert text[0] - plate[0] >= 12
    assert text[1] - plate[1] >= 8
    order = canvas.find_all()
    assert order.index(overlay.hint_backplate) < order.index(overlay.hint)


def test_outline_and_underline_leave_every_selected_glyph_pixel_unchanged(light_selection):
    overlay, box = light_selection
    hit = overlay.hover_hit
    overlay._clear_hover()
    before = canvas_pixels(overlay)
    overlay._set_hover(hit)
    after = canvas_pixels(overlay)
    assert before.crop(box).tobytes() == after.crop(box).tobytes()
    assert overlay.canvas.itemcget(overlay.hl_rect, 'fill') == ''
    assert overlay.canvas.itemcget(overlay.hl_rect, 'stipple') == ''
    assert float(overlay.canvas.itemcget(overlay.hl_rect, 'width')) == 2
    assert overlay.canvas.itemcget(overlay.hl_underline, 'state') == 'normal'
    underline = overlay.canvas.coords(overlay.hl_underline)
    assert underline[1] > box[3] and underline[3] > box[3]
    assert after.getpixel((int((box[0] + box[2]) / 2), int(underline[1]))) == (255, 212, 59)
    overlay._clear_hover()
    assert overlay.canvas.itemcget(overlay.hl_rect, 'state') == 'hidden'
    assert overlay.canvas.itemcget(overlay.hl_underline, 'state') == 'hidden'


def test_hint_has_opaque_dark_padding_over_a_light_scene(light_selection):
    overlay, _ = light_selection
    assert_hint_fits(overlay)
    pixels = canvas_pixels(overlay)
    left, top, _, _ = overlay.canvas.coords(overlay.hint_backplate)
    assert pixels.getpixel((int(left + 4), int(top + 4))) == (17, 19, 24)
    assert overlay.canvas.itemcget(overlay.hint_backplate, 'stipple') == ''
    assert overlay.canvas.itemcget(overlay.hint, 'fill') == '#f1f1f5'


@pytest.mark.parametrize('size', [(640, 480), (480, 360), (320, 240)])
def test_wrapped_status_plate_grows_and_shrinks_with_its_text(light_selection, size):
    overlay, _ = light_selection
    overlay.win.geometry(f'{size[0]}x{size[1]}+0+0')
    overlay.win.update()
    overlay._set_keyboard_status('Esc — скасувати')
    short = overlay.canvas.coords(overlay.hint_backplate)
    overlay._set_keyboard_status('←/→ — слова · ↑/↓ — рядки · Enter — картка · Esc — скасувати\n'
                                 'Рядок 2: A readable instruction stays visible on a bright frozen game scene.')
    assert_hint_fits(overlay)
    long = overlay.canvas.coords(overlay.hint_backplate)
    assert long[3] - long[1] > short[3] - short[1]
    assert '\n' in overlay.canvas.itemcget(overlay.hint, 'text')
    overlay._set_keyboard_status('Слів не знайдено. Esc — скасувати')
    assert_hint_fits(overlay)
    shorter = overlay.canvas.coords(overlay.hint_backplate)
    assert shorter[3] - shorter[1] < long[3] - long[1]


def test_long_ocr_status_fits_small_screen_and_restores_text_after_resize(light_selection):
    overlay, _ = light_selection
    overlay.win.geometry('320x240+0+0')
    overlay.win.update()
    status = 'Рядок OCR: ' + 'changing ' * 150 + '\nEsc — скасувати'
    overlay._set_keyboard_status(status)
    assert_hint_fits(overlay)
    fitted = overlay.canvas.itemcget(overlay.hint, 'text')
    assert len(fitted) < len(status)
    assert fitted.endswith('Esc — скасувати')
    overlay.win.geometry('1280x800+0+0')
    overlay.win.update()
    assert_hint_fits(overlay)
    assert overlay.canvas.itemcget(overlay.hint, 'text') == status


def test_hint_moves_away_from_selected_top_word_and_returns_for_lower_word(app, monkeypatch):
    image = Image.new('RGB', (1280, 800), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=36)
    first = draw.textbbox((140, 24), 'changing', font=font)
    second = draw.textbbox((140, 720), 'world', font=font)
    draw.text((140, 24), 'changing', font=font, fill='black')
    draw.text((140, 720), 'world', font=font, fill='black')
    monkeypatch.setattr(w, '_grab', lambda *args, **kwargs: (image, 0, 0))
    monkeypatch.setattr(w, 'ocr_region', lambda *args, **kwargs: [w.W('changing', *first, 0),
                                                               w.W('world', *second, 1)])
    overlay = w.RegionSelector(app, lambda *args: None)
    try:
        wait_for(app.root, lambda: overlay.navigator is not None)
        assert_hint_fits(overlay)
        assert overlay.canvas.coords(overlay.hint_backplate)[1] > first[3]
        assert canvas_pixels(overlay).crop(first).tobytes() == image.crop(first).tobytes()
        overlay.key('down')
        assert_hint_fits(overlay)
        assert overlay.canvas.coords(overlay.hint_backplate)[3] < second[1]
        assert canvas_pixels(overlay).crop(second).tobytes() == image.crop(second).tobytes()
    finally:
        overlay.finish(None)
