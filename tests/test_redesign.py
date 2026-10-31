"""Real Tk coverage for the redesign and manual OCR correction workflow."""
import copy
import queue
import threading

import pytest

import word_lookup as w


def test_dark_yellow_app_and_library_keep_editable_fields(app, card):
    assert app.root.cget("background") == "#111318"
    w.add_saved_word(card)
    app.open_saved_words()
    app.show_saved_details(card["word"])
    app.root.update()
    assert app.library_title.get() == "Бібліотека · 1 слів"
    assert set(app.editor_fields) >= {"translation", "definition", "notes", "context"}
    assert app.editor_fields["notes"].cget("background") == "#1d2029"
    assert app.saved_tree.item(app._saved_tree_id(card["word"]), "values")[-1] == card["source"]


def test_word_title_opens_keyboard_correction_and_escape_keeps_card(app, card):
    app.show_lookup(card, None, (400, 350, 500, 380))
    popup = app.popup
    popup.edit_word()
    app.root.update()
    assert popup.word_editor.winfo_ismapped()
    assert popup.pinned
    popup.word_var.set("changing")
    assert popup.cancel_word_edit() == "break"
    assert app.popup is popup
    assert popup.word_var.get() == card["word"]
    assert not popup.correction_row.winfo_ismapped()


def test_correction_submit_validates_and_reuses_card_anchor(app, card, monkeypatch):
    app.show_lookup(card, None, (400, 350, 500, 380))
    popup = app.popup
    calls = []
    monkeypatch.setattr(app, "retry_word", lambda *args: calls.append(args))
    monkeypatch.setattr(app, "toast", lambda *_: None)
    popup.word_var.set("two words")
    popup.submit_word()
    assert calls == []
    popup.word_var.set("changing")
    popup.submit_word()
    assert calls == [("changing", card, popup.anchor)]


def test_retry_word_preserves_context_without_ocr_capture_or_pause(app, card, monkeypatch):
    # A real worker posts events, and the UI consumes only events of the active token.
    card["word"] = "changin"
    previous = copy.deepcopy(card)
    previous["sentence"] = "He was changin his mind."
    previous["sentence_tr"] = "Він передумував."
    w.cfg["popup"]["translate_definition"] = False
    calls = []
    finished = threading.Event()

    def builder(word, on_partial, context=""):
        calls.append((word, context))
        result = dict(card, word=word, translation="змінювати")
        on_partial(result)
        return result, True

    class CaptureQueue(queue.Queue):
        def put(self, event, *args, **kwargs):
            super().put(event, *args, **kwargs)
            if event[0] == "update":
                finished.set()

    def forbidden(*args, **kwargs):
        raise AssertionError("Manual correction must reuse captured context")

    monkeypatch.setattr(w, "events", CaptureQueue())
    monkeypatch.setattr(w, "build_word_card_fast", builder)
    monkeypatch.setattr(w, "find_word_at", forbidden)
    monkeypatch.setattr(w, "_grab", forbidden)
    monkeypatch.setattr(app, "_begin_game_pause", forbidden)
    app.retry_word("changing", previous, (400, 350, 500, 380))
    assert finished.wait(2)
    app._pump()
    assert calls == [("changing", previous["context"])]
    assert app.popup.card["word"] == "changing"
    assert app.popup.card["sentence"] == previous["sentence"]
    assert app.popup.card["sentence_tr"] == previous["sentence_tr"]
    assert app.popup.card["source"] == previous["source"]


def test_partial_update_preserves_word_correction_draft(app, card):
    app.show_lookup(card, None, (400, 350, 500, 380))
    app.popup.edit_word()
    app.root.update()
    app.popup.word_var.set('chang')
    app.popup.word_editor.icursor(3)
    app.rerender(app.popup)
    app.root.update()
    assert app.popup.correction_row.winfo_ismapped()
    assert app.popup.word_var.get() == 'chang'
    assert app.popup.word_editor.index('insert') == 3


def test_global_escape_cancels_word_edit_before_dismissing_card(app, card):
    app.show_lookup(card, None, (400, 350, 500, 380))
    popup = app.popup
    popup.edit_word()
    app.root.update()
    app._handle(('esc',))
    assert app.popup is popup
    assert not popup.correction_row.winfo_ismapped()
    app._handle(('esc',))
    assert app.popup is None


def test_tk_escape_then_global_hook_keeps_card(app, card):
    app.show_lookup(card, None, (400, 350, 500, 380))
    popup = app.popup
    popup.edit_word()
    app.root.update()
    popup.cancel_word_edit(object())
    app._handle(('esc',))
    assert app.popup is popup


@pytest.mark.parametrize('width,font_size,scaling,word', [
    (width, font_size, scaling, word)
    for width in (300, 400)
    for font_size in (11, 24)
    for scaling in (1.0, 2.0)
    for word in ('understatement', 'pneumonoultramicroscopicsilicovolcanoconiosis')
])
def test_headword_and_actions_fit_narrow_and_scaled_cards(app, card, width, font_size, scaling, word):
    previous_scaling = app.root.tk.call('tk', 'scaling')
    try:
        app.root.tk.call('tk', 'scaling', scaling)
        w.cfg['popup'].update(width=width, font_size=font_size)
        card['word'] = word
        app.show_lookup(card, None, (400, 200, 450, 230))
        app.root.update()
        popup = app.popup
        title = popup.word_title
        viewport = popup.viewport.canvas
        left, right = viewport.winfo_rootx(), viewport.winfo_rootx() + viewport.winfo_width()
        assert title.winfo_width() >= title.winfo_reqwidth()
        assert title.winfo_height() >= title.winfo_reqheight()
        assert left <= title.winfo_rootx()
        assert title.winfo_rootx() + title.winfo_width() <= right
        for button in popup.toolbar_buttons:
            assert button.winfo_ismapped()
            assert button.winfo_width() >= button.winfo_reqwidth()
            assert left <= button.winfo_rootx()
            assert button.winfo_rootx() + button.winfo_width() <= right
            assert button.winfo_rooty() + button.winfo_height() <= title.winfo_rooty()
        # Pin text gets longer; actions must still fit after this interaction.
        popup.toggle_pin()
        app.root.update()
        assert all(button.winfo_width() >= button.winfo_reqwidth() for button in popup.toolbar_buttons)
    finally:
        app.close_all()
        app.root.tk.call('tk', 'scaling', previous_scaling)
