"""Credential persistence isolated from ordinary preference saves."""
from collections import deque
import copy
import hashlib

from word_lookup_secrets import provider_key_store

KEY_SECTIONS = ("deepl", "googlecloud", "libretranslate")


def safe_settings(config: dict, replacement: str = "") -> dict:
    result = copy.deepcopy(config)
    for section in KEY_SECTIONS:
        if result.get(section, {}).get("key"):
            result[section]["key"] = replacement
    return result


class CredentialPreferences:
    def __init__(self, profile_path, log=lambda message: None, store_factory=None):
        self.profile_path, self.log = profile_path, log
        self.store_factory = store_factory or provider_key_store
        self.state = {}
        self.known_secrets = deque(maxlen=16)

    def remember(self, key: str) -> None:
        if key and key not in self.known_secrets:
            self.known_secrets.append(key)

    def _baseline(self, provider):
        path = self.profile_path()
        state = self.state.get(provider)
        if state is None or state["path"] != path:
            state = self.state[provider] = {"path": path, "digest": self._digest(""), "message": ""}
        return state

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def load(self, config: dict) -> None:
        for provider in KEY_SECTIONS[1:]:
            store, secure = self.store_factory(provider), ""
            try:
                secure = store.load()
            except Exception:
                self.log(f"Не вдалося прочитати захищений ключ {provider}.")
            if secure:
                config[provider]["key"] = secure
            key = config[provider]["key"].strip()
            self.remember(key)
            state = self._baseline(provider)
            state.update(digest=self._digest(key) if secure or not key else None,
                         message=store.message if secure else "")

    def save(self, config: dict, *, retry_provider: str | None = None) -> None:
        for provider in KEY_SECTIONS[1:]:
            state = self._baseline(provider)
            key = config[provider]["key"].strip()
            self.remember(key)
            digest = self._digest(key)
            if digest == state["digest"] and retry_provider != provider:
                continue
            store = self.store_factory(provider)
            try:
                secure = store.store(key)
            except Exception:
                secure = False
            message = store.message if secure else "Захищене сховище недоступне; ключ працює лише в цьому сеансі."
            state.update(digest=digest, message=message)
            if not secure:
                self.log(provider + ": " + message)

    def status(self, provider: str) -> str:
        return self._baseline(provider)["message"]
