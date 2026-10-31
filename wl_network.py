"""Shared request pacing, bounded translation cache and conservative offline OCR fixes.

No provider or UI imports: the desktop app supplies its configured HTTP session,
cache location, cancellation predicate, and translation function.
"""
from collections import OrderedDict
from concurrent.futures import Future, TimeoutError
from contextlib import contextmanager
import functools
import inspect
import json
import os
from pathlib import Path
import tempfile
import threading
import time


class LookupCancelled(RuntimeError):
    """A superseded lookup must not send another remote request."""


_context = threading.local()


def check_cancelled():
    predicate = getattr(_context, "cancelled", None)
    if predicate is not None and predicate():
        raise LookupCancelled("Пошук скасовано")


@contextmanager
def network_scope(cancelled):
    previous = getattr(_context, "cancelled", None)
    _context.cancelled = cancelled
    try:
        yield
    finally:
        _context.cancelled = previous


def scoped_task(fn, *args, **kwargs):
    """Copy cancellation into provider executor threads when scheduling a task."""
    cancelled = getattr(_context, "cancelled", None)
    def run():
        with network_scope(cancelled):
            check_cancelled()
            return fn(*args, **kwargs)
    return run


def network_job(cancelled):
    """Bind a UI worker to the token that was current when it was created."""
    def decorate(fn):
        @functools.wraps(fn)
        def run(*args, **kwargs):
            with network_scope(cancelled):
                try:
                    check_cancelled()
                    return fn(*args, **kwargs)
                except LookupCancelled:
                    return None  # UI token guards already discard obsolete results.
        return run
    return decorate


def cancellable_lookup(fn):
    """Scope App worker methods by their explicit token parameter."""
    signature = inspect.signature(fn)
    @functools.wraps(fn)
    def run(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        token = bound.arguments.get("token")
        if token is None:
            token = self._new_token()
            bound.arguments["token"] = token
        return network_job(lambda: token != self.active_token)(fn)(*bound.args, **bound.kwargs)
    return run


class RequestGate:
    """One shared budget for all translation/dictionary providers and fallbacks.

    Waiting tasks reserve no future slots, so abandoned searches cannot hold up
    the next lookup. In-flight requests remain bounded by their HTTP timeouts.
    """
    def __init__(self, interval=0.4, *, clock=time.monotonic, sleep=time.sleep):
        self.interval = interval
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        while True:
            check_cancelled()
            with self._lock:
                now = self._clock()
                delay = self._next - now
                if delay <= 0:
                    self._next = now + self.interval
                    return
            self._sleep(min(delay, 0.05))


REQUEST_GATE = RequestGate()


class TranslationCache:
    """Thread-safe LRU with one computation per key, and optional atomic storage.

    Errors are never cached. Each key includes language and provider mode.
    Sentence/definition text stays on the user's machine, alongside word cache.
    """
    def __init__(self):
        self._lock = threading.RLock()
        self._values = OrderedDict()
        self._inflight = {}
        self._path = None

    def _load(self, path):
        if path == self._path:
            return
        self._values.clear()
        self._path = path
        if path:
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._values.update((k, v) for k, v in list(data.items())[-1000:]
                                        if isinstance(k, str) and isinstance(v, str))
            except (OSError, ValueError):
                # An absent/old cache is recoverable; translations are recomputed.
                return

    def _save(self):
        if not self._path:
            return
        parent = os.path.dirname(self._path)
        os.makedirs(parent, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".translation-cache-", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self._values, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def clear(self, path=None):
        with self._lock:
            self._load(path)
            self._values.clear()
            if path:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    return

    def translate(self, key, compute, *, limit=1000, path=None, on_error=None):
        check_cancelled()
        if limit <= 0:
            return compute()
        with self._lock:
            self._load(path)
            if key in self._values:
                self._values.move_to_end(key)
                return self._values[key]
            flight_key = (path, key)
            pending = self._inflight.get(flight_key)
            owner = pending is None
            if owner:
                pending = self._inflight[flight_key] = Future()
        if not owner:
            while True:
                check_cancelled()
                try:
                    return pending.result(timeout=0.05)
                except TimeoutError:
                    continue
                except LookupCancelled:
                    # The older owner may have been cancelled while this lookup
                    # remains current. Retry under this caller's own token.
                    check_cancelled()
                    return self.translate(key, compute, limit=limit, path=path, on_error=on_error)
        try:
            result = compute()
            check_cancelled()
            if result:
                with self._lock:
                    self._load(path)
                    self._values[key] = result
                    self._values.move_to_end(key)
                    while len(self._values) > min(limit, 1000):
                        self._values.popitem(last=False)
                    try:
                        self._save()
                    except OSError as error:
                        if on_error:
                            on_error(error)
            pending.set_result(result)
            return result
        except BaseException as error:
            with self._lock:
                if self._inflight.get(flight_key) is pending:
                    self._inflight.pop(flight_key)
            pending.set_exception(error)
            raise
        finally:
            with self._lock:
                if self._inflight.get(flight_key) is pending:
                    self._inflight.pop(flight_key)


TEXT_CACHE = TranslationCache()

# Explicit common HUD/font confusions. Never fuzzy-correct arbitrary names or
# legitimate short words (e.g. "form", "rn", "well") to an unrelated dictionary word.
OCR_CORRECTIONS = {
    "changin": "changing", "goin": "going", "doin": "doing", "lookin": "looking",
    "talkin": "talking", "nothin": "nothing", "somethin": "something",
    "everythin": "everything", "anythin": "anything", "gettin": "getting",
    "runnin": "running", "walkin": "walking", "waitin": "waiting",
    "ccpyright": "copyright", "diskibution": "distribution", "footlng": "footing",
    "changlng": "changing", "somethlng": "something", "nothlng": "nothing",
    "everythlng": "everything", "looklng": "looking",
}


def correct_ocr_word(word):
    """Deterministic local correction; manual entry can opt out by bypassing it."""
    fixed = OCR_CORRECTIONS.get(word.lower())
    if not fixed:
        return word
    if word.isupper():
        return fixed.upper()
    return fixed.capitalize() if word[:1].isupper() else fixed
