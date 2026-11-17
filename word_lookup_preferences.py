"""Explicit application settings and protected credential preferences."""
import hashlib
import os
from word_lookup_config import DEFAULT_CONFIG, atomic_json_write, load_settings
from word_lookup_credentials import CredentialPreferences, safe_settings


class Settings:
    def __init__(self, data, path, logger, *, writer=atomic_json_write, credentials=None):
        self.data, self._path, self.logger, self.writer = data, path, logger, writer
        self.defaults = DEFAULT_CONFIG
        self.credentials = credentials or CredentialPreferences(lambda: os.path.abspath(self.path()), logger.write)
        self.credential_status = ""
        self.credential_state = {}
        self.file_state = {}
        self.notice = ""

    def path(self):
        return self._path()

    def load_config(self) -> dict:
        loaded = load_settings(self.path(), self.defaults)
        cfg = loaded.values
        self.file_state.clear()
        self.file_state.update(path=os.path.abspath(self.path()), writable=loaded.writable)
        self.notice = loaded.notice
        if loaded.notice:
            self.logger.write(loaded.notice)
        try:
            from word_lookup_secrets import load_key, storage_message
            secure_key = load_key()
            key_status = storage_message()
            if secure_key:
                cfg["deepl"]["key"] = secure_key
        except Exception:
            # Backend messages can include credentials. Report only a safe diagnosis.
            self.logger.write("Захищене сховище недоступне; ключ не прочитано.")
            secure_key = ""
            key_status = "Не вдалося прочитати збережений ключ. Введи його й натисни «Зберегти ключ»."
        # Seed the session baseline from protected storage. An empty key is not an implicit
        # request to delete an unreadable/missing entry. Only a legacy plaintext key
        # without a vault value needs one migration attempt at startup.
        key = cfg.get("deepl", {}).get("key", "").strip()
        self.credential_state.clear()
        self.credential_state.update(path=os.path.abspath(self.path()),
            digest=self._key_digest(key) if secure_key or not key else None,
            secure=bool(secure_key), status=key_status)
        self.credential_status = key_status
        self.credentials.remember(key)
        self.credentials.load(cfg)
        return cfg


    def save_config(self, cfg: dict, *, retry_key=False, retry_provider=None) -> bool:
        """Persist settings; keep the API key in Windows-protected storage only."""
        from word_lookup_secrets import store_key, storage_message
        key = cfg.get("deepl", {}).get("key", "").strip()
        self.credentials.remember(key)
        path = os.path.abspath(self.path())
        if self.credential_state.get("path") != path:
            self.credential_state.clear()
            self.credential_state.update(path=path, digest=self._key_digest(""), secure=False, status="")
        digest = self._key_digest(key)
        if retry_key or digest != self.credential_state.get("digest"):
            secure = False
            try:
                secure = store_key(key)
            except Exception:
                # Backend exceptions can echo credentials; never print their text.
                secure = False
            if key:
                status = ((storage_message() or "Ключ збережено у захищеному сховищі Windows.") if secure else
                          "Ключ працює лише в цьому сеансі: захищене збереження недоступне.")
            else:
                status = ((storage_message() or "Ключ видалено зі сховища.") if secure else
                          "Не вдалося видалити ключ зі сховища. Попередній ключ може повернутися після перезапуску.")
            # A failed attempt is also consumed: ordinary preference saves must not
            # retry the OS operation. Explicit retry or a changed key may try again.
            self.credential_state.update(digest=digest, secure=secure, status=status)
            if not secure:
                self.logger.write(status)
        self.credential_status = self.credential_state.get("status", "")
        if key and self.credential_state.get("secure") and not self.credential_status:
            self.credential_status = "Ключ збережено у захищеному сховищі Windows."
        if (self.file_state.get("path") == path and not self.file_state.get("writable", True)):
            self.logger.write("Конфіг не записано: попередній файл недоступний або має новішу схему.")
            return False
        self.credentials.save(cfg, retry_provider=retry_provider)
        safe = safe_settings(cfg)
        try:
            self.writer(self.path(), safe)
        except (OSError, ValueError):
            self.logger.exception("Збереження налаштувань")
            return False
        return True


    def _key_digest(self, key):
        """Compare session values without keeping another plaintext copy of a key."""
        return hashlib.sha256(key.encode("utf-8")).hexdigest()


    def get(self, path):
        value = self.data
        for key in path:
            value = value[key]
        return value

    def put(self, path, value):
        parent = self.data
        for key in path[:-1]:
            parent = parent[key]
        parent[path[-1]] = value
