"""Behavioral domain checks usable on macOS without a display or global hooks."""
import copy
import gettext
import json
from pathlib import Path
import pytest
from word_lookup_config import DEFAULT_CONFIG, load_settings
from word_lookup_preferences import Settings
from word_lookup_storage import Library
from word_lookup_schemas import UnsupportedSchema, decode_words, encode_words
from word_lookup_models import ShowCard, UpdateCard, as_event
from word_lookup_i18n import translator
from scripts.compile_translations import compile_catalog
from scripts.summarize_fps import summarize


class QuietLog:
    def write(self, _message):
        pass
    def exception(self, _where=''):
        pass


@pytest.fixture
def library(tmp_path):
    settings = Settings(copy.deepcopy(DEFAULT_CONFIG), lambda: str(tmp_path / 'config.json'), QuietLog())
    return Library(settings, QuietLog())


def test_library_round_trip_preserves_progress_and_manual_edits(library):
    assert library.add_saved_word({'kind': 'word', 'word': 'kindness', 'translation': 'доброта'})
    assert library.update_saved_entry('kindness', {'notes': 'remember me'})
    library.reload_saved_words()
    entry = library.saved_entry('kindness')
    assert entry['translation'] == 'доброта'
    assert entry['notes'] == 'remember me'
    assert library.saved_words_error() == ''


def test_unreadable_future_library_is_not_overwritten(library):
    path = Path(library._saved_words_path())
    original = json.dumps({'schema': 999, 'kind': 'wordlookup.library', 'words': []})
    path.write_text(original, encoding='utf-8')
    assert library.load_saved_words() == []
    assert not library.add_saved_word({'kind': 'word', 'word': 'test', 'translation': 'тест'})
    assert path.read_text(encoding='utf-8') == original


def test_public_schema_accepts_legacy_lists():
    words = [{'word': 'legacy', 'translation': 'спадщина'}]
    assert decode_words(words) == decode_words(encode_words(words))
    with pytest.raises(UnsupportedSchema):
        decode_words({'schema': 999, 'words': words})


@pytest.mark.parametrize('value', [True, -50, 9999, 'large', None])
def test_bad_font_sizes_do_not_escape_settings_schema(tmp_path, value):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'popup': {'font_size': value}}), encoding='utf-8')
    loaded = load_settings(str(path))
    assert type(loaded.values['popup']['font_size']) is int
    assert 8 <= loaded.values['popup']['font_size'] <= 24


def test_named_event_round_trip_has_fields():
    card = {'word': 'hello'}
    event = as_event(('show', card, None, None, 42))
    assert isinstance(event, ShowCard)
    assert event.card is card and event.token == 42
    assert UpdateCard(42, 'hello', card).word == 'hello'


def test_gettext_catalog_compiles_and_is_utf8(tmp_path):
    source = Path(__file__).parents[1] / 'wordlookup/locale/en/LC_MESSAGES/wordlookup.po'
    output = tmp_path / 'messages.mo'
    compile_catalog(source, output)
    with output.open('rb') as stream:
        catalog = gettext.GNUTranslations(stream)
    assert catalog.gettext('Копіювати') == 'Copy'
    assert translator('uk').gettext('Копіювати') == 'Копіювати'


def test_presentmon_summary_uses_real_frame_intervals(tmp_path):
    sample = tmp_path / 'capture.csv'
    sample.write_text('ProcessID,SwapChainAddress,msBetweenPresents\n1,a,10\n1,a,20\n1,a,30\n2,b,5\n', encoding="utf-8")
    result = summarize(sample)
    assert result['frames'] == 3
    assert result['average_fps'] == 50
    assert result['p99_ms'] == 30
