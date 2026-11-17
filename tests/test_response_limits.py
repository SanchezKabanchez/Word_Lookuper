from types import SimpleNamespace
import threading
import time

import pytest
import requests

from word_lookup_network import LockedSession, ResponseTooLarge, bounded_response


def response(chunks, **headers):
    result = requests.Response()
    result.status_code = 200
    result.headers.update(headers)
    result.raw = SimpleNamespace(close=lambda: None, release_conn=lambda: None)
    result.iter_content = lambda chunk_size: iter(chunks)
    return result


def test_oversize_stream_is_stopped_without_materializing_full_body():
    reads = []
    result = response([])
    def chunks(chunk_size):
        for index in range(100):
            reads.append(index)
            yield b"x" * 32
    result.iter_content = chunks
    with pytest.raises(ResponseTooLarge):
        bounded_response(result, limit=64)
    assert reads == [0, 1, 2]


def test_announced_oversize_is_rejected_before_reading():
    result = response([], **{"Content-Length": "99999999"})
    result.iter_content = lambda chunk_size: pytest.fail("Oversize response read a chunk")
    with pytest.raises(ResponseTooLarge):
        bounded_response(result)


def test_json_remains_available_after_bounded_stream_is_closed():
    result = response([b'{"transla', b'tion":"ok"}'], **{"Content-Type": "application/json"})
    assert bounded_response(result).json() == {"translation": "ok"}


def test_audio_requires_audio_mime_type():
    result = response([b"not audio"], **{"Content-Type": "text/html"})
    with pytest.raises(requests.RequestException, match="аудіо"):
        bounded_response(result, audio=True)
    valid = response([b"synthetic mp3"], **{"Content-Type": "audio/mpeg"})
    assert bounded_response(valid, audio=True).content == b"synthetic mp3"


def test_shared_session_serializes_mutable_request_state(monkeypatch):
    session = LockedSession()
    active, maximum = 0, 0
    gate = threading.Lock()
    def request(_self, *args, **kwargs):
        nonlocal active, maximum
        with gate:
            active += 1
            maximum = max(maximum, active)
        time.sleep(.02)
        with gate:
            active -= 1
        return object()
    monkeypatch.setattr(requests.Session, "request", request)
    workers = [threading.Thread(target=lambda: session.get("https://example.invalid")) for _ in range(3)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(2)
    assert maximum == 1
