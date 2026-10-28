from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont
import word_lookup as w


def test_escape_rejects_queued_results_and_errors(app, card, monkeypatch):
    token = app._new_token()
    app.show_lookup(card, None, (500, 400, 600, 430))
    app._handle(("esc",))
    assert app.popup is None
    app._handle(("show", card, None, None, token))
    assert app.popup is None
    called = []
    monkeypatch.setattr(app, "toast", called.append)
    app._handle(("lookup_event", token, ("toast", "stale error")))
    assert called == []


def test_pin_survives_update_and_outside_click(app, card):
    token = app._new_token()
    app.show_lookup(card, None, (500, 400, 600, 430))
    app.popup.toggle_pin()
    app._handle(("update", token, card["word"], card))
    assert app.popup.pinned
    app._handle(("press", 0, 790, "left", set()))
    assert app.popup is not None
    app.dismiss_lookup()
    assert app.popup is None


def test_long_popup_fits_screen_and_scrolls(app, card):
    card["meanings"][0]["defs"][0]["d"] = "A long definition. " * 300
    app.show_lookup(card, None, (1260, 780, 1270, 790))
    app.root.update()
    popup = app.popup
    assert popup.win.winfo_y() >= 0
    assert popup.win.winfo_x() >= 0
    assert popup.win.winfo_y() + popup.win.winfo_height() <= 800
    assert popup.viewport.canvas.yview()[1] < 1.0
    before = popup.viewport.canvas.yview()[0]
    popup.viewport._wheel(SimpleNamespace(x_root=popup.win.winfo_x() + 40,
                                          y_root=popup.win.winfo_y() + 60, delta=-120, num=0))
    assert popup.viewport.canvas.yview()[0] > before


@pytest.mark.parametrize("area,anchor", [((-1920, 0, 0, 1080), (-800, 100, -700, 130)),
                                          ((0, -1080, 1920, 0), (900, -1050, 1000, -1020))])
def test_popup_placement_on_negative_monitors(area, anchor):
    x, y, width, height, _ = w.place_popup(anchor, 500, 1600, area)
    assert area[0] <= x and area[1] <= y
    assert x + width <= area[2] and y + height <= area[3]


def test_library_filter_selection_offline_open_and_delete(app, card, monkeypatch):
    w.add_saved_word(card)
    second = dict(card, word="apple", translation="яблуко", source="Garden")
    w.add_saved_word(second)
    app.open_saved_words()
    app.saved_query.set("garden")
    assert [e["word"] for e in app._visible_saved_words()] == ["apple"]
    app.select_all_saved(True)
    assert app._saved_check_var("apple").get()
    assert not app._saved_check_var(card["word"]).get()
    def no_lookup(*args):
        raise AssertionError("Saved cards must open without a lookup")
    monkeypatch.setattr(w, "build_word_card_fast", no_lookup)
    app.open_saved_word("apple")
    assert app.popup.card["translation"] == "яблуко"
    app.delete_saved_word("apple")
    assert "Обрано: 0" in app.saved_selected_var.get()
    assert not app._visible_saved_words()


def test_review_requeues_unknown_and_finishes_known(app, card):
    w.add_saved_word(card)
    review = w.ReviewWindow(app, w.load_saved_words())
    review.grade(True)
    assert review.completed == 0
    review.reveal()
    review.grade(False)
    assert len(review.pending) == 1 and not review.revealed
    review.reveal()
    review.grade(True)
    assert review.pending == [] and review.completed == 1
    assert review.known_btn.instate(["disabled"])
    review.win.destroy()


def test_copy_translation_really_reaches_clipboard(app, card):
    app.show_lookup(card, None)
    app.popup.copy_translation()
    assert w.pyperclip.paste() == card["translation"]


def test_tesseract_recognizes_text_from_frozen_image():
    image = Image.new("RGB", (600, 100), "white")
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 32)
    ImageDraw.Draw(image).text((20, 20), "Hello wonderful world", fill="black", font=font)
    w.cfg["ocr"]["engine"] = "tesseract"
    w._engine_cache["t"] = 0
    words = w.ocr_region(0, 0, 600, 100, frozen=(image, 0, 0))
    assert [word.text.lower() for word in words] == ["hello", "wonderful", "world"]
