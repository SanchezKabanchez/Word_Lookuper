"""Saved progress must survive concurrent export, edits and failed commits."""
import copy
import json
import threading
from pathlib import Path

import pytest
import word_lookup as w


def incomplete(card):
    return dict(card, meanings=[], definition_tr='', groups=[], translation='переклад')


def test_enrichment_timeout_cannot_mutate_saved_entry_after_return(card, monkeypatch):
    w.add_saved_word(incomplete(card))
    entry = w.load_saved_words()[0]
    started, release, completed = threading.Event(), threading.Event(), threading.Event()
    def slow_enrich(candidate):
        started.set()
        assert release.wait(2)
        candidate['definition'] = 'late network result'
        completed.set()
        return True
    monkeypatch.setattr(w, 'enrich_entry', slow_enrich)
    def timeout_after_worker_starts(*args, **kwargs):
        assert started.wait(2)
        raise TimeoutError('controlled export deadline')
        yield  # the iterator raises only after the worker is running
    monkeypatch.setattr(w, 'as_completed', timeout_after_worker_starts)
    try:
        assert w.enrich_entries([entry], timeout=0.03) == 0
        assert started.is_set()
    finally:
        release.set()
        assert completed.wait(2)
    assert entry['definition'] == ''
    assert w.read_saved_words_file(w._saved_words_path())[0]['definition'] == ''


def test_enrichment_does_not_overwrite_manual_edit_made_during_request(card, monkeypatch):
    w.add_saved_word(incomplete(card))
    entry = w.load_saved_words()[0]
    started, release = threading.Event(), threading.Event()
    def delayed(candidate):
        started.set()
        assert release.wait(2)
        candidate.update(definition='network result', example='network example', enriched=True)
        return True
    monkeypatch.setattr(w, 'enrich_entry', delayed)
    errors = []
    def export():
        try:
            w.enrich_entries([entry], timeout=2)
        except Exception as error:
            errors.append(error)
    worker = threading.Thread(target=export)
    worker.start()
    try:
        assert started.wait(2)
        w.update_saved_entry(entry['word'], {'definition': 'My manual definition'})
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and errors == []
    assert entry['definition'] == 'My manual definition'
    assert entry['example'] == 'network example'
    persisted = w.read_saved_words_file(w._saved_words_path())[0]
    assert persisted['definition'] == 'My manual definition'


def test_failed_add_cannot_rollback_another_concurrent_success(card, monkeypatch):
    first_entered, release_first, second_done = threading.Event(), threading.Event(), threading.Event()
    real_write = w._write_saved_words
    first_result, second_result = [], []
    def selected_write():
        if threading.current_thread().name == 'failing-save':
            first_entered.set()
            assert release_first.wait(2)
            return False
        return real_write()
    monkeypatch.setattr(w, '_write_saved_words', selected_write)
    first = threading.Thread(name='failing-save', target=lambda: first_result.append(
        w.add_saved_word(dict(card, word='first'))))
    def second_save():
        second_result.append(w.add_saved_word(dict(card, word='second')))
        second_done.set()
    second = threading.Thread(name='successful-save', target=second_save)
    first.start()
    try:
        assert first_entered.wait(2)
        second.start()
        second_done.wait(0.05)
    finally:
        release_first.set()
        first.join(3)
        second.join(3)
    assert first_result == [False] and second_result == [True]
    assert [entry['word'] for entry in w.load_saved_words()] == ['second']
    assert [entry['word'] for entry in w.read_saved_words_file(w._saved_words_path())] == ['second']


def test_failed_enrichment_commit_restores_memory_and_disk(card, monkeypatch):
    w.add_saved_word(incomplete(card))
    entry = w.load_saved_words()[0]
    original = copy.deepcopy(entry)
    def enrich(candidate):
        candidate['definition'] = 'network result'
        return True
    monkeypatch.setattr(w, 'enrich_entry', enrich)
    monkeypatch.setattr(w, '_write_saved_words', lambda: False)
    with pytest.raises(OSError, match='дозаповнення'):
        w.enrich_entries([entry])
    assert entry == original
    assert w.read_saved_words_file(w._saved_words_path()) == json.loads(json.dumps([original]))


def test_enrichment_does_not_resurrect_a_word_deleted_during_request(card, monkeypatch):
    w.add_saved_word(incomplete(card))
    entry = w.load_saved_words()[0]
    started, release = threading.Event(), threading.Event()
    def delayed(candidate):
        started.set()
        assert release.wait(2)
        candidate['definition'] = 'network result'
        return True
    monkeypatch.setattr(w, 'enrich_entry', delayed)
    errors = []
    def export():
        try:
            w.enrich_entries([entry], timeout=2)
        except Exception as error:
            errors.append(error)
    worker = threading.Thread(target=export)
    worker.start()
    try:
        assert started.wait(2)
        assert w.remove_saved_word(entry['word'])
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors
    assert w.load_saved_words() == []
    assert w.read_saved_words_file(w._saved_words_path()) == []


@pytest.mark.parametrize('word', ['', ' \t\n', None, 123])
def test_invalid_saved_word_is_rejected_without_creating_progress_file(word):
    assert not w.add_saved_word({'word': word})
    assert w.saved_entry(word) is None
    assert not w.is_word_saved(word)
    assert not w.remove_saved_word(word)
    assert not Path(w._saved_words_path()).exists()


def test_backup_pruning_skips_reading_retained_libraries_without_expired_images(card, monkeypatch):
    from word_lookup import _library as storage
    w.add_saved_word(card)
    for revision in range(7):
        w.update_saved_entry(card['word'], {'notes': str(revision)})
    def unnecessary_scan():
        raise AssertionError('No expired images need an ownership scan')
    monkeypatch.setattr(storage, '_backup_image_names', unnecessary_scan)
    w.update_saved_entry(card['word'], {'notes': 'final'})
    assert w.saved_entry(card['word'])['notes'] == 'final'


def test_anki_emphasis_does_not_highlight_inside_html_escape_entities():
    assert w._emphasize('An amp & speaker.', ['amp'], 'b') == 'An <b>amp</b> &amp; speaker.'


def test_text_export_uses_one_snapshot_when_original_entries_change(tmp_path, monkeypatch):
    entries = [{'word': 'first', 'translation': 'one'}, {'word': 'second', 'translation': 'before'}]
    real_back = w._anki_back
    def back(entry):
        entries[1]['translation'] = 'changed concurrently'
        return real_back(entry)
    monkeypatch.setattr(w, '_anki_back', back)
    path = tmp_path / 'words.txt'
    w.export_anki_txt(entries, str(path))
    assert 'second\tbefore\n' in path.read_text(encoding="utf-8")
    assert 'changed concurrently' not in path.read_text(encoding="utf-8")


def test_empty_dictionary_result_does_not_disable_future_export_enrichment(card, monkeypatch):
    entry = {'word': card['word'], 'translation': 'переклад', 'definition': ''}
    monkeypatch.setattr(w, 'english_dictionary', lambda word: {'meanings': [], 'dict_note': 'Offline'})
    assert not w.enrich_entry(entry)
    assert not entry.get('enriched')
    assert w._needs_enrich(dict(entry, enriched=True))  # repair a prior failed lookup marker


def test_detached_enrichment_still_works_without_saving_it_as_library_progress(monkeypatch):
    entry = {'word': 'hello', 'translation': 'привіт'}
    def enrich(candidate):
        candidate['definition'] = 'A greeting.'
        return True
    monkeypatch.setattr(w, 'enrich_entry', enrich)
    assert w.enrich_entries([entry]) == 1
    assert entry['definition'] == 'A greeting.'
    assert not Path(w._saved_words_path()).exists()


def test_missing_cache_on_startup_and_clear_does_not_log_an_error(monkeypatch):
    errors = []
    monkeypatch.setattr(w, 'log_exc', lambda *args: errors.append(args))
    w.load_card_cache()
    w.clear_card_cache()
    assert errors == []


def test_invalid_review_metadata_is_treated_as_due_and_repaired_on_grade(card):
    w.add_saved_word(card)
    entry = w.load_saved_words()[0]
    entry['review'] = None
    assert w.review_due(entry)
    stats = w.record_review(entry, True)
    assert stats['attempts'] == 1 and stats['streak'] == 1
