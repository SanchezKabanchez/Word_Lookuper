import copy
import queue

import pytest
import word_lookup as w


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "config_path", lambda: str(tmp_path / "config.json"))
    monkeypatch.setattr(w, "cfg", copy.deepcopy(w.DEFAULT_CONFIG))
    monkeypatch.setattr(w, "_saved_words", None)
    monkeypatch.setattr(w, "_card_cache", {})
    monkeypatch.setattr(w, "_cache_new", 0)
    monkeypatch.setattr(w, "events", queue.Queue())
    # Tests must not accidentally depend on an external translation service.
    def no_network(*args, **kwargs):
        raise AssertionError("Unexpected network request in offline tests")
    monkeypatch.setattr(w.SESSION, "get", no_network)
    monkeypatch.setattr(w.requests, "get", no_network)


@pytest.fixture
def app():
    instance = w.App()
    instance.root.update()
    yield instance
    instance.quit()


@pytest.fixture
def card():
    result = copy.deepcopy(w.SAMPLE_CARD)
    result.update(context="That is an understatement.", sentence="That is an understatement.",
                  source="Example game", sentence_tr="Це применшення.")
    return result
