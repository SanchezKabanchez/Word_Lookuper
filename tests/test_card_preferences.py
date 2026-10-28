"""Card preferences apply to stored content and avoid unnecessary work."""

import copy
import json
import queue
import threading
from types import SimpleNamespace

from PIL import Image
import pytest
import word_lookup as w


def widget_texts(widget):
    texts = []
    if isinstance(widget, w.tk.Text):
        texts.append(widget.get("1.0", "end-1c"))
    else:
        try:
            texts.append(str(widget.cget("text")))
        except w.tk.TclError:
            pass
    for child in widget.winfo_children():
        texts.append(widget_texts(child))
    return "\n".join(texts)


def finish_tk_callbacks(app, milliseconds=130):
    finished = w.tk.BooleanVar(master=app.root, value=False)
    app.root.after(milliseconds, lambda: finished.set(True))
    app.root.wait_variable(finished)


@pytest.fixture
def detailed_card(card):
    card["sentence"] = card["context"] = "Original sentence marker."
    card["sentence_tr"] = "Translated sentence marker."
    card["definition_tr"] = "Translated definition marker."
    card["phonetic"] = "PHONETICMARKER"
    card["meanings"][0]["defs"][0]["e"] = "Example sentence marker."
    card["groups"] = [("іменник", ["first", "second", "third", "fourth", "FIFTHALTERNATIVE"])]
    return card


@pytest.mark.parametrize("original,translated", [(True, True), (True, False), (False, True), (False, False)])
def test_original_and_translated_sentence_are_independent_offline(app, detailed_card, original, translated):
    w.add_saved_word(detailed_card)
    w.cfg["popup"]["sentence"] = original
    w.cfg["popup"]["sentence_translation"] = translated
    # Main-word translation visibility must not control sentence visibility.
    w.cfg["sections"]["bilingual"] = False
    app.open_saved_word(detailed_card["word"])
    contents = widget_texts(app.popup.content)
    assert ("Original sentence marker." in contents) is original
    assert ("Translated sentence marker." in contents) is translated


@pytest.mark.parametrize("setting,marker", [
    ("translate_definition", "Translated definition marker."),
    ("phonetic", "PHONETICMARKER"),
    ("examples", "Example sentence marker."),
    ("alternate_translations", "FIFTHALTERNATIVE"),
])
def test_hidden_card_sections_do_not_leak_from_saved_data(app, detailed_card, setting, marker):
    w.add_saved_word(detailed_card)
    w.cfg["popup"][setting] = True
    app.open_saved_word(detailed_card["word"])
    assert marker in widget_texts(app.popup.content)
    w.cfg["popup"][setting] = False
    app.open_saved_word(detailed_card["word"])
    assert marker not in widget_texts(app.popup.content)
    assert detailed_card["word"] in widget_texts(app.popup.content)


def test_context_image_visibility_preserves_saved_image(app, detailed_card):
    detailed_card["_context_image"] = Image.new("RGB", (240, 120), "navy")
    w.add_saved_word(detailed_card)
    entry = w.saved_entry(detailed_card["word"])
    image_name = entry["image_file"]
    w.cfg["popup"]["context_image"] = True
    app.open_saved_word(detailed_card["word"])
    assert app.popup.context_photo is not None
    w.cfg["popup"]["context_image"] = False
    app.open_saved_word(detailed_card["word"])
    assert app.popup.context_photo is None
    assert entry["image_file"] == image_name
    assert w.os.path.isfile(w.context_image_path(image_name))


def test_settings_share_variables_and_debounce_disk_writes(app, monkeypatch):
    writes = []
    monkeypatch.setattr(w, "save_config", lambda config: writes.append(copy.deepcopy(config)))
    variable = app._setting_vars[("popup", "font_size")]
    assert app._var(w.tk.IntVar, ("popup", "font_size")) is variable
    for value in (9, 10, 11):
        variable.set(value)
    assert w.cfg["popup"]["font_size"] == 11
    assert writes == []
    finish_tk_callbacks(app, 400)
    assert len(writes) == 1
    assert writes[0]["popup"]["font_size"] == 11


def test_pending_settings_flush_immediately_and_persist(app):
    app._setting_vars[("popup", "font_size")].set(9)
    app._setting_vars[("popup", "sentence_translation")].set(False)
    app._flush_config_save()
    with open(w.config_path(), encoding="utf-8") as handle:
        saved = json.load(handle)
    assert saved["popup"]["font_size"] == 9
    assert saved["popup"]["sentence_translation"] is False
    restored = w.load_config()
    assert restored["popup"]["sentence_translation"] is False


def test_live_preferences_refresh_real_card_preserving_pin_and_scroll(app, detailed_card):
    detailed_card["meanings"][0]["defs"][0]["d"] = "A long definition. " * 200
    app.show_lookup(detailed_card, None)
    app.popup.toggle_pin()
    app.root.update()
    app.popup.viewport.canvas.yview_moveto(0.4)
    app.root.update()
    previous_popup = app.popup
    previous_scroll = previous_popup.viewport.canvas.yview()[0]
    app._setting_vars[("popup", "phonetic")].set(False)
    finish_tk_callbacks(app)
    assert app.popup is not previous_popup
    assert app.popup.pinned
    assert abs(app.popup.viewport.canvas.yview()[0] - previous_scroll) < 0.03
    assert "PHONETICMARKER" not in widget_texts(app.popup.content)


def test_card_settings_shortcut_selects_appearance_tab(app):
    app.open_card_settings()
    assert app.notebook.select() == str(app.look_panel)


def test_popup_menu_stays_open_and_edits_live_preferences(app, card):
    app.show_lookup(card, None)
    popup = app.popup
    popup._show_options()
    app.root.update()
    menu = popup.options_menu
    assert menu.winfo_exists() and menu.winfo_ismapped()
    assert popup.pinned
    # A menu can extend beyond the card; a queued global click must preserve it.
    app._handle(("press", 0, 0, "left", set()))
    assert app.popup is popup
    index = next(i for i, (path, _) in enumerate(w.CARD_CONTENT_OPTIONS)
                 if path == ("popup", "sentence_translation"))
    menu.invoke(index)
    menu.unpost()
    finish_tk_callbacks(app)
    assert w.cfg["popup"]["sentence_translation"] is False
    assert app.popup.pinned


@pytest.mark.parametrize("original", [True, False])
def test_migration_preserves_previous_sentence_visibility(original):
    w.atomic_json_write(w.config_path(), {"version": 3, "popup": {"sentence": original, "font_size": 12}})
    loaded = w.load_config()
    assert loaded["popup"]["sentence"] is original
    assert loaded["popup"]["sentence_translation"] is original
    assert loaded["popup"]["font_size"] == 10


def test_migration_preserves_explicit_new_flags_and_custom_font():
    w.atomic_json_write(w.config_path(), {"version": 3, "popup": {
        "sentence": False, "sentence_translation": True, "font_size": 14}})
    loaded = w.load_config()
    assert loaded["popup"]["sentence_translation"] is True
    assert loaded["popup"]["font_size"] == 14


def prepare_lookup(app, monkeypatch, card):
    w.cfg["learning"]["capture_context"] = False
    w.cfg["ocr"]["highlight"] = False
    hit = {"word": card["word"], "line_text": "Original sentence marker.", "box": (10, 10, 90, 30)}
    monkeypatch.setattr(w, "find_word_at", lambda *args, **kwargs: hit)
    monkeypatch.setattr(w, "build_word_card_fast", lambda *args, **kwargs: (copy.deepcopy(card), False))
    return app._new_token()


def test_disabling_sentence_translation_skips_translator_but_keeps_context(app, card, monkeypatch):
    token = prepare_lookup(app, monkeypatch, card)
    w.cfg["popup"]["sentence"] = True
    w.cfg["popup"]["sentence_translation"] = False
    w.cfg["popup"]["translate_definition"] = False
    monkeypatch.setattr(w, "find_sentence", lambda *args: "Original sentence marker.")
    translations = []
    monkeypatch.setattr(w, "translate_text", lambda text: translations.append(text) or "translated")
    completed = threading.Event()

    class TrackingQueue(queue.Queue):
        def put(self, event, *args, **kwargs):
            super().put(event, *args, **kwargs)
            if event[0] == "update" and event[3].get("sentence_pending") is False:
                completed.set()

    monkeypatch.setattr(w, "events", TrackingQueue())
    app._lookup_thread("word", 20, 20, token)
    assert completed.wait(2), "Sentence worker did not complete"
    assert translations == []
    snapshots = []
    while not w.events.empty():
        event = w.events.get_nowait()
        if event[0] == "update":
            snapshots.append(event[3])
    assert any(item.get("context") == "Original sentence marker." for item in snapshots)


def test_hidden_definitions_skip_definition_translation(app, card, monkeypatch):
    card.pop("definition_tr", None)
    token = prepare_lookup(app, monkeypatch, card)
    w.cfg["popup"]["sentence"] = False
    w.cfg["popup"]["sentence_translation"] = False
    w.cfg["popup"]["translate_definition"] = True
    w.cfg["sections"]["definitions"] = False
    translations = []
    monkeypatch.setattr(w, "translate_text", lambda text: translations.append(text) or "translated")
    app._lookup_thread("word", 20, 20, token)
    assert translations == []


def test_cancelled_lookup_skips_capture_and_ocr(app, monkeypatch):
    token = app._new_token()
    app._new_token()
    calls = []
    monkeypatch.setattr(w, "_grab", lambda *args, **kwargs: calls.append("capture"))
    monkeypatch.setattr(w, "find_word_at", lambda *args, **kwargs: calls.append("ocr"))
    app._lookup_thread("word", 20, 20, token)
    assert calls == []


def test_cancelling_during_sentence_ocr_skips_translation(app, card, monkeypatch):
    token = prepare_lookup(app, monkeypatch, card)
    w.cfg["popup"]["sentence"] = True
    w.cfg["popup"]["sentence_translation"] = True
    w.cfg["popup"]["translate_definition"] = False
    completed = threading.Event()
    translations = []

    def find_sentence(*args):
        app._new_token()
        return "Original sentence marker."

    def start_worker(target, daemon=True):
        def run():
            try:
                target()
            finally:
                completed.set()
        return threading.Thread(target=run, daemon=daemon)

    monkeypatch.setattr(w, "find_sentence", find_sentence)
    monkeypatch.setattr(w, "translate_text", lambda text: translations.append(text) or "translated")
    monkeypatch.setattr(w, "threading", SimpleNamespace(Thread=start_worker, Lock=threading.Lock))
    app._lookup_thread("word", 20, 20, token)
    assert completed.wait(2), "Sentence worker did not finish cancellation"
    assert translations == []


def test_dictionary_starts_in_parallel_and_partial_does_not_wait(card, monkeypatch):
    dictionary_started = threading.Event()
    release_dictionary = threading.Event()
    partial_received = threading.Event()
    finished = threading.Event()
    result = {}

    def dictionary(word):
        dictionary_started.set()
        assert release_dictionary.wait(2), "Test did not release dictionary worker"
        return {key: card[key] for key in ("base", "phonetic", "audios", "meanings")}

    def translation(word, context=""):
        assert dictionary_started.wait(2), "Dictionary must start before translation completes"
        return "переклад", [], "", False

    def run():
        try:
            result["card"], result["partial"] = w.build_word_card_fast(
                card["word"], on_partial=lambda partial: partial_received.set())
        except BaseException as error:
            result["error"] = error
        finally:
            finished.set()

    monkeypatch.setattr(w, "english_dictionary", dictionary)
    monkeypatch.setattr(w, "_translate_word", translation)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert partial_received.wait(2), "Quick translation must be displayed before dictionary completes"
        assert not finished.is_set()
    finally:
        release_dictionary.set()
        worker.join(3)
    assert finished.is_set()
    assert "error" not in result, result.get("error")
    assert result["partial"]
    assert result["card"]["meanings"] == card["meanings"]
