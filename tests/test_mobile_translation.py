"""Mobile fallback stays bounded and extracts only the translated result."""

import pytest
import word_lookup as w


class MobileResponse:
    def __init__(self, body, status=200):
        self.text = body
        self.status_code = status
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise w.requests.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


@pytest.mark.parametrize("body,expected", [
    ('<div class="result-container">Привіт &amp; <span>світе</span>!</div>', 'Привіт & світе!'),
    ('<div class="extra t0">Перший<br>Другий<br/>Третій &#33;</div>', 'Перший\nДругий\nТретій !'),
    ('<div>Navigation</div><div class="t0">Переклад</div><div>Footer</div>', 'Переклад'),
    ('<div class="t0">  </div><div class="result-container">Результат</div>', 'Результат'),
])
def test_mobile_extracts_nested_translation_with_bounded_request(monkeypatch, body, expected):
    response = MobileResponse(body)
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(w.SESSION, "get", get)
    assert w._p_deep("  hello  ", "en", True) == (expected, [])
    assert calls == [("https://translate.google.com/m", {
        "params": {"sl": "en", "tl": "uk", "q": "hello"}, "timeout": 8})]
    assert response.closed


@pytest.mark.parametrize("status,body,error", [
    (429, "", w.RateLimited),
    (503, "Unavailable", w.requests.HTTPError),
    (200, '<div class="navigation">No translated result</div>', ValueError),
    (200, '<div class="result-container">   </div>', ValueError),
])
def test_mobile_closes_response_on_http_or_empty_result(monkeypatch, status, body, error):
    response = MobileResponse(body, status)
    monkeypatch.setattr(w.SESSION, "get", lambda *args, **kwargs: response)
    with pytest.raises(error):
        w._p_deep("hello", "en", False)
    assert response.closed


def test_mobile_network_timeout_is_propagated(monkeypatch):
    def timeout(url, **kwargs):
        assert kwargs["timeout"] == 8
        raise w.requests.Timeout("read timed out")

    monkeypatch.setattr(w.SESSION, "get", timeout)
    with pytest.raises(w.requests.Timeout):
        w._p_deep("hello", "en", False)


def test_mobile_empty_same_language_and_oversize_inputs_skip_network():
    # The common fixture rejects unexpected network calls.
    assert w._p_deep("   ", "en", False) == ("", [])
    assert w._p_deep("привіт", "uk", False) == ("привіт", [])
    with pytest.raises(ValueError):
        w._p_deep("a" * 5001, "en", False)
