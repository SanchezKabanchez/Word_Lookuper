import copy
import gc
import queue

import pytest
import word_lookup as w
import word_lookup_secrets as secrets


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    class EmptyProviderKeyStore:
        message = ""
        def load(self):
            return ""
        def store(self, _key):
            return False
    monkeypatch.setattr(w, "config_path", lambda: str(tmp_path / "config.json"))
    monkeypatch.setattr(secrets, "_key_path", lambda: tmp_path / "deepl-key.dpapi")
    # Fault-injection tests of the vault must not touch real Windows DPAPI.
    # DPAPI-specific tests explicitly enable their own isolated adapter.
    monkeypatch.setattr(secrets, "_dpapi_available", lambda: False)
    monkeypatch.setattr(secrets, "_backend", lambda: None)
    monkeypatch.setattr(secrets, "_message", "")
    monkeypatch.setattr(w, "_credential_state", {})
    monkeypatch.setattr(w, "_credential_status", "")
    monkeypatch.setattr(w, "_config_file_state", {})
    monkeypatch.setattr(w, "_configuration_notice", "")
    monkeypatch.setattr(w._extra_credentials, "state", {})
    monkeypatch.setattr(w._extra_credentials, "store_factory", lambda _provider: EmptyProviderKeyStore())
    monkeypatch.setattr(w, "cfg", copy.deepcopy(w.DEFAULT_CONFIG))
    monkeypatch.setattr(w, "_saved_words", None)
    monkeypatch.setattr(w, "_saved_words_state", {})
    monkeypatch.setattr(w, "_card_cache", {})
    monkeypatch.setattr(w, "_cache_file_state", {})
    monkeypatch.setattr(w, "_cache_new", 0)
    monkeypatch.setattr(w, "events", queue.Queue())
    monkeypatch.setattr(w, "_last_game_detection", {})
    # Tests must not accidentally depend on an external translation service.
    def no_network(*args, **kwargs):
        raise AssertionError("Unexpected network request in offline tests")
    monkeypatch.setattr(w.SESSION, "get", no_network)
    monkeypatch.setattr(w.requests, "get", no_network)


@pytest.fixture
def app():
    # Tk/pynput own cycles; release prior test controllers before X11 client limit.
    gc.collect()
    instance = w.App()
    instance.root.update()
    yield instance
    instance.quit()
    gc.collect()


@pytest.fixture
def card():
    result = copy.deepcopy(w.SAMPLE_CARD)
    result.update(context="That is an understatement.", sentence="That is an understatement.",
                  source="Example game", sentence_tr="Це применшення.")
    return result
