"""Concurrent lookup stages retain complete cards and close partial failures."""
import queue
import threading
from types import SimpleNamespace

import word_lookup as w
from word_lookup_jobs import LookupJob


def job_for(word="marker"):
    hit = {"word": word, "line_text": "Original context marker.", "box": (10, 10, 90, 30)}
    return LookupJob(SimpleNamespace(active_token=42, settings=w._settings, cache=w._cache, library=w._library,
        providers=w._providers, ocr=w._ocr, logger=w._app_log, events=w.events,
        post_lookup=lambda token, kind, *args: w.events.put(w.as_event(("lookup_event", token, (kind, *args))))), "word", 20, 20, 42,
                     hit, None, "Example game", None)


def test_failed_dictionary_finishes_existing_partial_card(monkeypatch):
    w.cfg["popup"].update(sentence=False, sentence_translation=False)
    w.cfg["ocr"]["highlight"] = False
    def builder(word, partial, context=""):
        partial({"word": word, "translation": "переклад", "dict_pending": True})
        raise RuntimeError("dictionary timed out")
    monkeypatch.setattr(w, "build_word_card_fast", builder)
    job_for().run()
    shown, finished = w.events.get_nowait(), w.events.get_nowait()
    assert shown[0] == "show"
    assert finished[0] == "update"
    card = finished[3]
    assert card["translation"] == "переклад"
    assert card["context"] == "Original context marker."
    assert card["source"] == "Example game"
    assert card["dict_pending"] is False
    assert card["note"] == "dictionary timed out"


def test_later_sentence_completion_keeps_completed_word_card(monkeypatch):
    w.cfg["ocr"]["highlight"] = False
    w.cfg["popup"].update(sentence=True, sentence_translation=True, translate_definition=False)
    started, release, completed = threading.Event(), threading.Event(), threading.Event()
    def sentence(*args):
        started.set()
        assert release.wait(2), "Test did not release sentence OCR"
        return "The complete original sentence."
    def builder(word, partial, context=""):
        assert started.wait(2), "Sentence OCR did not start in parallel"
        partial({"word": word, "translation": "переклад", "dict_pending": True})
        return {"word": word, "translation": "переклад", "dict_pending": False}, True
    class TrackingQueue(queue.Queue):
        def put(self, event, *args, **kwargs):
            super().put(event, *args, **kwargs)
            if event[0] == "update" and event[3].get("sentence_pending") is False:
                completed.set()
    monkeypatch.setattr(w, "events", TrackingQueue())
    monkeypatch.setattr(w, "find_sentence", sentence)
    monkeypatch.setattr(w, "build_word_card_fast", builder)
    monkeypatch.setattr(w, "translate_text", lambda text: "Повне перекладене речення.")
    try:
        job_for().run()
    finally:
        release.set()
    assert completed.wait(2), "Sentence worker did not publish completion"
    snapshots = []
    while not w.events.empty():
        event = w.events.get_nowait()
        snapshots.append(event[1] if event[0] == "show" else event[3])
    final = snapshots[-1]
    assert final["translation"] == "переклад"
    assert final["dict_pending"] is False
    assert final["sentence"] == "The complete original sentence."
    assert final["sentence_tr"] == "Повне перекладене речення."


def test_definition_enrichment_caches_only_card_data_and_preserves_live_context(monkeypatch):
    w.cfg["popup"]["translate_definition"] = True
    cached = []
    monkeypatch.setattr(w, "_cache_word_card", lambda key, card, **kwargs: cached.append((key, dict(card))))
    monkeypatch.setattr(w, "translate_text", lambda text: "Переклад визначення.")
    job = job_for()
    job.emit("update", {"sentence": "A wider captured sentence.", "context": "A wider captured sentence."})
    card = {"word": "marker", "meanings": [{"defs": [{"d": "A visible indication."}]}]}
    job.complete(card, False)
    final = job.state
    assert final["definition_tr"] == "Переклад визначення."
    assert final["def_tr_pending"] is False
    assert final["context"] == "A wider captured sentence."
    assert len(cached) == 1
    assert cached[0][1]["definition_tr"] == final["definition_tr"]
    assert "context" not in cached[0][1]
