"""Request budgets/cache tests use local fakes; no external services required."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import pytest
import word_lookup as w
from wl_network import (LookupCancelled, RequestGate, TranslationCache,
                        correct_ocr_word, network_scope, scoped_task)


def test_sentence_cache_deduplicates_concurrent_misses_and_survives_restart(tmp_path):
    cache = TranslationCache()
    path = str(tmp_path / "translations.json")
    started, release = threading.Event(), threading.Event()
    calls = []
    def translate():
        calls.append(1)
        started.set()
        assert release.wait(2)
        return "Стій тут."
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(cache.translate, "uk|Stand here.", translate, path=path)
        assert started.wait(2)
        second = pool.submit(cache.translate, "uk|Stand here.", translate, path=path)
        release.set()
        assert first.result() == second.result() == "Стій тут."
    assert calls == [1]
    restored = TranslationCache()
    assert restored.translate("uk|Stand here.", lambda: pytest.fail("Cache miss"), path=path) == "Стій тут."


def test_failed_translation_is_retryable_and_cache_is_bounded(tmp_path):
    cache = TranslationCache()
    path = str(tmp_path / "translations.json")
    with pytest.raises(ValueError):
        cache.translate("bad", lambda: (_ for _ in ()).throw(ValueError()), path=path)
    assert cache.translate("bad", lambda: "good", path=path, limit=2) == "good"
    for key in ["two", "three"]:
        cache.translate(key, lambda: key, path=path, limit=2)
    assert list(json.loads((tmp_path / "translations.json").read_text())) == ["two", "three"]
    cache.clear(path)
    assert not (tmp_path / "translations.json").exists()


def test_sentence_cache_language_provider_and_disabled_setting_are_distinct(monkeypatch):
    calls = []
    monkeypatch.setattr(w, "_try_deepl", lambda *a: None)
    monkeypatch.setattr(w, "translate_with_fallback", lambda text, *a: (calls.append(text) or text + "!", [], "fake"))
    assert w.translate_text("Wait  here.") == w.translate_text("Wait here.") == "Wait here.!"
    assert calls == ["Wait here."]
    w.cfg["languages"]["target"] = "de"
    w.translate_text("Wait here.")
    assert len(calls) == 2
    w.cfg["cache"]["max_entries"] = 0
    w.translate_text("Wait here.")
    assert len(calls) == 3


def test_gate_spaces_competing_providers_and_cancellation_consumes_no_slot():
    current = [10.0]
    gate = RequestGate(0.4, clock=lambda: current[0], sleep=lambda seconds: current.__setitem__(0, current[0] + seconds))
    gate.wait()
    with network_scope(lambda: True):
        with pytest.raises(LookupCancelled):
            gate.wait()
    gate.wait()
    assert current[0] == pytest.approx(10.4)
    gate.wait()
    assert current[0] == pytest.approx(10.8)


def test_provider_tasks_copy_cancellation_before_sending_http(monkeypatch):
    with network_scope(lambda: True):
        task = scoped_task(w._p_gtx, "wait", "en", False)
    with ThreadPoolExecutor(1) as pool:
        with pytest.raises(LookupCancelled):
            pool.submit(task).result()


@pytest.mark.parametrize("raw,expected", [("changin", "changing"), ("Footlng", "Footing"), ("NOTHLNG", "NOTHING"),
                                         ("form", "form"), ("Cyberpunk", "Cyberpunk")])
def test_local_ocr_corrections_are_conservative(raw, expected):
    assert correct_ocr_word(raw) == expected


def test_irregular_base_candidate_needs_no_network():
    assert w.base_form_candidates("fought")[:2] == ["fight", "fought"]
    assert w.base_form_candidates("children")[:2] == ["child", "children"]
    assert w.base_form_candidates("saw")[0] == "saw"  # noun is independently meaningful


def test_dictionary_stops_after_success_instead_of_querying_suffixes(monkeypatch):
    calls = []
    class Response:
        status_code = 200
        def json(self):
            return [{"word": "running", "meanings": [{"partOfSpeech": "noun", "definitions": [{"definition": "Moving quickly."}]}]}]
    monkeypatch.setattr(w, "_remote_get", lambda url, **kw: (calls.append(url), Response())[1])
    assert w._freedictionary_lookup("running")["meanings"]
    assert len(calls) == 1


def test_deepl_success_skips_duplicate_google_translation(monkeypatch):
    monkeypatch.setattr(w, "_deepl_active", lambda: True)
    monkeypatch.setattr(w, "_try_deepl", lambda *a: "чекати")
    monkeypatch.setattr(w, "_google_word", lambda *a: pytest.fail("Duplicate Google request"))
    assert w._translate_word("wait", "Wait here.") == ("чекати", [], "", False)


def test_provider_requests_share_one_gate_including_deepl(monkeypatch):
    permits = []
    monkeypatch.setattr(w.REQUEST_GATE, "wait", lambda: permits.append(1))
    monkeypatch.setattr(w.SESSION, "get", lambda *a, **k: object())
    monkeypatch.setattr(w.SESSION, "post", lambda *a, **k: object())
    w._remote_get("https://api.dictionaryapi.dev/example")
    w._remote_get("https://translate.google.com/m")
    w._remote_get("https://api.datamuse.com/words")
    w._remote_post("https://api-free.deepl.com/v2/translate")
    assert len(permits) == 4


def test_cancelled_cache_owner_does_not_cancel_current_waiter():
    cache = TranslationCache()
    started, release = threading.Event(), threading.Event()
    old_cancelled = threading.Event()
    def old():
        with network_scope(old_cancelled.is_set):
            def compute():
                started.set()
                assert release.wait(2)
                return "old"
            return cache.translate("same", compute)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(old)
        assert started.wait(2)
        second = pool.submit(cache.translate, "same", lambda: "current")
        old_cancelled.set()
        release.set()
        with pytest.raises(LookupCancelled):
            first.result()
        assert second.result() == "current"
