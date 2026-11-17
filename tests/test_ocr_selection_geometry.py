"""Word labels must match the pointer, even with dropped OCR spaces or DPI."""
import pytest
from PIL import Image

import word_lookup as w
import word_lookup_geometry as ocr


def test_pointer_in_whitespace_does_not_choose_nearby_official():
    words = [w.W("Official", 168, 20, 237, 40, 0),
             w.W("Menu", 250, 20, 288, 40, 0)]
    assert w.pick_word(words, 267, 30, tol=14).text == "Menu"
    assert w.pick_word(words, 244, 30, tol=14) is None
    assert w.pick_word(words[:1], 250, 30, tol=14) is None
    assert w.pick_word(words, 288, 30, tol=14) is None


def test_overlapping_word_boxes_require_an_unambiguous_pointer():
    words = [w.W("Official", 168, 20, 270, 40, 0),
             w.W("Menu", 250, 20, 288, 40, 0)]
    assert w.pick_word(words, 267, 30, tol=14) is None


@pytest.mark.parametrize("sx,sy", [(1.0, 1.0), (1.25, 1.25), (1.5, 1.5), (1.25, 1.5)])
def test_word_selection_preserves_capture_scale_and_negative_monitor_origin(monkeypatch, sx, sy):
    # Screen coordinates are logical inputs to _grab; OCR coordinates are the
    # returned screenshot pixels. Apply each axis exactly once in each direction.
    monkeypatch.setattr(w, "pick_engine", lambda: "rapidocr")
    monkeypatch.setattr(w, "_grab", lambda *args: (
        Image.new("RGB", (round(600 * sx), round(100 * sy))),
        -800, 200, -200, 300, sx, sy))
    monkeypatch.setattr(w, "_run_ocr", lambda *args: [
        w.W("Official", 168 * sx, 20 * sy, 237 * sx, 40 * sy, 0),
        w.W("Menu", 250 * sx, 20 * sy, 288 * sx, 40 * sy, 0)])
    monkeypatch.setattr(w, "_split_merged_word", lambda *args: None)
    point = (-533, 230)
    hit = w.find_word_at(*point)
    assert hit["word"] == "Menu"
    assert hit["box"] == (-550, 220, -512, 240)
    assert hit["box"][0] <= point[0] < hit["box"][2]
    assert hit["box"][1] <= point[1] < hit["box"][3]


def test_ocr_missing_target_does_not_substitute_neighbour(monkeypatch):
    monkeypatch.setattr(w, "pick_engine", lambda: "rapidocr")
    monkeypatch.setattr(w, "_grab", lambda *args: (
        Image.new("RGB", (600, 100)), 0, 0, 600, 100, 1, 1))
    monkeypatch.setattr(w, "_run_ocr", lambda *args: [w.W("Official", 168, 20, 237, 40, 0)])
    assert w.find_word_at(250, 30) is None


def test_refined_crop_selects_word_at_pointer_instead_of_its_first_word(monkeypatch):
    image = Image.new("RGB", (400, 100))
    monkeypatch.setattr(w, "_run_ocr", lambda *args: [
        w.W("Official", 5, 5, 65, 35, 0),
        w.W("Main", 75, 5, 110, 35, 0),
        w.W("Menu", 120, 5, 160, 35, 0)])
    assert w._reocr_crop(image, (100, 20, 280, 60), "rapidocr", point=(240, 40)) == "Menu"
    assert w._reocr_crop(image, (100, 20, 280, 60), "rapidocr") is None


def test_merged_line_refinement_isolates_the_pointer_word(monkeypatch):
    calls = []
    monkeypatch.setattr(w, "_line_gaps", lambda *args, **kwargs:
                        (100, 290, [(170, 180), (220, 230)]))

    def refine(image, box, engine, point=None):
        calls.append((box, point))
        return "Menu"

    monkeypatch.setattr(w, "_reocr_crop", refine)
    result = w._split_merged_word(Image.new("RGB", (400, 100)),
                                 w.W("OfficialMainMenu", 100, 20, 290, 40, 0), 267, "rapidocr")
    assert result.text == "Menu"
    assert calls == [((230, 20, 290, 40), (267, 30))]
    assert w._split_merged_word(Image.new("RGB", (400, 100)),
                               w.W("OfficialMainMenu", 100, 20, 290, 40, 0), 225, "rapidocr") is False


def test_extra_image_gaps_do_not_shift_recognized_word_labels(monkeypatch):
    monkeypatch.setattr(ocr, "_line_gaps", lambda *args:
                        (10, 290, [(40, 50), (100, 110), (170, 180)]))
    assert w._gap_segments(Image.new("RGB", (300, 100)), 3, 0, 0, 300, 100) is None


def test_ctc_positions_recover_menu_without_proportional_width_guess(monkeypatch):
    # Character locations from a real RapidOCR 1.4.4 recognition of the bold
    # title. Its text omitted spaces; the image still has word-sized gaps.
    text = "OfficialMainMenu"
    intervals = [(115, 130), (132, 143), (143, 152), (152, 162),
                 (162, 173), (173, 183), (183, 196), (196, 209),
                 (219, 234), (240, 254), (254, 266), (266, 280),
                 (294, 309), (311, 326), (332, 347), (348, 363)]
    boxes = [[[left, 33], [right, 33], [right, 58], [left, 58]] for left, right in intervals]
    record = [[[115, 33], [363, 33], [363, 58], [115, 58]], text, 0.99, boxes, list(text)]
    monkeypatch.setattr(ocr, "_line_gaps", lambda *args:
                        (115, 363, [(210, 218), (281, 293)]))
    words = ocr._ctc_words(Image.new("RGB", (400, 100)), record, 0)
    assert [word.text for word in words] == ["Official", "Main", "Menu"]
    assert w.pick_word(words, 327, 44, tol=14).text == "Menu"
    assert words[-1].x0 == 294 and words[-1].x1 == 363


def test_ctc_mismatched_metadata_cannot_supply_guessed_word_positions():
    record = [[[0, 0], [100, 0], [100, 30], [0, 30]], "Official Menu", 0.9, [], []]
    assert ocr._ctc_words(Image.new("RGB", (100, 30)), record, 0) is None
