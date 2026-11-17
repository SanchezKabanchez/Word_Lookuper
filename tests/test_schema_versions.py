import json
from pathlib import Path

import pytest

import word_lookup as w
from word_lookup_schemas import UnsupportedSchema, decode_cards, decode_words
from wl_network import TranslationCache


def test_legacy_library_is_read_and_next_save_writes_versioned_envelope(card):
    path = Path(w._saved_words_path())
    path.write_text('[{"word":"legacy","notes":"keep"}]')
    assert w.load_saved_words()[0]["notes"] == "keep"
    assert isinstance(json.loads(path.read_text()), list)
    assert w.add_saved_word(card)
    payload = json.loads(path.read_text())
    assert payload["schema"] == 1 and payload["kind"] == "wordlookup.library"
    assert len(decode_words(payload)) == 2


def test_future_library_schema_cannot_be_saved_or_repaired_over(card, tmp_path):
    path = Path(w._saved_words_path())
    original = b'{"schema":999,"kind":"wordlookup.library","words":[{"word":"future"}]}'
    path.write_bytes(original)
    assert w.load_saved_words() == [] and w.saved_words_error()
    assert not w.add_saved_word(card)
    source = tmp_path / "older.json"
    source.write_text('[{"word":"older"}]')
    with pytest.raises(UnsupportedSchema):
        w.restore_saved_words(source)
    assert path.read_bytes() == original


def test_future_cache_schema_survives_app_exit_until_explicit_clear():
    path = Path(w._cache_path())
    original = b'{"schema":999,"kind":"wordlookup.card-cache","cards":{"keep":"future"}}'
    path.write_bytes(original)
    w.load_card_cache()
    w.save_card_cache()
    assert path.read_bytes() == original
    w.clear_card_cache()
    w.save_card_cache()
    assert decode_cards(json.loads(path.read_text())) == {}


def test_future_text_cache_is_preserved_until_explicit_clear(tmp_path):
    path = tmp_path / "translations.json"
    original = '{"schema":999,"kind":"wordlookup.text-cache","translations":{}}'
    path.write_text(original)
    cache = TranslationCache()
    assert cache.translate("word", lambda: "fresh", path=str(path)) == "fresh"
    assert path.read_text() == original
    cache.clear(str(path))
    cache.translate("another", lambda: "new value", path=str(path))
    assert json.loads(path.read_text())["schema"] == 1
