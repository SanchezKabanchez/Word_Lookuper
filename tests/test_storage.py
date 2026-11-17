from contextlib import closing
import json
import os
import sqlite3
import zipfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image
import word_lookup as w
from word_lookup_schemas import decode_cards


def test_corrupt_config_sections_do_not_break_defaults():
    w.atomic_json_write(w.config_path(), {"version": "broken", "popup": None,
                                        "ocr": {"region_w": "invalid"}, "languages": {"target": "pl"}})
    loaded = w.load_config()
    assert loaded["popup"] == w.DEFAULT_CONFIG["popup"]
    assert loaded["ocr"]["region_w"] == w.DEFAULT_CONFIG["ocr"]["region_w"]
    assert loaded["languages"]["target"] == "pl"


def test_atomic_write_preserves_previous_file_on_failure(tmp_path, monkeypatch):
    path = str(tmp_path / "settings.json")
    w.atomic_json_write(path, {"old": True})
    def fail(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(w.os, "replace", fail)
    with pytest.raises(OSError):
        w.atomic_json_write(path, {"new": True})
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8")) == {"old": True}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["settings.json"]


def test_cache_is_bounded_lru_and_does_not_share_context(card):
    w.cfg["cache"]["max_entries"] = 2
    for name in ("a", "b"):
        w._cache_word_card(name, card)
    first = w.cached_word_card("a")
    first["meanings"][0]["defs"][0]["d"] = "Changed elsewhere"
    first["context"] = "Other game"
    w._cache_word_card("c", card)
    assert w.cached_word_card("b") is None
    assert w.cached_word_card("a")["context"] == card["context"]
    assert w.cached_word_card("a")["meanings"] == card["meanings"]
    w.cfg["cache"]["max_entries"] = 0
    assert w.cached_word_card("a") is None
    w.save_card_cache()
    assert decode_cards(json.load(open(w._cache_path(), encoding="utf-8"))) == {}


def test_parallel_cache_writes_are_valid_json(card):
    w.cfg["cache"]["max_entries"] = 20
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: w._cache_word_card(str(n), card), range(80)))
    w.save_card_cache()
    data = decode_cards(json.load(open(w._cache_path(), encoding="utf-8")))
    assert len(data) == 20
    assert all(v["word"] == card["word"] for v in data.values())


def test_saved_words_survive_restart_with_context_and_image(card):
    card["_context_image"] = Image.new("RGB", (1400, 800), "navy")
    assert w.add_saved_word(card)
    assert not w.add_saved_word(card)
    w._saved_words = None
    entry = w.load_saved_words()[0]
    assert entry["context"] == card["context"]
    assert entry["source"] == "Example game"
    image_path = w.context_image_path(entry["image_file"])
    with Image.open(image_path) as img:
        assert img.width <= 960 and img.height <= 480
    assert w.saved_word_card(entry)["sentence_tr"] == "Це применшення."
    assert w.remove_saved_word(card["word"].upper())
    # A previous version can be restored together with its screenshot.
    assert os.path.exists(image_path)


def test_saved_words_keep_five_previous_versions_and_prune_unused_images(card):
    card["_context_image"] = Image.new("RGB", (100, 80), "navy")
    assert w.add_saved_word(card)
    image_path = w.context_image_path(w.load_saved_words()[0]["image_file"])
    assert w.remove_saved_word(card["word"])
    path = Path(w._saved_words_path())
    assert w.read_saved_words_file(path) == []
    assert os.path.exists(image_path)
    for revision in range(7):
        w._saved_words = [{"word": str(revision), "review": {"attempts": revision}}]
        assert w._write_saved_words()
    backups = sorted(path.with_suffix(".backups").glob("*.json"))
    assert len(backups) == 5
    assert [w.read_saved_words_file(p)[0]["word"] for p in backups] == ["1", "2", "3", "4", "5"]
    assert w.read_saved_words_file(path)[0]["word"] == "6"
    assert not os.path.exists(image_path)


@pytest.mark.parametrize("failed_destination", ["backup", "current"])
def test_backup_and_main_write_failures_preserve_saved_progress(card, monkeypatch, failed_destination):
    assert w.add_saved_word(card)
    previous = Path(w._saved_words_path()).read_bytes()
    real_write = w.atomic_json_write
    def fail_selected(path, data, **kwargs):
        is_backup = Path(path).parent.suffix == ".backups"
        if is_backup == (failed_destination == "backup"):
            raise OSError("disk unavailable")
        return real_write(path, data, **kwargs)
    monkeypatch.setattr(w, "atomic_json_write", fail_selected)
    assert not w.remove_saved_word(card["word"])
    assert w.is_word_saved(card["word"])
    path = Path(w._saved_words_path())
    assert path.read_bytes() == previous
    assert not list(path.with_suffix(".backups").glob("*.json"))


def test_corrupt_existing_progress_is_not_overwritten(card):
    path = Path(w._saved_words_path())
    path.write_text("broken progress", encoding="utf-8")
    assert not w.add_saved_word(card)
    assert path.read_text(encoding="utf-8") == "broken progress"


def test_profile_anki_tag_survives_restart_and_takes_precedence(card):
    w.cfg["anki"]["game"] = "Global game"
    card["anki_tag"] = "Cyberpunk 2077"
    assert w.add_saved_word(card)
    w._saved_words = None
    entry = w.load_saved_words()[0]
    assert w._entry_tag(entry) == "Cyberpunk_2077"
    assert w.saved_word_card(entry)["anki_tag"] == "Cyberpunk 2077"


def test_search_matches_translation_context_and_source():
    entries = [{"word": "chest", "translation": "скриня", "source": "Game A", "added": "2026-01-02"},
               {"word": "apple", "translation": "яблуко", "context": "In the garden", "added": "2026-01-03"}]
    assert w.filter_saved_words(entries, "СКРИНЯ game")[0]["word"] == "chest"
    assert w.filter_saved_words(entries, "GARDEN")[0]["word"] == "apple"
    assert w.filter_saved_words(entries, "not found") == []
    assert [e["word"] for e in w.filter_saved_words(entries, order="az")] == ["apple", "chest"]
    assert w.filter_saved_words(entries, order="oldest")[0]["word"] == "chest"


def test_anki_package_contains_fields_context_and_media(card, tmp_path):
    card["_context_image"] = Image.new("RGB", (100, 80), "navy")
    w.add_saved_word(card)
    entry = w.load_saved_words()[0]
    path = tmp_path / "deck.apkg"
    w.export_anki_apkg([entry], str(path))
    with zipfile.ZipFile(path) as archive:
        media = json.loads(archive.read("media"))
        assert entry["image_file"] in media.values()
        assert all(archive.read(key).startswith(b"\xff\xd8") for key in media)
        archive.extract("collection.anki2", tmp_path)
    with closing(sqlite3.connect(tmp_path / "collection.anki2")) as db:
        fields = db.execute("select flds from notes").fetchone()[0]
        assert "применшення" in fields
        assert "understatement" in fields
        assert entry["image_file"] in fields
        assert "<b>understatement</b>" in fields
        assert db.execute("select count(*) from cards").fetchone()[0] == 1
        models = json.loads(db.execute("select models from col").fetchone()[0])
        assert "{{tts en_US:Word}}" in next(iter(models.values()))["tmpls"][0]["qfmt"]
        assert all(not name.endswith((".mp3", ".wav")) for name in media.values())


def test_media_path_rejects_directory_traversal():
    assert w.context_image_path("../../config.json") is None
    assert w.context_image_path("/tmp/context-bad.jpg") is None


def test_saved_partial_is_enriched_without_duplicates(card):
    partial = dict(card, meanings=[], sentence_tr="", definition_tr="")
    assert w.add_saved_word(partial)
    assert w.saved_entry(card["word"])["definition"] == ""
    assert w.add_saved_word(card)
    assert len(w.load_saved_words()) == 1
    restored = w.saved_word_card(w.saved_entry(card["word"]))
    assert restored["meanings"] == card["meanings"]
    assert restored["audios"] == card["audios"]
    assert restored["sentence_tr"] == card["sentence_tr"]


def test_secret_redacted_in_console_and_file(capsys):
    w.log("DeepL-Auth-Key some-test-secret")
    assert "some-test-secret" not in capsys.readouterr().out
    assert "some-test-secret" not in open(w.log_path(), encoding="utf-8").read()
