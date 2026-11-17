"""Rotating, redacted application logs and GUI/thread exception hooks."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import threading
import traceback
from typing import Callable


class RedactionFilter(logging.Filter):
    def __init__(self, redact: Callable[[str], str]):
        super().__init__()
        self.redact = redact

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + "".join(traceback.format_exception(*record.exc_info))
        record.msg, record.args, record.exc_info, record.exc_text = self.redact(message), (), None, None
        return True


class AppLog:
    def __init__(self, path: Callable[[], str], redact: Callable[[str], str], max_bytes: int = 300_000):
        self.path, self.redact, self.max_bytes = path, redact, max_bytes
        self._lock = threading.RLock()
        self._handler = None
        self._path = None
        self._logger = logging.Logger("WordLookup", logging.DEBUG)

    def _ensure_handler(self) -> None:
        path = self.path()
        if path == self._path and self._handler is not None:
            return
        self._close_handler()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._handler = RotatingFileHandler(path, maxBytes=self.max_bytes, backupCount=1,
                                            encoding="utf-8", delay=True)
        self._handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        self._handler.addFilter(RedactionFilter(self.redact))
        self._logger.addHandler(self._handler)
        self._path = path

    def write(self, message: str, level: int = logging.INFO) -> None:
        message = self.redact(message)
        if len(message) > 65536:
            message = message[:65536] + "\n[запис скорочено]"
        if sys.stdout is not None:
            try:
                print("[WordLookup]", message, flush=True)
            except (OSError, ValueError):
                pass  # A detached/windowed executable may have no console.
        try:
            with self._lock:
                self._ensure_handler()
                self._logger.log(level, message)
        except (OSError, ValueError):
            if sys.stderr is not None:
                try:
                    print("[WordLookup] Не вдалося записати журнал.", file=sys.stderr)
                except (OSError, ValueError):
                    pass

    def _close_handler(self) -> None:
        if self._handler is not None:
            self._logger.removeHandler(self._handler)
            self._handler.close()
            self._handler = None

    def clear(self) -> None:
        with self._lock:
            self._close_handler()
            for path in (Path(self.path()), Path(self.path() + ".1")):
                path.unlink(missing_ok=True)

    def exception(self, where: str = "", info: tuple | None = None) -> None:
        info = sys.exc_info() if info is None else info
        self.write("ПОМИЛКА " + where + "\n" + "".join(traceback.format_exception(*info)).rstrip(), logging.ERROR)


class ForwardHandler(logging.Handler):
    def __init__(self, destination: AppLog):
        super().__init__()
        self.destination = destination

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        if record.exc_info:
            text += "\n" + "".join(traceback.format_exception(*record.exc_info))
        self.destination.write(text, record.levelno)


def install_exception_hooks(destination: AppLog) -> None:
    def system_hook(kind, value, tb):
        if issubclass(kind, KeyboardInterrupt):
            sys.__excepthook__(kind, value, tb)
        else:
            destination.exception("main", (kind, value, tb))

    def thread_hook(args):
        if args.exc_type is not SystemExit:
            name = args.thread.name if args.thread is not None else "unknown"
            destination.exception("thread " + name, (args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook, threading.excepthook = system_hook, thread_hook
    for name in ("word_lookup_pause", "word_lookup_platform", "word_lookup_input", "word_lookup_ui", "word_lookup_selection"):
        app_logger = logging.getLogger(name)
        for handler in list(app_logger.handlers):
            if isinstance(handler, ForwardHandler):
                app_logger.removeHandler(handler)
        app_logger.addHandler(ForwardHandler(destination))
        app_logger.setLevel(logging.INFO)
