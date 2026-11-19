"""Provider permissions and HTTP redirects for external lookup services.

The app passes live configuration before every HTTP hop, including after budget
waits. Keeping the guard independent of the UI makes the privacy boundary easy to
exercise without contacting a service.
"""
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from wordlookup.network.official import libre_endpoint

import threading

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
AUDIO_TYPES = {"audio/mpeg", "audio/mp3", "audio/ogg", "application/ogg", "audio/wav",
               "audio/x-wav", "audio/mp4", "audio/aac", "audio/flac", "audio/webm"}


def __getattr__(name):
    if name == "ResponseTooLarge":
        from wordlookup.network.responses import ResponseTooLarge
        return ResponseTooLarge
    raise AttributeError(name)


class LockedSession:
    """Create pooled HTTP state on first use, serializing concurrent callers.

    Opening settings or using the local library does not import requests/SSL.
    Headers supplied by the provider are installed once, before its first call.
    """
    def __init__(self, headers=None):
        self._request_lock = threading.RLock()
        self._initial_headers = dict(headers or {})
        self._session = None

    def _get_session(self):
        if self._session is None:
            import requests
            session = requests.Session()
            session.headers.update(self._initial_headers)
            self._session = session
        return self._session

    @property
    def headers(self):
        with self._request_lock:
            return self._get_session().headers

    @property
    def cookies(self):
        with self._request_lock:
            return self._get_session().cookies

    def request(self, *args, **kwargs):
        with self._request_lock:
            return self._get_session().request(*args, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def close(self):
        with self._request_lock:
            if self._session is not None:
                self._session.close()


def bounded_response(response, *, audio=False, limit=MAX_RESPONSE_BYTES):
    from wordlookup.network.responses import bounded_response as read_response
    return read_response(response, audio=audio, limit=limit)


PROVIDER_HOSTS = {
    "google": {"translate.googleapis.com", "translate.google.com", "clients5.google.com"},
    "mymemory": {"api.mymemory.translated.net"},
    "datamuse": {"api.datamuse.com"},
    "dictionaryapi": {"api.dictionaryapi.dev"},
    "deepl": {"api-free.deepl.com", "api.deepl.com"},
    "googlecloud": {"translation.googleapis.com"},
    "libretranslate": set(),
}
# Recordings returned by dictionaryapi.dev. They remain controlled by its switch.
AUDIO_HOSTS = {"api.dictionaryapi.dev", "upload.wikimedia.org", "ssl.gstatic.com"}


class NetworkDisabled(RuntimeError):
    """A lookup would send data to a service the user disabled."""


def network_allowed(config, provider):
    policy = config.get("network", {})
    return (provider in PROVIDER_HOSTS and not policy.get("offline", False)
            and bool(policy.get("providers", {}).get(provider, provider not in {"googlecloud", "libretranslate"})))


def provider_for_url(url, *, audio=False, config=None):
    parsed = urlsplit(url)
    if config is not None and not audio:
        try:
            endpoint = urlsplit(libre_endpoint(config.get("libretranslate", {}).get("url", "")))
            if (not parsed.username and not parsed.password and not parsed.fragment
                    and (parsed.scheme, parsed.hostname, parsed.port, parsed.path) ==
                    (endpoint.scheme, endpoint.hostname, endpoint.port, endpoint.path)):
                return "libretranslate"
        except ValueError:
            pass
    try:
        valid = (parsed.scheme == "https" and not parsed.username and not parsed.password
                 and parsed.port in (None, 443))
    except ValueError:
        valid = False
    if valid:
        host = (parsed.hostname or "").lower()
        if audio:
            return "dictionaryapi" if host in AUDIO_HOSTS else None
        return next((name for name, hosts in PROVIDER_HOSTS.items() if host in hosts), None)
    return None


def require_network(config, url, *, audio=False, expected=None):
    provider = provider_for_url(url, audio=audio, config=config)
    if not provider or (expected is not None and provider != expected):
        raise NetworkDisabled("Запит до невідомого джерела або перенаправлення між джерелами заблоковано.")
    if (audio and urlsplit(url).hostname == "ssl.gstatic.com"
            and not network_allowed(config, "google")):
        raise NetworkDisabled("Аудіо Google вимкнено разом із джерелом Google.")
    if not network_allowed(config, provider):
        if config.get("network", {}).get("offline", False):
            raise NetworkDisabled("Офлайн-режим: доступні збережені слова, кеш і локальний словник.")
        raise NetworkDisabled(f"Джерело {provider} вимкнено в налаштуваннях мережі.")
    return provider


def remote_request(config, session, gate, method, url, *, audio=False, **kwargs):
    """Never let requests follow an unvalidated redirect with lookup text or a key."""
    provider = require_network(config(), url, audio=audio)
    kwargs["allow_redirects"] = False
    kwargs["stream"] = True
    for _hop in range(6):
        gate.wait()
        require_network(config(), url, audio=audio, expected=provider)
        response = getattr(session, method)(url, **kwargs)
        status = getattr(response, "status_code", 200)
        location = getattr(response, "headers", {}).get("Location")
        if status not in {301, 302, 303, 307, 308} or not location:
            return bounded_response(response, audio=audio)
        destination = urljoin(url, location)
        try:
            require_network(config(), destination, audio=audio, expected=provider)
            if urlsplit(destination).netloc != urlsplit(url).netloc:
                # Authentication must never leave the host the key was sent to.
                headers = kwargs.get("headers", {})
                kwargs["headers"] = {k: v for k, v in headers.items()
                                     if k.lower() not in {"authorization", "cookie"}}
                if method != "get" and status in {307, 308}:
                    raise NetworkDisabled("Перенаправлення запиту з даними на інший сервер заблоковано.")
        finally:
            response.close()
        kwargs.pop("params", None)
        if status == 303 or (status in {301, 302} and method == "post"):
            method = "get"
            kwargs.pop("data", None)
            kwargs.pop("json", None)
        url = destination
    import requests
    raise requests.TooManyRedirects("Забагато перенаправлень джерела словника")


class _MobileTranslationParser(HTMLParser):
    """Extract only the translated-result element from Google's mobile page."""
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts = []
        self.results = []

    def handle_starttag(self, tag, attrs):
        if self.depth:
            if tag == "br":
                self.parts.append("\n")
            if tag not in self.VOID:
                self.depth += 1
        elif tag == "div" and set(dict(attrs).get("class", "").split()) & {"t0", "result-container"}:
            self.depth = 1
            self.parts = []

    def handle_endtag(self, tag):
        if self.depth and tag not in self.VOID:
            self.depth -= 1
            if not self.depth:
                self.results.append("".join(self.parts).strip())

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def mobile_translation(body):
    parser = _MobileTranslationParser()
    parser.feed(body)
    parser.close()
    return next((item for item in parser.results if item), "")
