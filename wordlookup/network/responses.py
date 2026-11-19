"""Bound HTTP response bodies; imported only when a provider is used."""
import requests
from wordlookup.network.policy import MAX_RESPONSE_BYTES, AUDIO_TYPES


class ResponseTooLarge(requests.RequestException):
    """A provider exceeded the bounded response budget."""


def bounded_response(response, *, audio=False, limit=MAX_RESPONSE_BYTES):
    headers = getattr(response, "headers", {})
    content_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    try:
        length = int(headers.get("Content-Length", "0"))
        if length < 0 or length > limit:
            raise ResponseTooLarge("Відповідь джерела перевищує 2 МБ.")
        if audio and content_type not in AUDIO_TYPES:
            raise requests.RequestException("Джерело повернуло невідомий формат аудіо.")
        if not hasattr(response, "iter_content"):
            return response
        content = bytearray()
        for chunk in response.iter_content(chunk_size=32768):
            if len(content) + len(chunk) > limit:
                raise ResponseTooLarge("Відповідь джерела перевищує 2 МБ.")
            content.extend(chunk)
        response._content = bytes(content)
        response._content_consumed = True
        return response
    finally:
        if hasattr(response, "close"):
            response.close()
