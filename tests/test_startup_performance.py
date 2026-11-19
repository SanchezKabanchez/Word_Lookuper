"""Startup must remain useful without loading unused native/HTTP runtimes."""
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
import word_lookup as w


def test_real_window_and_listeners_start_without_unused_runtimes(tmp_path):
    code = """
import sys
import word_lookup as w
def run(app):
    def inspect():
        try:
            forbidden = ('numpy', 'cv2', 'onnxruntime', 'rapidocr_onnxruntime',
                         'ctranslate2', 'sentencepiece', 'pytesseract', 'pygame', 'requests')
            assert not [name for name in forbidden if name in sys.modules]
            assert app.root.winfo_ismapped()
            assert app.manager.listener.running
            assert app.mouse_listener.running
            assert app.notebook.index('end') == 4
            print('startup-ready-without-unused-runtimes', flush=True)
        finally:
            app.quit()
    app.root.after(300, inspect)
    app.root.mainloop()
w.App.run = run
w.main()
"""
    child = subprocess.run([sys.executable, "-c", code],
        env=dict(os.environ, WORD_LOOKUP_DATA_DIR=str(tmp_path)),
        capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert child.returncode == 0, child.stderr
    assert "startup-ready-without-unused-runtimes" in child.stdout, child.stderr


@pytest.mark.parametrize("returncode, ready", [(0, True), (1, False)])
def test_tesseract_probe_checks_exit_status_without_importing_wrapper(monkeypatch, returncode, ready):
    import word_lookup_ocr as ocr_module
    w.cfg["ocr"]["tesseract_cmd"] = "configured-tesseract"
    monkeypatch.setattr(ocr_module.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(w, "_configure_tesseract_cmd", lambda: pytest.fail("Readiness imported OCR wrapper"))
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=returncode)
    monkeypatch.setattr(ocr_module.subprocess, "run", run)
    assert w.tesseract_ready() is ready
    assert calls[0][0] == ["configured-tesseract", "--version"]
    assert calls[0][1]["timeout"] == 2


def test_missing_or_timed_out_tesseract_does_not_report_ready(monkeypatch):
    import word_lookup_ocr as ocr_module
    monkeypatch.setattr(ocr_module.importlib.util, "find_spec", lambda _name: None)
    monkeypatch.setattr(ocr_module.subprocess, "run", lambda *_a, **_k: pytest.fail("Wrapper not installed"))
    assert not w.tesseract_ready()
    monkeypatch.setattr(ocr_module.importlib.util, "find_spec", lambda _name: object())
    w.cfg["ocr"]["tesseract_cmd"] = "configured-tesseract"
    def timeout(*_a, **_k):
        raise subprocess.TimeoutExpired("configured-tesseract", 2)
    monkeypatch.setattr(ocr_module.subprocess, "run", timeout)
    assert not w.tesseract_ready()


def test_session_is_created_on_use_and_preserves_pool_headers_and_cookies(monkeypatch):
    import requests
    from word_lookup_network import LockedSession
    session = LockedSession({"User-Agent": "WordLookup-test"})
    assert session._session is None
    session.close()
    assert session._session is None
    clients = []
    def request(client, method, url, **_kwargs):
        clients.append(client)
        assert client.headers["User-Agent"] == "WordLookup-test"
        return client.cookies.get("state")
    monkeypatch.setattr(requests.Session, "request", request)
    assert session.get("https://example.invalid") is None
    session.cookies.set("state", "remembered")
    assert session.post("https://example.invalid") == "remembered"
    assert clients[0] is clients[1]
    session.close()


def test_empty_recovery_directory_does_not_load_process_runtime(tmp_path):
    child = subprocess.run([sys.executable, "-c",
        "import sys; from word_lookup_pause import recover_paused_games; "
        "assert recover_paused_games(sys.argv[1]) == []; assert 'psutil' not in sys.modules",
        str(tmp_path)], capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert child.returncode == 0, child.stderr
