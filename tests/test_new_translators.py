import copy
from functools import lru_cache
import json
from types import SimpleNamespace

import pytest

import word_lookup as w
from word_lookup_credentials import CredentialPreferences, safe_settings
from word_lookup_network import NetworkDisabled, provider_for_url
from word_lookup_offline import OfflineTranslator, OfflineUnavailable
from word_lookup_translators import OfficialTranslators, libre_endpoint


def result(data, status=200):
    return SimpleNamespace(status_code=status, json=lambda: data, raise_for_status=lambda: None)


def test_google_cloud_uses_official_endpoint_and_header_key():
    config = copy.deepcopy(w.DEFAULT_CONFIG)
    config["googlecloud"]["key"] = "synthetic-cloud-key"
    calls = []
    provider = OfficialTranslators(lambda: config, lambda url, **kw:
        (calls.append((url, kw)), result({"data": {"translations": [{"translatedText": "Привіт &amp; світ"}]}}))[1], lambda: "uk")
    assert provider.google_cloud("Hello world", "en") == ("Привіт & світ", [])
    url, request = calls[0]
    assert url == "https://translation.googleapis.com/language/translate/v2"
    assert request["headers"] == {"X-Goog-Api-Key": "synthetic-cloud-key"}
    assert request["json"] == {"q": "Hello world", "source": "en", "target": "uk", "format": "text"}
    assert "synthetic-cloud-key" not in url


def test_libretranslate_uses_selected_endpoint_and_optional_key():
    config = copy.deepcopy(w.DEFAULT_CONFIG)
    config["libretranslate"].update(url="http://127.0.0.1:5000", key="synthetic-libre-key")
    calls = []
    provider = OfficialTranslators(lambda: config, lambda url, **kw:
        (calls.append((url, kw)), result({"translatedText": "Привіт"}))[1], lambda: "uk")
    assert provider.libre_translate("Hello", "en") == ("Привіт", [])
    assert calls[0][0] == "http://127.0.0.1:5000/translate"
    assert calls[0][1]["json"]["api_key"] == "synthetic-libre-key"
    assert provider_for_url(calls[0][0], config=config) == "libretranslate"
    assert provider_for_url("http://127.0.0.1:5001/translate", config=config) is None


@pytest.mark.parametrize("url", ["http://untrusted.example", "https://user:key@example.com", "file:///etc/passwd",
                                  "https://example.com?key=secret", "https://example.com/#fragment"])
def test_libre_server_address_rejects_credentials_and_unencrypted_remote_hosts(url):
    with pytest.raises(ValueError):
        libre_endpoint(url)


def test_official_providers_obey_privacy_switches_before_http(monkeypatch):
    w.cfg["googlecloud"]["key"] = "synthetic-cloud-key"
    w.cfg["libretranslate"]["url"] = "http://127.0.0.1:5000"
    with pytest.raises(NetworkDisabled):
        w._official_translators.google_cloud("Hello", "en")
    with pytest.raises(NetworkDisabled):
        w._official_translators.libre_translate("Hello", "en")
    w.cfg["network"]["providers"]["googlecloud"] = True
    monkeypatch.setattr(w.REQUEST_GATE, "wait", lambda: None)
    monkeypatch.setattr(w.SESSION, "post", lambda *args, **kwargs: result(
        {"data": {"translations": [{"translatedText": "Привіт"}]}}))
    assert w._official_translators.google_cloud("Hello", "en")[0] == "Привіт"


@pytest.mark.parametrize("data", [None, [], {"data": None}, {"data": {"translations": []}}])
def test_changed_cloud_response_contract_is_reported(data):
    with pytest.raises(ValueError):
        OfficialTranslators._result(result(data), cloud=True)


def test_local_translation_works_offline_without_http_and_keeps_provider_badge(monkeypatch):
    w.cfg["network"]["offline"] = True
    w.cfg["offline_translation"]["enabled"] = True
    monkeypatch.setattr(w._offline_translator, "translate", lambda text, source, target: "локальний переклад")
    assert w.translate_text("Synthetic offline sentence.") == "локальний переклад"
    assert w.text_card("original", "локальний переклад")["translator"] == "Argos (локально)"
    card = w.build_word_card("synthetic")
    assert card["translator"] == "Argos (локально)"
    assert card["translation"] == "локальний переклад"


def test_argos_is_lazy_and_reuses_only_current_model_route():
    config = copy.deepcopy(w.DEFAULT_CONFIG)
    calls = []
    class Language:
        def __init__(self, code):
            self.code = code
        def get_translation(self, target):
            return SimpleNamespace(translate=lambda text: self.code + ":" + target.code + ":" + text)
    @lru_cache()
    def installed():
        calls.append("languages")
        return [Language("en"), Language("uk"), Language("pl")]
    def load():
        calls.append("load")
        return SimpleNamespace(get_installed_languages=installed)
    engine = OfflineTranslator(lambda: config, loader=load)
    assert calls == []
    with pytest.raises(OfflineUnavailable):
        engine.translate("Hello", "en", "uk")
    config["offline_translation"]["enabled"] = True
    assert engine.translate("Hello", "auto", "uk") == "en:uk:Hello"
    assert engine.translate("Again", "en", "uk") == "en:uk:Again"
    assert calls == ["load", "languages"]
    assert engine.translate("Hello", "en", "pl") == "en:pl:Hello"
    assert calls == ["load", "languages", "languages"]


def test_extra_keys_are_saved_only_after_change_and_never_in_json():
    config = copy.deepcopy(w.DEFAULT_CONFIG)
    calls, values = [], {}
    class Store:
        message = "stored"
        def __init__(self, name):
            self.name = name
        def load(self):
            calls.append(("load", self.name))
            return values.get(self.name, "")
        def store(self, key):
            calls.append(("save", self.name))
            values[self.name] = key
            return True
    manager = CredentialPreferences(lambda: "isolated-profile", store_factory=Store)
    manager.load(config)
    calls.clear()
    manager.save(config)
    assert calls == []
    config["googlecloud"]["key"] = "synthetic-cloud-key"
    manager.save(config)
    manager.save(config)
    assert calls == [("save", "googlecloud")]
    assert values["googlecloud"] == "synthetic-cloud-key"
    assert "synthetic-cloud-key" not in json.dumps(safe_settings(config))


def test_provider_badge_and_key_controls_exist_in_real_ui(app, monkeypatch):
    card = dict(w.SAMPLE_CARD, translator="Google Cloud")
    app.show_lookup(card, None)
    app.root.update()
    assert set(app.translator_settings.status) == {"googlecloud", "libretranslate", "argos"}
    texts = []
    def visit(widget):
        if widget.winfo_class() == "Label":
            texts.append(widget.cget("text"))
        for child in widget.winfo_children():
            visit(child)
    visit(app.popup.content)
    assert "Перекладач: Google Cloud" in texts
