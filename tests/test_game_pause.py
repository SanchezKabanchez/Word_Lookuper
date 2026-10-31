import os
import queue
import subprocess
import sys
import threading
import time

import psutil
import pytest
import word_lookup as w


def wait_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    assert predicate(), "Process state did not change before deadline"


@pytest.fixture
def owned_process():
    # The only process actually suspended by these tests is this owned sleep process.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    target = psutil.Process(child.pid)
    yield target
    if target.is_running():
        try:
            target.resume()
        except psutil.NoSuchProcess:
            pass
    child.terminate()
    child.wait(timeout=4)


def test_guard_really_suspends_and_resumes_owned_process(owned_process):
    guard = w.GamePauseGuard(owned_process.pid, owned_process.create_time(), 10)
    try:
        guard.start()
        assert owned_process.status() == psutil.STATUS_STOPPED
        assert guard.active
        assert guard.release()[0]
        assert owned_process.status() != psutil.STATUS_STOPPED
        assert guard.release()[0]  # no second suspend/resume operation on the target
    finally:
        guard.release()


def test_watchdog_timeout_recovers_without_ui(owned_process):
    guard = w.GamePauseGuard(owned_process.pid, owned_process.create_time(), 1.5)
    try:
        guard.start()
        assert owned_process.status() == psutil.STATUS_STOPPED
        wait_until(lambda: not guard.active)
        assert owned_process.status() != psutil.STATUS_STOPPED
        assert guard.release()[0]
    finally:
        guard.release()


def test_pipe_closed_recovers_target(owned_process):
    guard = w.GamePauseGuard(owned_process.pid, owned_process.create_time(), 10)
    try:
        guard.start()
        guard.process.stdin.close()
        wait_until(lambda: not guard.active)
        assert owned_process.status() != psutil.STATUS_STOPPED
        assert guard.release()[0]
    finally:
        guard.release()


def test_parent_crash_recovers_target(owned_process):
    code = ("import word_lookup_pause as w, time; "
            f"g=w.GamePauseGuard({owned_process.pid},{owned_process.create_time()},10).start(); "
            "print('PAUSED_CHILD',flush=True); time.sleep(60)")
    parent = subprocess.Popen([sys.executable, "-u", "-c", code], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    messages = queue.Queue()
    threading.Thread(target=lambda: messages.put(parent.stdout.readline()), daemon=True).start()
    try:
        assert messages.get(timeout=6).strip() == "PAUSED_CHILD"
        assert owned_process.status() == psutil.STATUS_STOPPED
        parent.kill()
        parent.wait(timeout=3)
        wait_until(lambda: owned_process.status() != psutil.STATUS_STOPPED)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=3)
        parent.stdout.close()
        parent.stderr.close()


def test_reused_identity_is_rejected_without_pausing(owned_process):
    guard = w.GamePauseGuard(owned_process.pid, owned_process.create_time() - 0.001, 10)
    with pytest.raises(RuntimeError, match="identity"):
        guard.start()
    assert owned_process.status() != psutil.STATUS_STOPPED
    assert not guard.active


def test_does_not_take_ownership_of_an_already_suspended_process(owned_process):
    first = w.GamePauseGuard(owned_process.pid, owned_process.create_time(), 10).start()
    try:
        second = w.GamePauseGuard(owned_process.pid, owned_process.create_time(), 10)
        with pytest.raises(RuntimeError, match="eligible"):
            second.start()
        assert owned_process.status() == psutil.STATUS_STOPPED
    finally:
        first.release()


def test_guard_rejects_self():
    with pytest.raises(ValueError):
        w.GamePauseGuard(os.getpid(), psutil.Process().create_time()).start()


def test_new_emergency_hotkey_preserves_existing_custom_binding():
    w.atomic_json_write(w.config_path(), {"hotkeys": {"lookup_word": "ctrl+alt+p"}})
    restored = w.load_config()
    assert restored["hotkeys"]["lookup_word"] == "ctrl+alt+p"
    assert restored["hotkeys"]["resume_game"] == ""


class FakeGuard:
    def __init__(self, *args):
        self.active = True
        self.released = 0

    def start(self):
        return self

    def release(self):
        self.released += 1
        self.active = False
        return True, "RESUMED"


def test_selection_resumes_before_ocr_or_popup(app, monkeypatch):
    guard = app._game_pause = FakeGuard()
    monkeypatch.setattr(app, "_region_thread", lambda *args: None)
    app._region_done(("point", 400, 400))
    assert not guard.active and app._game_pause is None
    app.dismiss_lookup()
    assert guard.released == 1


def test_cancel_and_error_release_pause(app):
    guard = app._game_pause = FakeGuard()
    app._region_done(None)
    assert not guard.active
    guard = app._game_pause = FakeGuard()
    app._handle(("toast", "OCR failed"))
    assert not guard.active


def test_emergency_action_releases_even_while_app_paused(app):
    guard = app._game_pause = FakeGuard()
    app.paused = True
    app.do_action("resume_game")
    assert not guard.active


def test_overlay_creation_failure_releases_pause(app, monkeypatch):
    w.cfg["freeze"]["pause_game"] = True
    monkeypatch.setattr(w, "foreground_game", lambda name: (1234, 100, None))
    guards = []
    def make_guard(*args):
        guard = FakeGuard(); guards.append(guard); return guard
    monkeypatch.setattr(w, "GamePauseGuard", make_guard)
    def fail(*args):
        raise RuntimeError("Could not create overlay")
    monkeypatch.setattr(w, "RegionSelector", fail)
    app.start_region_select()
    assert len(guards) == 1 and not guards[0].active
    assert app._game_pause is None


def test_free_cursor_failure_releases_pause(app, monkeypatch):
    w.cfg["freeze"]["pause_game"] = True
    monkeypatch.setattr(w, "foreground_game", lambda name: (1234, 100, None))
    guard = FakeGuard()
    monkeypatch.setattr(w, "GamePauseGuard", lambda *args: guard)
    def fail(*args):
        raise RuntimeError("Could not create cursor")
    monkeypatch.setattr(w, "FreeCursorOverlay", fail)
    app.start_free_cursor()
    assert not guard.active and app._game_pause is None


def test_wrong_foreground_does_not_start_overlay(app, monkeypatch):
    w.cfg["freeze"]["pause_game"] = True
    def wrong(*args):
        raise RuntimeError("Wrong game")
    monkeypatch.setattr(w, "foreground_game", wrong)
    called = []
    monkeypatch.setattr(w, "RegionSelector", lambda *args: called.append(True))
    app.start_region_select()
    assert called == [] and app._game_pause is None


def test_capture_resumes_game_before_image_render_or_ocr(app, monkeypatch):
    from PIL import Image
    guard = app._game_pause = FakeGuard()
    def grab(*args):
        assert guard.active
        return Image.new("RGB", (1280, 800), "#111318"), 0, 0
    monkeypatch.setattr(w, "_grab", grab)
    selector = w.RegionSelector(app, lambda *args: None)
    try:
        assert selector.frozen is not None
        assert not guard.active
    finally:
        selector.finish(None)


def test_free_cursor_uses_snapshot_and_resumes_before_selection(app, monkeypatch):
    from PIL import Image
    guard = app._game_pause = FakeGuard()
    monkeypatch.setattr(w, "_grab", lambda *args: (Image.new("RGB", (1280, 800)), 0, 0))
    completed = []
    overlay = w.FreeCursorOverlay(app, (400, 400), lambda *args: completed.append(args))
    assert not guard.active
    assert overlay.frozen is not None
    overlay.finish((420, 400))
    assert completed[0][0] == (420, 400)
    assert completed[0][1] is overlay.frozen


def test_frozen_watchdog_command_uses_dedicated_dispatch(monkeypatch, tmp_path):
    import word_lookup_pause as pause
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    command = pause.watchdog_command(1, 2, 3, 4, 20, tmp_path)
    assert command[:2] == [sys.executable, "--pause-watchdog"]
    assert "-c" not in command


def test_recovery_after_both_parent_and_watchdog_are_killed(owned_process, tmp_path):
    import word_lookup_pause as pause
    folder = str(tmp_path)
    code = ("import word_lookup_pause as p,time; "
            f"g=p.GamePauseGuard({owned_process.pid},{owned_process.create_time()},20,"
            f"recovery_dir={folder!r}).start(); "
            "print(g.process.pid,flush=True); time.sleep(60)")
    parent = subprocess.Popen([sys.executable, "-u", "-c", code], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    messages = queue.Queue()
    threading.Thread(target=lambda: messages.put(parent.stdout.readline()), daemon=True).start()
    try:
        watchdog_pid = int(messages.get(timeout=6).strip())
        assert owned_process.status() == psutil.STATUS_STOPPED
        assert list(tmp_path.glob("*.json"))
        watchdog = psutil.Process(watchdog_pid)
        watchdog.kill()
        wait_until(lambda: not watchdog.is_running() or watchdog.status() == psutil.STATUS_ZOMBIE)
        parent.kill()
        parent.wait(timeout=4)
        assert owned_process.status() == psutil.STATUS_STOPPED
        outcomes = pause.recover_paused_games(folder)
        assert any("Відновлено" in outcome for outcome in outcomes)
        assert owned_process.status() != psutil.STATUS_STOPPED
        assert not list(tmp_path.glob("*.json"))
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=4)
        parent.stdout.close()
        parent.stderr.close()


def test_recovery_never_resumes_reused_pid_or_live_owned_pause(owned_process, tmp_path):
    import word_lookup_pause as pause
    guard = pause.GamePauseGuard(owned_process.pid, owned_process.create_time(), 20,
                                recovery_dir=str(tmp_path)).start()
    try:
        assert pause.recover_paused_games(str(tmp_path)) == []
        assert owned_process.status() == psutil.STATUS_STOPPED
        assert list(tmp_path.glob("*.json"))
    finally:
        guard.release()
    owned_process.suspend()
    try:
        record = dict(pid=owned_process.pid, created=owned_process.create_time() - 1,
                      parent_pid=2147483647, parent_created=1,
                      watchdog_pid=2147483646, watchdog_created=1)
        pause._write_record(tmp_path / "stale.json", record)
        assert pause.recover_paused_games(str(tmp_path))
        assert owned_process.status() == psutil.STATUS_STOPPED
        assert not list(tmp_path.glob("*.json"))
    finally:
        owned_process.resume()
