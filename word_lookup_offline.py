"""Optional, lazy Argos-format translation: one model pair, bounded CPU, no downloads."""
import gc
import importlib.util
import os
from pathlib import Path
import threading

from word_lookup_paths import data_dir


class OfflineUnavailable(RuntimeError):
    pass


class OfflineTranslator:
    def __init__(self, settings, cancelled=lambda: None, loader=None):
        self.settings = settings
        self.check_cancelled = cancelled
        self._loader = loader or self._load_argos
        self._lock = threading.Lock()
        self._pair = self._translation = self._module = None

    @staticmethod
    def available() -> bool:
        return all(importlib.util.find_spec(name) is not None for name in ("ctranslate2", "sentencepiece"))

    def _load_argos(self):
        if not self.available():
            raise OfflineUnavailable("Встанови офлайн-додаток через install_offline.bat.")
        from word_lookup_local_models import ModelCatalog
        threads = min(4, max(1, int(self.settings()["offline_translation"]["cpu_threads"])))
        os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
        return ModelCatalog(data_dir() / "models" / "argos", threads)

    def translate(self, text: str, source: str, target: str) -> str:
        if not self.settings()["offline_translation"]["enabled"]:
            raise OfflineUnavailable("Офлайн-перекладач вимкнено.")
        if len(text) > 5000:
            raise ValueError("Завеликий текст для локального перекладу.")
        source = "en" if source == "auto" else source
        if source == target:
            return text
        while not self._lock.acquire(timeout=.1):
            self.check_cancelled()
        try:
            self.check_cancelled()
            if self._module is None:
                self._module = self._loader()
            pair = source, target
            if self._pair != pair:
                self._translation = None
                self._module.get_installed_languages.cache_clear()
                gc.collect()
                languages = {language.code: language for language in self._module.get_installed_languages()}
                if source not in languages or target not in languages:
                    raise OfflineUnavailable(f"Потрібна встановлена модель {source} → {target}.")
                self._translation = languages[source].get_translation(languages[target])
                if self._translation is None:
                    raise OfflineUnavailable(f"Немає локального маршруту {source} → {target}.")
                self._pair = pair
            result = self._translation.translate(text)
            self.check_cancelled()
            return result
        finally:
            self._lock.release()

    def install_model(self, filename: str) -> None:
        with self._lock:
            if self._module is None:
                self._module = self._loader()
            self._module.install(Path(filename))
            self._pair, self._translation = None, None
