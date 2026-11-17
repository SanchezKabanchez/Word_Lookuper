"""Skip unused work while keeping newly enabled content and scene context fresh."""
import copy
import threading
from types import SimpleNamespace

import pytest
import word_lookup as w
from word_lookup_jobs import LookupJob


def disable_dictionary_content():
    w.cfg["sections"].update(definitions=False, thesaurus=False)
    w.cfg["popup"].update(phonetic=False, btn_speak=False)


def job_for():
    hit = {"word": "marker", "line_text": "Original context marker.", "box": (10, 10, 90, 30)}
    return LookupJob(SimpleNamespace(active_token=42, settings=w._settings, cache=w._cache, library=w._library,
        providers=w._providers, ocr=w._ocr, logger=w._app_log, events=w.events), "word", 20, 20, 42,
                     hit, None, "Current game", None)


def cached_card(card):
    cached = dict(copy.deepcopy(card), word="marker", context="Original context marker.",
                  sentence="Original context marker.", source="Old game", image_file="old.png", anki_tag="OldGame")
    w._cache_word_card(w._cache_key("marker", "Original context marker."), cached)
    return cached


@pytest.mark.parametrize("fast", [False, True])
def test_hidden_dictionary_content_creates_no_dictionary_worker(monkeypatch, fast):
    disable_dictionary_content()
    monkeypatch.setattr(w, "english_dictionary", lambda word: pytest.fail("Unused dictionary lookup"))
    monkeypatch.setattr(w, "ThreadPoolExecutor", lambda *a, **kw: pytest.fail("Unused executor"))
    monkeypatch.setattr(w, "_translate_word", lambda *a: ("переклад", [], "", False))
    if fast:
        result, partial = w.build_word_card_fast("marker", lambda card: pytest.fail("Redundant partial card"))
        assert partial is False
    else:
        result = w.build_word_card("marker")
    assert result["translation"] == "переклад"
    assert result["meanings"] == []
    assert result["_dictionary_complete"] is False


@pytest.mark.parametrize("section,key", [("sections", "definitions"), ("sections", "thesaurus"),
                                         ("popup", "phonetic"), ("popup", "btn_speak")])
def test_each_visible_dictionary_feature_keeps_its_lookup(monkeypatch, section, key):
    disable_dictionary_content()
    w.cfg[section][key] = True
    monkeypatch.setattr(w, "AUDIO_OK", False)  # An audio preference still asks for recordings.
    calls = []
    monkeypatch.setattr(w, "english_dictionary", lambda word: calls.append(word) or {
        "base": word, "phonetic": "phonetic", "audios": [], "meanings": []})
    monkeypatch.setattr(w, "_translate_word", lambda *a: ("переклад", [], "", False))
    assert w.build_word_card("marker")["_dictionary_complete"] is True
    assert calls == ["marker"]


def test_translation_only_cache_cannot_hide_newly_enabled_definitions(monkeypatch):
    disable_dictionary_content()
    monkeypatch.setattr(w, "_translate_word", lambda *a: ("переклад", [], "", False))
    w.build_word_card("marker")
    w.cfg["sections"]["definitions"] = True
    calls = []
    monkeypatch.setattr(w, "english_dictionary", lambda word: calls.append(word) or {
        "base": word, "phonetic": "", "audios": [],
        "meanings": [{"pos": "noun", "defs": [{"d": "Fresh definition.", "e": ""}], "syn": [], "ant": []}]})
    result = w.build_word_card_fast("marker")[0]
    assert calls == ["marker"]
    assert result["meanings"][0]["defs"][0]["d"] == "Fresh definition."
    assert result["_dictionary_complete"] is True


@pytest.mark.parametrize("builder", [w.build_word_card, w.build_word_card_fast])
def test_cancelled_dictionary_does_not_produce_or_cache_a_completed_card(monkeypatch, builder):
    def cancelled(word):
        raise w.LookupCancelled("newer lookup")
    monkeypatch.setattr(w, "english_dictionary", cancelled)
    monkeypatch.setattr(w, "_translate_word", lambda *a: ("переклад", [], "", False))
    with pytest.raises(w.LookupCancelled):
        builder("marker")
    assert not w._card_cache


def test_cancelled_google_lookup_is_not_converted_to_a_translation_failure(monkeypatch):
    def cancelled(*args):
        raise w.LookupCancelled("newer lookup")
    monkeypatch.setattr(w, "translate_with_fallback", cancelled)
    with pytest.raises(w.LookupCancelled):
        w._google_word("marker")


def test_exact_cached_context_skips_ocr_and_keeps_current_game_metadata(monkeypatch, card):
    old = cached_card(card)
    monkeypatch.setattr(w, "build_word_card_fast", lambda *a, **kw: pytest.fail("Repeated cached lookup"))
    monkeypatch.setattr(w, "find_sentence", lambda *a: pytest.fail("Repeated OCR"))
    monkeypatch.setattr("word_lookup_jobs.threading", SimpleNamespace(Lock=threading.Lock,
                      Thread=lambda *a, **kw: pytest.fail("Unneeded sentence worker")))
    job = job_for()
    job.word()
    assert job.state["sentence"] == "Original context marker."
    assert job.state["sentence_tr"] == old["sentence_tr"]
    assert job.state["source"] == "Current game"
    assert job.state["sentence_pending"] is False
    assert "image_file" not in job.state
    assert "anki_tag" not in job.state


def test_other_scene_cached_context_is_not_reused(monkeypatch, card):
    old = cached_card(card)
    key = w._cache_key("marker", "Original context marker.")
    w._card_cache[key] = dict(old, context="A different old scene.", sentence="A different old scene.")
    calls = []
    class ImmediateThread:
        def __init__(self, target, daemon=True):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr("word_lookup_jobs.threading", SimpleNamespace(Lock=threading.Lock, Thread=ImmediateThread))
    monkeypatch.setattr(w, "find_sentence", lambda *a: calls.append(1) or "The current full sentence.")
    monkeypatch.setattr(w, "translate_text", lambda text: "Поточне речення.")
    monkeypatch.setattr(w, "build_word_card_fast", lambda *a, **kw: pytest.fail("Repeated dictionary work"))
    job = job_for()
    job.word()
    assert calls == [1]
    assert job.state["sentence"] == "The current full sentence."
    assert job.state["sentence_tr"] == "Поточне речення."
    assert job.state["source"] == "Current game"


def test_hidden_context_does_not_spawn_sentence_ocr(monkeypatch):
    w.cfg["popup"].update(sentence=False, sentence_translation=False, translate_definition=False)
    monkeypatch.setattr(w, "find_sentence", lambda *a: pytest.fail("Hidden context OCR"))
    monkeypatch.setattr("word_lookup_jobs.threading", SimpleNamespace(Lock=threading.Lock,
                      Thread=lambda *a, **kw: pytest.fail("Hidden context worker")))
    monkeypatch.setattr(w, "build_word_card_fast", lambda *a, **kw: ({"word": "marker", "translation": "переклад"}, False))
    job_for().word()


def test_unchanged_card_updates_do_not_request_another_popup_render():
    job = job_for()
    job.emit("update", {"context": job.context, "sentence": job.context})
    assert w.events.empty()
    job.emit("update", {"translation": "переклад"})
    job.emit("update", {"translation": "переклад"})
    assert w.events.qsize() == 1
    job.emit("show", {})  # First presentation still needs an event, even if unchanged.
    assert w.events.qsize() == 2
