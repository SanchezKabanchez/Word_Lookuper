"""Official translation APIs with explicit settings and HTTP dependencies."""
from html import unescape
from urllib.parse import urlsplit


class ProviderRateLimited(Exception):
    pass


def libre_endpoint(base: str) -> str:
    parsed = urlsplit(base.strip())
    try:
        valid_port = parsed.port is None or 1 <= parsed.port <= 65535
    except ValueError:
        valid_port = False
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (not parsed.hostname or not valid_port or parsed.username or parsed.password or parsed.query
            or parsed.fragment or parsed.scheme not in {"https", "http"}
            or (parsed.scheme == "http" and not local)):
        raise ValueError("LibreTranslate потребує HTTPS або локального HTTP-сервера.")
    return base.strip().rstrip("/") + "/translate"


class OfficialTranslators:
    def __init__(self, settings, post, target_language):
        self.settings, self.post, self.target_language = settings, post, target_language

    def google_cloud(self, text: str, source: str, want_dictionary: bool = False):
        key = self.settings()["googlecloud"]["key"].strip()
        if not key:
            return "", []
        payload = {"q": text, "target": self.target_language(), "format": "text"}
        if source and source != "auto":
            payload["source"] = source
        response = self.post("https://translation.googleapis.com/language/translate/v2",
            json=payload, headers={"X-Goog-Api-Key": key}, timeout=8)
        return self._result(response, cloud=True), []

    def libre_translate(self, text: str, source: str, want_dictionary: bool = False):
        settings = self.settings()["libretranslate"]
        payload = {"q": text, "source": source or "auto", "target": self.target_language(), "format": "text"}
        if settings["key"].strip():
            payload["api_key"] = settings["key"].strip()
        response = self.post(libre_endpoint(settings["url"]), json=payload, timeout=8)
        return self._result(response), []

    @staticmethod
    def _result(response, *, cloud=False) -> str:
        if response.status_code == 429:
            raise ProviderRateLimited("Ліміт офіційного API перекладу.")
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Неправильний формат відповіді API.")
        if cloud:
            content = data.get("data")
            translations = content.get("translations", []) if isinstance(content, dict) else []
            if not isinstance(translations, list) or not translations:
                raise ValueError("Google Cloud не повернув переклад.")
            text = translations[0].get("translatedText") if isinstance(translations[0], dict) else None
        else:
            text = data.get("translatedText")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("API не повернув переклад.")
        return unescape(text.strip())
