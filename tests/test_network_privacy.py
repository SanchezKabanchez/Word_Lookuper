"""Network switches must be enforced before HTTP, including redirects and audio."""
from types import SimpleNamespace

import pytest
import word_lookup as w
from word_lookup_network import (NetworkDisabled, provider_for_url,
                                 remote_request)


def policy(*, offline=False, only=None):
    return {"network": {"offline": offline, "providers": {
        name: only is None or name == only
        for name in ("google", "mymemory", "datamuse", "dictionaryapi", "deepl")}}}


@pytest.mark.parametrize("method,url", [
    ("get", "https://translate.googleapis.com/translate_a/single"),
    ("get", "https://clients5.google.com/translate_a/t"),
    ("get", "https://translate.google.com/m"),
    ("get", "https://api.mymemory.translated.net/get"),
    ("get", "https://api.datamuse.com/words"),
    ("get", "https://api.dictionaryapi.dev/api/v2/entries/en/test"),
    ("post", "https://api-free.deepl.com/v2/translate"),
    ("get", "https://api.deepl.com/v2/usage"),
])
def test_offline_blocks_every_remote_entry_point_before_http(monkeypatch, method, url):
    w.cfg.update(policy(offline=True))
    monkeypatch.setattr(w.REQUEST_GATE, "wait", lambda: pytest.fail("Disabled request consumed a permit"))
    with pytest.raises(NetworkDisabled, match="Офлайн"):
        getattr(w, "_remote_" + method)(url)


def test_deepl_only_does_not_send_words_to_fallbacks_or_dictionaries(monkeypatch):
    w.cfg.update(policy(only="deepl"))
    w.cfg["deepl"].update(enabled=True, key="test:fx")
    calls = []
    response = SimpleNamespace(status_code=200, headers={}, raise_for_status=lambda: None,
                               json=lambda: {"translations": [{"text": "тест"}]})
    monkeypatch.setattr(w.REQUEST_GATE, "wait", lambda: None)
    monkeypatch.setattr(w.SESSION, "post", lambda url, **kw: (calls.append((url, kw)), response)[1])
    card = w.build_word_card("test")
    assert card["translation"] == "тест"
    assert card["meanings"] == []
    assert [url for url, _kw in calls] == ["https://api-free.deepl.com/v2/translate"]
    assert calls[0][1]["allow_redirects"] is False


def test_disabled_google_is_skipped_before_fallback_function(monkeypatch):
    w.cfg.update(policy(only="mymemory"))
    monkeypatch.setattr(w, "_PROVIDERS", (("Google", lambda *a: pytest.fail("Google was called")),
                                         ("MyMemory", lambda *a: ("тест", []))))
    monkeypatch.setattr(w, "_cooldown", {})
    assert w.translate_with_fallback("test", "en", True) == ("тест", [], "MyMemory")


def test_failed_deepl_only_has_no_google_fallback(monkeypatch):
    w.cfg.update(policy(only="deepl"))
    w.cfg["deepl"].update(enabled=True, key="test:fx")
    monkeypatch.setattr(w, "_try_deepl", lambda *a: None)
    with pytest.raises(w.TranslateError, match="джерело перекладу"):
        w.build_word_card("uncached")


def test_existing_card_and_sentence_cache_remain_available_offline(monkeypatch):
    w.cfg["deepl"].update(enabled=True, key="test:fx")
    monkeypatch.setattr(w, "_try_deepl", lambda *a: "Зачекай тут.")
    assert w.translate_text("Wait here.") == "Зачекай тут."
    card = {**w.SAMPLE_CARD, "word": "test", "translation": "тест"}
    w._cache_word_card(w._cache_key("test"), card)
    w.cfg.update(policy(offline=True))
    monkeypatch.setattr(w, "_try_deepl", lambda *a: pytest.fail("Cached translation recomputed"))
    assert w.translate_text("Wait here.") == "Зачекай тут."
    assert w.build_word_card("test")["translation"] == "тест"


def test_offline_uncached_word_explains_missing_cache():
    w.cfg.update(policy(offline=True))
    with pytest.raises(w.TranslateError, match="Офлайн.*кеш"):
        w.build_word_card("uncached")


def test_offline_audio_does_not_even_start_worker(monkeypatch):
    w.cfg.update(policy(offline=True))
    monkeypatch.setattr(w.threading, "Thread", lambda *a, **kw: pytest.fail("Started audio worker"))
    w.play_audio("https://api.dictionaryapi.dev/media/pronunciations/en/test-us.mp3")


@pytest.mark.parametrize("url", ["http://translate.google.com/m", "https://translate.google.com.evil.test/m",
                                 "https://user:password@translate.google.com/m", "https://translate.google.com:123/m"])
def test_url_validation_rejects_unknown_or_insecure_destinations(url):
    assert provider_for_url(url) is None


def test_redirect_to_other_provider_never_sends_request_or_authentication():
    calls, closed = [], []
    response = SimpleNamespace(status_code=307, headers={"Location": "https://translate.google.com/m"},
                               close=lambda: closed.append(True))
    session = SimpleNamespace(post=lambda url, **kw: (calls.append((url, kw)), response)[1])
    with pytest.raises(NetworkDisabled, match="перенаправлення"):
        remote_request(lambda: policy(), session, SimpleNamespace(wait=lambda: None), "post",
                       "https://api-free.deepl.com/v2/translate", headers={"Authorization": "secret"},
                       data={"text": "private"})
    assert len(calls) == 1
    assert closed == [True]
    assert calls[0][1]["allow_redirects"] is False


def test_switching_offline_while_waiting_for_budget_blocks_request():
    config = policy()
    def wait():
        config["network"]["offline"] = True
    with pytest.raises(NetworkDisabled):
        remote_request(lambda: config, SimpleNamespace(get=lambda *a, **kw: pytest.fail("HTTP sent")),
                       SimpleNamespace(wait=wait), "get", "https://translate.google.com/m")


def test_same_provider_redirect_rechecks_policy_and_discards_initial_params():
    calls, closed = [], []
    responses = iter([
        SimpleNamespace(status_code=302, headers={"Location": "/result"}, close=lambda: closed.append(True)),
        SimpleNamespace(status_code=200, headers={}),
    ])
    session = SimpleNamespace(get=lambda url, **kw: (calls.append((url, kw)), next(responses))[1])
    result = remote_request(lambda: policy(), session, SimpleNamespace(wait=lambda: None), "get",
                            "https://translate.google.com/m", params={"q": "private"})
    assert result.status_code == 200
    assert [url for url, _kw in calls] == ["https://translate.google.com/m", "https://translate.google.com/result"]
    assert "params" not in calls[1][1]
    assert closed == [True]


def test_manual_word_spelling_is_authoritative_through_dictionary_executor(monkeypatch):
    # "changin" normally triggers an explicit OCR fix; the user may intend the
    # dialect spelling or a proper name and must be able to keep it unchanged.
    calls = []
    monkeypatch.setattr(w, "_translate_word", lambda word, context="": (word, [], "", False))
    def dictionary(word):
        calls.append(w.correct_ocr_word(word))
        return {"base": word, "phonetic": "", "audios": [], "meanings": []}
    monkeypatch.setattr(w, "english_dictionary", dictionary)
    assert w.correct_ocr_word("changin") == "changing"
    with w.manual_word_scope():
        card = w.build_word_card("changin")
    assert card["word"] == "changin"
    assert card["translation"] == "changin"
    assert calls == ["changin"]
    assert w.correct_ocr_word("changin") == "changing"


def test_dictionary_recording_on_google_host_also_respects_google_switch(monkeypatch):
    w.cfg.update(policy(only="dictionaryapi"))
    with pytest.raises(NetworkDisabled, match="Аудіо Google"):
        w._remote_get("https://ssl.gstatic.com/dictionary/static/sounds/test.mp3", audio=True)
