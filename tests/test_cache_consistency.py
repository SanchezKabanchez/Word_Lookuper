"""Cache clearing and setting changes stay coherent with concurrent lookups."""
import json
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest
import word_lookup as w
from wl_network import TranslationCache
from word_lookup_jobs import LookupJob


def empty_dictionary():
    return {"base": "", "phonetic": "", "audios": [], "meanings": []}


def test_text_cache_clear_does_not_join_or_restore_an_older_inflight_result(tmp_path):
    cache = TranslationCache()
    path = str(tmp_path / "translations.json")
    began, release, fresh_began = threading.Event(), threading.Event(), threading.Event()
    def old_compute():
        began.set()
        assert release.wait(3)
        return "before clear"
    def fresh_compute():
        fresh_began.set()
        return "after clear"
    with ThreadPoolExecutor(2) as pool:
        old = pool.submit(cache.translate, "key", old_compute, path=path)
        assert began.wait(2)
        cache.clear(path)
        fresh = pool.submit(cache.translate, "key", fresh_compute, path=path)
        try:
            assert fresh_began.wait(2), "New lookup joined the pre-clear computation"
        finally:
            release.set()
        assert old.result(timeout=2) == "before clear"
        assert fresh.result(timeout=2) == "after clear"
    assert cache.translate("key", lambda: pytest.fail("Fresh cache result was lost"), path=path) == "after clear"
    assert json.loads((tmp_path / "translations.json").read_text())["translations"] == {"key": "after clear"}


def test_text_cache_clear_without_new_lookup_leaves_deleted_file_deleted(tmp_path):
    cache = TranslationCache()
    path = str(tmp_path / "translations.json")
    began, release = threading.Event(), threading.Event()
    def compute():
        began.set()
        assert release.wait(3)
        return "cleared private text"
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(cache.translate, "key", compute, path=path)
        assert began.wait(2)
        cache.clear(path)
        release.set()
        assert pending.result(timeout=2) == "cleared private text"
    assert not (tmp_path / "translations.json").exists()
    assert not cache._values


def test_smaller_text_cache_budget_applies_even_on_a_cache_hit():
    cache = TranslationCache()
    for value in range(5):
        cache.translate(str(value), lambda value=value: str(value))
    assert cache.translate("1", lambda: pytest.fail("Existing translation was recomputed"), limit=1) == "1"
    assert list(cache._values) == ["1"]


@pytest.mark.parametrize("fast", [False, True])
def test_card_cache_clear_cannot_be_undone_by_an_older_builder(monkeypatch, fast):
    began, release = threading.Event(), threading.Event()
    def translation(*args):
        began.set()
        assert release.wait(3)
        return "переклад", [], "", False
    monkeypatch.setattr(w, "_translate_word", translation)
    monkeypatch.setattr(w, "english_dictionary", lambda word: empty_dictionary())
    builder = w.build_word_card_fast if fast else w.build_word_card
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(builder, "marker")
        assert began.wait(2)
        w.clear_card_cache()
        release.set()
        result = pending.result(timeout=2)
    assert (result[0] if fast else result)["translation"] == "переклад"
    assert not w._card_cache


def test_definition_enrichment_after_clear_does_not_restore_the_card(monkeypatch):
    job = LookupJob(SimpleNamespace(active_token=42, settings=w._settings, cache=w._cache, library=w._library,
        providers=w._providers, ocr=w._ocr, logger=w._app_log, events=w.events), "word", 20, 20, 42,
                    {"word": "marker", "line_text": "Current marker sentence."}, None, "Game", None)
    def translation(text):
        w.clear_card_cache()
        return "Переклад визначення."
    monkeypatch.setattr(w, "translate_text", translation)
    card = {"word": "marker", "meanings": [{"defs": [{"d": "A definition."}]}]}
    job.complete(card, False)
    assert job.state["definition_tr"] == "Переклад визначення."
    assert not w._card_cache


def test_translation_fallback_keeps_the_language_used_by_its_cache_key(monkeypatch):
    began, release = threading.Event(), threading.Event()
    calls = []
    def delayed_deepl(*args):
        began.set()
        assert release.wait(3)
        return None
    class Response:
        status_code = 200
        headers = {}
        def __init__(self, language):
            self.language = language
        def raise_for_status(self):
            pass
        def json(self):
            return [[[self.language + " translation"]]]
    def get(url, **kwargs):
        language = kwargs["params"]["tl"]
        calls.append(language)
        return Response(language)
    monkeypatch.setattr(w, "_try_deepl", delayed_deepl)
    monkeypatch.setattr(w.REQUEST_GATE, "wait", lambda: None)
    monkeypatch.setattr(w.SESSION, "get", get)
    monkeypatch.setattr(w, "_cooldown", {})
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(w.translate_text, "Current sentence.")
        assert began.wait(2)
        w.cfg["languages"]["target"] = "de"
        release.set()
        assert pending.result(timeout=2) == "uk translation"
    w.cfg["languages"]["target"] = "uk"
    assert w.translate_text("Current sentence.") == "uk translation"
    w.cfg["languages"]["target"] = "de"
    assert w.translate_text("Current sentence.") == "de translation"
    assert calls == ["uk", "de"]


def test_word_and_dictionary_executor_share_original_language_snapshot(monkeypatch):
    began, release = threading.Event(), threading.Event()
    def translation(*args):
        began.set()
        assert release.wait(3)
        return w._target_language() + " translation", [], "", False
    def dictionary(word):
        assert release.wait(3)
        return {**empty_dictionary(), "meanings": [{"pos": w.pos_name("noun"), "defs": [], "syn": [], "ant": []}]}
    monkeypatch.setattr(w, "_translate_word", translation)
    monkeypatch.setattr(w, "english_dictionary", dictionary)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(w.build_word_card, "marker")
        assert began.wait(2)
        w.cfg["languages"]["target"] = "de"
        release.set()
        card = pending.result(timeout=2)
    assert card["translation"] == "uk translation"
    assert card["meanings"][0]["pos"] == "іменник"
    assert list(w._card_cache)[0].startswith("marker|uk|")


def test_lookup_job_definition_keeps_its_captured_language(monkeypatch):
    job = LookupJob(SimpleNamespace(active_token=42, settings=w._settings, cache=w._cache, library=w._library,
        providers=w._providers, ocr=w._ocr, logger=w._app_log, events=w.events), "word", 20, 20, 42,
                    {"word": "marker", "line_text": "Current marker sentence."}, None, "Game", None)
    w.cfg["languages"]["target"] = "de"
    monkeypatch.setattr(w, "translate_text", lambda text: w._target_language() + " definition")
    job.complete({"word": "marker", "meanings": [{"defs": [{"d": "Definition."}]}]}, False)
    assert job.state["definition_tr"] == "uk definition"
    assert list(w._card_cache)[0].startswith("marker|uk|")


def test_empty_ambiguous_irregular_lookup_does_not_repeat_the_same_provider_chain(monkeypatch):
    calls = []
    monkeypatch.setattr(w, "_english_lookup", lambda word: calls.append(word) or empty_dictionary())
    assert not w.english_dictionary("saw")["meanings"]
    assert calls == ["saw", "see"]


def test_dictionary_disabled_during_lookup_is_not_cached_as_complete(monkeypatch):
    calls = []
    def disabled_lookup(word):
        calls.append(word)
        w.cfg["network"]["providers"].update(dictionaryapi=False, datamuse=False)
        return empty_dictionary()
    monkeypatch.setattr(w, "english_dictionary", disabled_lookup)
    monkeypatch.setattr(w, "_translate_word", lambda *a: ("переклад", [], "", False))
    assert w.build_word_card("marker")["_dictionary_complete"] is False
    w.cfg["network"]["providers"].update(dictionaryapi=True, datamuse=True)
    monkeypatch.setattr(w, "english_dictionary", lambda word: calls.append(word) or {
        **empty_dictionary(), "phonetic": "fresh phonetic"})
    assert w.build_word_card("marker")["phonetic"] == "fresh phonetic"
    assert calls == ["marker", "marker"]


def test_enabling_new_dictionary_source_loads_its_missing_features(monkeypatch):
    w.cfg["network"]["providers"]["dictionaryapi"] = False
    calls = []
    monkeypatch.setattr(w, "english_dictionary", lambda word: calls.append(word) or empty_dictionary())
    monkeypatch.setattr(w, "_translate_word", lambda *a: ("переклад", [], "", False))
    assert w.build_word_card("marker")["_dictionary_sources"] == ["datamuse"]
    w.cfg["network"]["providers"]["dictionaryapi"] = True
    monkeypatch.setattr(w, "english_dictionary", lambda word: calls.append(word) or {
        **empty_dictionary(), "phonetic": "fresh phonetic"})
    assert w.build_word_card_fast("marker")[0]["phonetic"] == "fresh phonetic"
    assert calls == ["marker", "marker"]


def test_timed_out_dictionary_stage_cannot_continue_provider_fallbacks(monkeypatch):
    began, release, cancelled, done = (threading.Event() for _ in range(4))
    late_requests = []
    class ShortFuture:
        def __init__(self, future):
            self.future = future
        def result(self, timeout=None):
            return self.future.result(timeout=0.03)
    class ShortExecutor:
        def __init__(self, **kwargs):
            self.pool = ThreadPoolExecutor(**kwargs)
        def submit(self, *args, **kwargs):
            return ShortFuture(self.pool.submit(*args, **kwargs))
        def shutdown(self, **kwargs):
            return self.pool.shutdown(**kwargs)
    def dictionary(word):
        began.set()
        try:
            assert release.wait(3)
            try:
                w.check_cancelled()  # The next provider request's actual guard.
            except w.LookupCancelled:
                cancelled.set()
                raise
            late_requests.append(word)
            return empty_dictionary()
        finally:
            done.set()
    monkeypatch.setattr(w, "ThreadPoolExecutor", ShortExecutor)
    monkeypatch.setattr(w, "english_dictionary", dictionary)
    monkeypatch.setattr(w, "_translate_word", lambda *a: ("переклад", [], "", False))
    try:
        card, _ = w.build_word_card_fast("marker")
        assert began.is_set()
        assert card["dict_note"]
    finally:
        release.set()
    assert done.wait(2)
    assert cancelled.is_set()
    assert not late_requests
