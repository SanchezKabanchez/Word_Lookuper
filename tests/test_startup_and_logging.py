import logging
import os
from pathlib import Path
import subprocess
import sys
import threading

import word_lookup as w
from word_lookup_instance import SingleInstance
from word_lookup_logging import AppLog, install_exception_hooks


def test_second_launch_activates_primary_without_importing_gui(tmp_path):
    first = SingleInstance(tmp_path)
    assert first.acquire()
    try:
        child = subprocess.run([sys.executable, "-c",
            "import sys; import word_lookup_launcher as app; app.main(); "
            "assert 'tkinter' not in sys.modules; print('secondary-clean')"],
            env=dict(os.environ, WORD_LOOKUP_DATA_DIR=str(tmp_path)), capture_output=True, text=True, timeout=15)
        assert child.returncode == 0, child.stderr
        assert "secondary-clean" in child.stdout
        assert first.activation_requested()
        first._last_poll = 0
        assert not first.activation_requested()
    finally:
        first.close()
    replacement = SingleInstance(tmp_path)
    assert replacement.acquire()
    replacement.close()


def test_separate_profiles_can_run_independently(tmp_path):
    first, second = SingleInstance(tmp_path / "one"), SingleInstance(tmp_path / "two")
    try:
        assert first.acquire() and second.acquire()
    finally:
        first.close()
        second.close()


def test_standard_log_rotation_redacts_before_writing(tmp_path, capsys):
    path = tmp_path / "app.log"
    logger = AppLog(lambda: str(path), lambda text: text.replace("private-key", "***"), max_bytes=180)
    for index in range(8):
        logger.write(f"event {index}: private-key " + "x" * 60)
    assert path.exists() and Path(str(path) + ".1").exists()
    assert "private-key" not in path.read_text() + Path(str(path) + ".1").read_text()
    assert "private-key" not in capsys.readouterr().out
    logger.clear()
    assert not path.exists() and not Path(str(path) + ".1").exists()
    logger.write("after clear")
    assert "after clear" in path.read_text()


def test_tk_callback_exceptions_reach_redacted_log(app):
    w.cfg["deepl"]["key"] = "synthetic-callback-secret"
    def broken_callback():
        raise ValueError("synthetic-callback-secret in callback")
    app.root.after_idle(broken_callback)
    app.root.update()
    content = Path(w.log_path()).read_text()
    assert "Tk callback" in content and "ValueError" in content
    assert "synthetic-callback-secret" not in content


def test_thread_and_system_hooks_log_without_console(tmp_path, monkeypatch):
    path = tmp_path / "app.log"
    logger = AppLog(lambda: str(path), lambda text: text.replace("secret-value", "***"))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    names = ("word_lookup_pause", "word_lookup_platform", "word_lookup_input", "word_lookup_ui", "word_lookup_selection")
    previous = {name: (list(logging.getLogger(name).handlers), logging.getLogger(name).level) for name in names}
    try:
        install_exception_hooks(logger)
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)
        def broken_thread():
            raise RuntimeError("secret-value in worker")
        worker = threading.Thread(target=broken_thread, name="synthetic-worker")
        worker.start()
        worker.join(3)
        try:
            raise ValueError("secret-value in main")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        content = path.read_text()
        assert "synthetic-worker" in content and "main" in content
        assert "secret-value" not in content
    finally:
        for name, (handlers, level) in previous.items():
            logging.getLogger(name).handlers[:] = handlers
            logging.getLogger(name).setLevel(level)
