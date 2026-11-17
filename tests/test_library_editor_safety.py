"""Unsaved editor drafts and failed image/database writes must stay recoverable."""
from pathlib import Path

from PIL import Image
import pytest

import word_lookup as w


@pytest.fixture
def library_editor(app, card, monkeypatch):
    w.add_saved_word(card)
    app.open_saved_words()
    app.show_saved_details(card["word"])
    app.root.update()
    monkeypatch.setattr(w.messagebox, "askyesnocancel", lambda *args, **kwargs: False)
    yield app
    app._editor_dirty = False
    for field in getattr(app, "editor_fields", {}).values():
        if field.winfo_exists():
            field.edit_modified(False)


def test_discarded_draft_is_not_resurrected_when_library_reopens(library_editor):
    app = library_editor
    app.editor_fields["notes"].insert("1.0", "discard this draft")
    app.root.update()
    assert app._editor_dirty
    app._hide_saved_window()
    app.open_saved_words()
    app.root.update()
    assert app.editor_fields["notes"].get("1.0", "end-1c") == ""
    assert not app._editor_dirty
    assert not w.saved_entry(app._editor_word).get("notes")


def test_successful_save_consumes_pending_text_modified_event(library_editor):
    app = library_editor
    app.editor_fields["notes"].insert("1.0", "save immediately")
    # The widget has changed, but <<Modified>> has not reached the binding yet.
    assert app.editor_fields["notes"].edit_modified()
    assert app.save_saved_details()
    app.root.update()
    assert not app._editor_dirty
    assert w.saved_entry(app._editor_word)["notes"] == "save immediately"


def test_fast_close_cannot_skip_unsaved_change_prompt(library_editor, monkeypatch):
    app = library_editor
    prompts = []
    monkeypatch.setattr(w.messagebox, "askyesnocancel",
                        lambda *args, **kwargs: prompts.append(args) or None)
    app.editor_fields["notes"].insert("1.0", "unprocessed draft")
    app._hide_saved_window()
    assert len(prompts) == 1
    assert app.saved_win.state() != "withdrawn"
    assert app._editor_dirty
    assert app.editor_fields["notes"].get("1.0", "end-1c") == "unprocessed draft"


def test_failed_save_preserves_editor_draft_and_previous_persisted_data(library_editor, monkeypatch):
    app = library_editor
    word = app._editor_word
    app.editor_fields["notes"].insert("1.0", "retry me")
    app.root.update()
    monkeypatch.setattr(w, "_write_saved_words", lambda: False)
    assert not app.save_saved_details()
    assert app._editor_dirty
    assert app.editor_fields["notes"].get("1.0", "end-1c") == "retry me"
    assert "Не вдалося" in app.editor_message.get()
    assert not w.saved_entry(word).get("notes")


def test_cancelled_image_chooser_does_not_keep_explicitly_discarded_text(library_editor, monkeypatch):
    app = library_editor
    app.editor_fields["notes"].insert("1.0", "discard before choosing image")
    app.root.update()
    monkeypatch.setattr(w.filedialog, "askopenfilename", lambda **kwargs: "")
    app.replace_saved_image()
    assert app.editor_fields["notes"].get("1.0", "end-1c") == ""
    assert not app._editor_dirty


def test_failed_image_replacement_removes_new_file_and_retains_old_image(library_editor, tmp_path, monkeypatch):
    app = library_editor
    old_name = w.save_context_image(Image.new("RGB", (50, 40), "navy"))
    w.update_saved_entry(app._editor_word, {"image_file": old_name})
    app.show_saved_details(app._editor_word)
    app.root.update()
    media = Path(w.context_image_path(old_name)).parent
    existing = set(media.iterdir())
    source = tmp_path / "replacement.png"
    Image.new("RGB", (80, 60), "green").save(source)
    monkeypatch.setattr(w.filedialog, "askopenfilename", lambda **kwargs: str(source))
    monkeypatch.setattr(w, "_write_saved_words", lambda: False)
    app.replace_saved_image()
    assert set(media.iterdir()) == existing
    assert Path(w.context_image_path(old_name)).exists()
    assert w.saved_entry(app._editor_word)["image_file"] == old_name
    assert "Зображення не додано" in app.editor_message.get()


def test_open_saved_card_resumes_game_and_rejects_previous_lookup_events(library_editor, card, monkeypatch):
    app = library_editor
    w.saved_entry(card["word"])["anki_tag"] = "saved_tag"
    app._lookup_profile = {"anki_tag": "live_game_tag"}
    old_token = app._new_token()
    released = []

    class Guard:
        active = True

        def release(self):
            released.append(True)
            self.active = False
            return True, "resumed"

    app._game_pause = Guard()
    app._game_pause_hwnd = 123
    monkeypatch.setattr(w, "_restore_foreground", lambda hwnd: None)
    app.open_saved_word(card["word"])
    assert released == [True]
    assert app._game_pause is None
    assert app._lookup_profile == {}
    assert app._lookup_profiles[app.active_token] == {}
    assert app.popup.card["anki_tag"] == "saved_tag"
    assert app.popup.card["source"] == card["source"]
    saved_popup = app.popup
    late = dict(card, translation="late lookup translation")
    app._handle(("show", late, None, None, old_token))
    app._handle(("update", old_token, card["word"], late))
    assert app.popup is saved_popup
    assert app.popup.card["translation"] == card["translation"]


def test_open_saved_card_closes_previous_selection_inputs(library_editor):
    app = library_editor
    closed = []

    class Overlay:
        def __init__(self, name):
            self.name = name

        def finish(self, result):
            closed.append((self.name, result))
            setattr(app, self.name, None)

    app.selector = Overlay("selector")
    app.free_cursor = Overlay("free_cursor")
    app.open_saved_word(app._editor_word)
    assert closed == [("selector", None), ("free_cursor", None)]
    assert app.selector is None and app.free_cursor is None


def test_notes_save_preserves_fields_filled_after_editor_opened(library_editor, card):
    app = library_editor
    entry = w.saved_entry(card["word"])
    entry["definition"] = ""
    app.show_saved_details(card["word"])
    app.root.update()
    app.editor_fields["notes"].insert("1.0", "my new note")
    app.root.update()
    # A completed lookup fills a previously absent field while notes are edited.
    completed = dict(card, meanings=[{"pos": "noun", "defs": [{"d": "newly arrived definition", "e": ""}],
                                    "syn": [], "ant": []}])
    assert w.add_saved_word(completed)
    w.update_saved_entry(card["word"], {"status": "known"})
    assert app.save_saved_details()
    assert entry["definition"] == "(noun) newly arrived definition"
    assert entry["notes"] == "my new note"
    assert entry["status"] == "known"
    assert app.editor_fields["definition"].get("1.0", "end-1c") == "(noun) newly arrived definition"
    assert app.editor_status.get() == "Знаю"
    app.root.update()
    assert not app._editor_dirty


def test_explicit_clear_in_editor_is_still_persisted(library_editor):
    app = library_editor
    word = app._editor_word
    assert app.editor_fields["definition"].get("1.0", "end-1c")
    app.editor_fields["definition"].delete("1.0", "end")
    assert app.save_saved_details()
    assert w.saved_entry(word)["definition"] == ""
    assert "definition" in w.saved_entry(word)["edited_fields"]
