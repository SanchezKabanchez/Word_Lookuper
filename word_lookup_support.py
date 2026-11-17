"""Redacted application support report and log actions."""
import os
import re
import sys
import json
from datetime import datetime
from word_lookup_credentials import safe_settings
from word_lookup_version import __version__

_SECRET_RE = re.compile(r"(DeepL-Auth-Key\s+)\S+|(['\"]?key['\"]?\s*[:=]\s*['\"])[^'\"]+", re.I)

class Support:
    def __init__(self, settings, logger):
        self.settings, self.logger = settings, logger


    def log_path(self) -> str:
        return os.path.join(os.path.dirname(self.settings.path()), "app.log")

    def _mask_secrets(self, text: str) -> str:
        config = self.settings.data
        keys = [config.get(name, {}).get("key", "") for name in ("deepl", "googlecloud", "libretranslate")]
        credentials = self.settings.credentials
        if credentials is not None:
            keys += list(credentials.known_secrets)
        for key in keys:
            if key:
                text = text.replace(key, "***")
        return _SECRET_RE.sub(lambda m: (m.group(1) or m.group(2)) + "***", text)

    def read_log_tail(self, max_lines: int = 250) -> list:
        lines = []
        for path in (self.log_path() + ".1", self.log_path()):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    lines += f.read().splitlines()
            except FileNotFoundError:
                continue  # no rotated log on a fresh installation
            except OSError:
                self.logger.exception('word_lookup.read_log_tail')
        return lines[-max_lines:]

    def clear_log(self):
        self.logger.clear()

    def diagnostic_report(self, max_lines: int = 250) -> str:
        """Текст для вставки в чат: система, налаштування (ключ DeepL приховано), останні події."""
        safe = safe_settings(self.settings.data, "***")
        head = [f"Word Lookup — звіт {datetime.now():%Y-%m-%d %H:%M:%S}",
                f"Версія: {__version__}; система: {sys.platform}, Python {sys.version.split()[0]}",
                "Налаштування: " + json.dumps(safe, ensure_ascii=False)]
        tail = self.read_log_tail(max_lines)
        return "\n".join(head + ["", f"Останні події журналу ({len(tail)} рядків):"] + tail)
