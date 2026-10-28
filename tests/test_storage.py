import copy
import json
import os
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image
import word_lookup as w


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
    assert json.loads((tmp_path / "settings.json").read_text()) == {"old": True}
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
    assert json.load(open(w._cache_path())) == {}


def test_parallel_cache_writes_are_valid_json(card):
    w.cfg["cache"]["max_entries"] = 20
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n: w._cache_word_card(str(n), card), range(80)))
    w.save_card_cache()
    data = json.load(open(w._cache_path()))
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
    assert not os.path.exists(image_path)


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
    with sqlite3.connect(tmp_path / "collection.anki2") as db:
        fields = db.execute("select flds from notes").fetchone()[0]
        assert "применшення" in fields
        assert "understatement" in fields
        assert entry["image_file"] in fields
        assert "<b>understatement</b>" in fields
        assert db.execute("select count(*) from cards").fetchone()[0] == 1


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
    assert "some-test-secret" not in open(w.log_path()).read()
