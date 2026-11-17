"""Disk refresh, stale-write protection and reversible JSON recovery."""
import json
from pathlib import Path

import pytest

import word_lookup as w
from word_lookup import _library as storage


def write_library(entries, path=None, *, bom=False):
    path = Path(path or w._saved_words_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8-sig" if bom else "utf-8")
    return path


def test_open_library_reads_file_that_appeared_after_initial_empty_cache(app):
    assert w.load_saved_words() == []
    write_library([{"word": f"sample-{index}", "translation": "переклад"} for index in range(35)])
    app.open_saved_words()
    app.root.update()
    assert len(app.saved_tree.get_children()) == 35
    assert app.library_title.get() == "Бібліотека · 35 слів"
    assert str(w._saved_words_path()) in app.library_storage_info.get()


def test_reopening_library_refreshes_count_and_existing_editor(app):
    write_library([{"word": "sample", "translation": "before"}])
    app.open_saved_words()
    app.root.update()
    write_library([{"word": "sample", "translation": "after"}, {"word": "another"}])
    app.open_saved_words()
    app.root.update()
    assert len(app.saved_tree.get_children()) == 2
    app.show_saved_details("sample")
    assert app.editor_fields["translation"].get("1.0", "end-1c") == "after"


def test_refresh_preserves_entry_and_list_identity_for_review(card):
    w.add_saved_word(card)
    entries = w.load_saved_words()
    entry = entries[0]
    assert w.reload_saved_words() is entries
    assert entries[0] is entry
    w.record_review(entry, True)
    assert w.read_saved_words_file(w._saved_words_path())[0]["review"]["attempts"] == 1


def test_lookups_use_cache_without_polling_disk(monkeypatch):
    write_library([{"word": "sample"}])
    assert len(w.load_saved_words()) == 1
    def unexpected(*_args):
        raise AssertionError("A lookup should not reparse the library")
    monkeypatch.setattr(storage, "_read_saved_file", unexpected)
    for _ in range(10):
        assert w.is_word_saved("sample")


def test_utf8_bom_library_is_loaded_and_remains_writable(card):
    path = write_library([{"word": "existing", "notes": "Збережена нотатка"}], bom=True)
    assert w.load_saved_words()[0]["notes"] == "Збережена нотатка"
    assert w.add_saved_word(card)
    assert len(w.read_saved_words_file(path)) == 2


@pytest.mark.parametrize("raw", [b"broken JSON", b"{}", b'[{}, {"word":"valid"}]', b"\xff\xfe"])
def test_read_error_is_visible_and_cannot_be_saved_as_empty_library(raw, card):
    path = Path(w._saved_words_path())
    path.write_bytes(raw)
    assert w.load_saved_words() == []
    assert "Не вдалося прочитати" in w.saved_words_error()
    assert not w.add_saved_word(card)
    assert path.read_bytes() == raw


def test_reload_read_error_preserves_last_loaded_entries_read_only(card):
    w.add_saved_word(card)
    previous = w.load_saved_words()
    Path(w._saved_words_path()).write_bytes(b"damaged")
    assert w.reload_saved_words() is previous
    assert len(previous) == 1
    assert w.saved_words_error()
    assert not w.add_saved_word(dict(card, word="second"))
    assert Path(w._saved_words_path()).read_bytes() == b"damaged"


def test_stale_empty_cache_cannot_overwrite_file_created_by_another_copy(card):
    assert w.load_saved_words() == []
    path = write_library([{"word": "saved-in-another-copy"}])
    original = path.read_bytes()
    assert not w.add_saved_word(card)
    assert path.read_bytes() == original
    assert "змінився" in w.saved_words_error()
    w.reload_saved_words()
    assert w.add_saved_word(card)
    assert len(w.read_saved_words_file(path)) == 2


def test_valid_empty_library_does_not_resurrect_deleted_words_from_backup():
    path = write_library([])
    write_library([{"word": "deleted-intentionally"}], path.with_suffix(".backups") / "saved_words-0001.json")
    assert w.reload_saved_words() == []
    assert not w.saved_words_error()
    assert path.read_text() == "[]"


def test_import_merges_missing_words_preserving_edits_progress_and_source(tmp_path):
    current = [{"word": "existing", "translation": "my edit", "notes": "private note",
                "edited_fields": ["translation"], "review": {"attempts": 8}}]
    path = write_library(current)
    original = path.read_bytes()
    w.load_saved_words()
    source = write_library([{"word": "EXISTING", "translation": "old translation", "review": {"attempts": 1}},
                            {"word": "restored", "context": "synthetic context", "status": "learning"}],
                           tmp_path / "selected.json")
    source_bytes = source.read_bytes()
    result = w.restore_saved_words(source)
    assert result["added"] == 1 and result["total"] == 2
    assert w.saved_entry("existing") == current[0]
    assert w.saved_entry("restored")["context"] == "synthetic context"
    assert Path(result["checkpoint"]).read_bytes() == original
    assert source.read_bytes() == source_bytes
    assert len(w.read_saved_words_file(path)) == 2


def test_importing_active_file_refreshes_stale_cache_without_rewriting():
    assert w.load_saved_words() == []
    path = write_library([{"word": "existing"}])
    original = path.read_bytes()
    result = w.restore_saved_words(path)
    assert result == {"added": 0, "total": 1, "checkpoint": ""}
    assert len(w.load_saved_words()) == 1
    assert path.read_bytes() == original


def test_explicit_restore_keeps_exact_corrupt_original(tmp_path):
    path = Path(w._saved_words_path())
    corrupt = b"\xff\xfe damaged original"
    path.write_bytes(corrupt)
    assert w.load_saved_words() == []
    source = write_library([{"word": "restored"}], tmp_path / "selected.json")
    result = w.restore_saved_words(source)
    assert result["total"] == 1
    assert Path(result["checkpoint"]).read_bytes() == corrupt
    assert w.load_saved_words()[0]["word"] == "restored"
    assert not w.saved_words_error()


def test_failed_restore_commit_leaves_current_file_and_memory_intact(tmp_path, monkeypatch):
    path = write_library([{"word": "current"}])
    original = path.read_bytes()
    previous = w.load_saved_words()
    source = write_library([{"word": "restored"}], tmp_path / "selected.json")
    def disk_failure(*_args, **_kwargs):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(w, "atomic_json_write", disk_failure)
    with pytest.raises(OSError):
        w.restore_saved_words(source)
    assert w.load_saved_words() is previous
    assert previous == [{"word": "current"}]
    assert path.read_bytes() == original
    checkpoints = list(path.with_suffix(".recovery").glob("*.json"))
    assert len(checkpoints) == 1 and checkpoints[0].read_bytes() == original


def test_restore_aborts_if_another_copy_changes_file_while_checkpoint_is_written(tmp_path, monkeypatch):
    path = write_library([{"word": "current"}])
    w.load_saved_words()
    source = write_library([{"word": "restored"}], tmp_path / "selected.json")
    preserve = storage._preserve_before_restore
    def external_change(destination):
        checkpoint = preserve(destination)
        write_library([{"word": "newer-external-word"}])
        return checkpoint
    monkeypatch.setattr(storage, "_preserve_before_restore", external_change)
    with pytest.raises(OSError, match="інша копія"):
        w.restore_saved_words(source)
    assert w.read_saved_words_file(path) == [{"word": "newer-external-word"}]


def test_refresh_resets_filters_that_hide_saved_words(app):
    write_library([{"word": "sample"}])
    app.open_saved_words()
    app.root.update()
    app.saved_query.set("no-match")
    app.saved_state_filter.set("Знаю")
    app.saved_due_only.set(True)
    assert not app.saved_tree.get_children()
    app.reload_library()
    app.root.update()
    assert app.saved_query.get() == ""
    assert app.saved_state_filter.get() == "Усі"
    assert not app.saved_due_only.get()
    assert len(app.saved_tree.get_children()) == 1


def test_recovery_button_imports_selected_file_and_shows_all_entries(app, tmp_path, monkeypatch):
    app.open_saved_words()
    source = write_library([{"word": "restored", "notes": "synthetic note"}], tmp_path / "selected.json")
    monkeypatch.setattr(w.filedialog, "askopenfilename", lambda **_kwargs: str(source))
    app.restore_library_file()
    app.root.update()
    assert app.library_title.get() == "Бібліотека · 1 слів"
    assert len(app.saved_tree.get_children()) == 1
    assert w.saved_entry("restored")["notes"] == "synthetic note"


def test_cancelled_refresh_keeps_unsaved_editor_and_disk(app, monkeypatch):
    path = write_library([{"word": "sample", "notes": "original"}])
    app.open_saved_words()
    app.root.update()
    app.editor_fields["notes"].insert("end", " unsaved")
    original = path.read_bytes()
    monkeypatch.setattr(w.messagebox, "askyesnocancel", lambda *_args, **_kwargs: None)
    assert not app.reload_library()
    assert "unsaved" in app.editor_fields["notes"].get("1.0", "end")
    assert path.read_bytes() == original
    app._editor_dirty = False
    for field in app.editor_fields.values():
        field.edit_modified(False)
