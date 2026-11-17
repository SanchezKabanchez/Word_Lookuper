import os
import queue
import subprocess
import sys
import threading
import time

import psutil
import pytest
import word_lookup as w


def wait_until(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    assert predicate(), "Process state did not change before deadline"


def wait_for_pause(process, paused):
    """Suspend/resume acknowledgments may precede the OS status observation."""
    wait_until(lambda: (process.status() == psutil.STATUS_STOPPED) is paused)


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
        wait_for_pause(owned_process, True)
        assert guard.active
        assert guard.release()[0]
        wait_for_pause(owned_process, False)
        assert guard.release()[0]  # no second suspend/resume operation on the target
    finally:
        guard.release()


def test_watchdog_timeout_recovers_without_ui(owned_process):
    guard = w.GamePauseGuard(owned_process.pid, owned_process.create_time(), 1.5)
    try:
        guard.start()
        wait_for_pause(owned_process, True)
        wait_until(lambda: not guard.active)
        wait_for_pause(owned_process, False)
        assert guard.release()[0]
    finally:
        guard.release()


def test_pipe_closed_recovers_target(owned_process):
    guard = w.GamePauseGuard(owned_process.pid, owned_process.create_time(), 10)
    try:
        guard.start()
        guard.process.stdin.close()
        wait_until(lambda: not guard.active)
        wait_for_pause(owned_process, False)
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
        wait_for_pause(owned_process, True)
        parent.kill()
        parent.wait(timeout=3)
        wait_for_pause(owned_process, False)
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
    wait_for_pause(owned_process, False)
    assert not guard.active


def test_does_not_take_ownership_of_an_already_suspended_process(owned_process):
    first = w.GamePauseGuard(owned_process.pid, owned_process.create_time(), 10).start()
    try:
        second = w.GamePauseGuard(owned_process.pid, owned_process.create_time(), 10)
        with pytest.raises(RuntimeError, match="eligible"):
            second.start()
        wait_for_pause(owned_process, True)
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
    w.cfg["freeze"]["pause_mode"] = "snapshot"
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
    w.cfg["freeze"]["pause_mode"] = "snapshot"
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


@pytest.mark.parametrize("mode", ["word", "line"])
@pytest.mark.parametrize("pause_mode", ["snapshot", "lookup"])
def test_direct_ocr_pause_captures_before_worker_and_uses_selected_lifetime(app, monkeypatch, mode, pause_mode):
    from PIL import Image
    w.cfg["freeze"].update(pause_game=True, pause_mode=pause_mode)
    order, guards = [], []
    def game(name):
        order.append("validated")
        return 1234, 100, None
    monkeypatch.setattr(w, "foreground_game", game)
    class Guard(FakeGuard):
        def start(self):
            order.append("paused")
            return self
        def release(self):
            order.append("resumed")
            return super().release()
    def guard(*args):
        instance = Guard(); guards.append(instance); return instance
    monkeypatch.setattr(w, "GamePauseGuard", guard)
    image = Image.new("RGB", (1280, 800))
    def grab(*args):
        assert guards[0].active
        order.append("captured")
        return image, 0, 0
    monkeypatch.setattr(w, "_grab", grab)
    completed = threading.Event()
    def lookup(actual_mode, x, y, token, frozen=None):
        assert actual_mode == mode and (x, y) == (400, 500)
        assert frozen == (image, 0, 0)
        order.append("worker")
        completed.set()
    monkeypatch.setattr(app, "_lookup_thread", lookup)
    app._start_lookup(mode, (400, 500))
    assert completed.wait(2)
    expected = ["validated", "paused", "captured"]
    if pause_mode == "snapshot":
        expected.append("resumed")
    assert order == expected + ["worker"]
    assert guards[0].active is (pause_mode == "lookup")
    app.dismiss_lookup()
    assert not guards[0].active


def test_bound_mouse_lookup_uses_same_pause_route(app, monkeypatch):
    routed = []
    monkeypatch.setattr(app, "_start_lookup", lambda mode, pos=None: routed.append((mode, pos)))
    monkeypatch.setattr(app, "_mouse_match", lambda *args: True)
    app._handle(("press", 400, 500, "middle", set()))
    assert routed == [("word", (400, 500))]


def test_clipboard_lookup_never_starts_pause_or_captures_screen(app, monkeypatch):
    w.cfg["freeze"].update(pause_game=True, pause_mode="lookup")
    def unexpected(*args):
        raise AssertionError("Clipboard lookup must not pause or capture")
    monkeypatch.setattr(w, "foreground_game", unexpected)
    monkeypatch.setattr(w, "_grab", unexpected)
    completed = threading.Event()
    monkeypatch.setattr(app, "_lookup_thread", lambda *args: completed.set())
    app._start_lookup("selection", (400, 500))
    assert completed.wait(2)
    assert app._game_pause is None


def test_direct_lookup_refuses_unconfirmed_foreground_and_capture_failure_releases(app, monkeypatch):
    w.cfg["freeze"].update(pause_game=True, pause_mode="lookup")
    guards, worker = [], []
    monkeypatch.setattr(app, "_lookup_thread", lambda *args: worker.append(True))
    def wrong(name):
        raise RuntimeError("Wrong game")
    monkeypatch.setattr(w, "foreground_game", wrong)
    monkeypatch.setattr(w, "GamePauseGuard", lambda *args: guards.append(FakeGuard()))
    app._start_lookup("word", (400, 500))
    assert guards == [] and worker == []
    monkeypatch.setattr(w, "foreground_game", lambda name: (1234, 100, None))
    guard = FakeGuard()
    monkeypatch.setattr(w, "GamePauseGuard", lambda *args: guard)
    def broken_capture(*args):
        raise w.OcrError("Capture failed")
    monkeypatch.setattr(w, "_grab", broken_capture)
    app._start_lookup("word", (400, 500))
    assert not guard.active and app._game_pause is None and worker == []


def test_direct_lookup_releases_guard_if_start_raises_after_suspending(app, monkeypatch):
    w.cfg["freeze"].update(pause_game=True, pause_mode="lookup")
    monkeypatch.setattr(w, "foreground_game", lambda name: (1234, 100, None))
    class Guard(FakeGuard):
        def start(self):
            raise RuntimeError("Lost pause acknowledgement")
    guard = Guard()
    monkeypatch.setattr(w, "GamePauseGuard", lambda *args: guard)
    worker = []
    monkeypatch.setattr(app, "_lookup_thread", lambda *args: worker.append(True))
    app._start_lookup("word", (400, 500))
    assert not guard.active and guard.released == 1
    assert app._game_pause is None and worker == []


def test_lookup_pause_holds_after_selection_and_through_card_until_escape(app, card, monkeypatch):
    guard = app._game_pause = FakeGuard()
    app._game_pause_mode = "lookup"
    monkeypatch.setattr(app, "_region_thread", lambda *args: None)
    app._region_done(("point", 400, 500))
    assert guard.active and app._game_pause is guard
    app._handle(("show", card, None, (400, 500, 400, 500), app.active_token))
    assert app.popup and guard.active
    app._handle(("esc",))
    assert not guard.active and app._game_pause is None and app.popup is None


def test_lookup_pause_releases_on_error_with_partial_card_and_card_timeout(app, card):
    guard = app._game_pause = FakeGuard()
    app._game_pause_mode = "lookup"
    app.show_lookup(card, None, (400, 500, 400, 500))
    app._handle(("toast", "Translation failed"))
    assert not guard.active
    guard = app._game_pause = FakeGuard()
    app._game_pause_mode = "lookup"
    app.popup._on_timeout()
    assert not guard.active and app.popup is None


def test_expired_watchdog_and_late_result_never_restart_pause(app, card, monkeypatch):
    guard = app._game_pause = FakeGuard()
    app._game_pause_mode = "lookup"
    guard.active = False  # the external watchdog reached its original deadline
    starts = []
    monkeypatch.setattr(w, "GamePauseGuard", lambda *args: starts.append(True))
    token = app._new_token()
    app._pump()
    assert app._game_pause is None
    app._handle(("show", card, None, (400, 500, 400, 500), token))
    assert app.popup and app._game_pause is None and starts == []


def test_profile_enabled_pause_applies_to_direct_binding_even_when_global_disabled(app, monkeypatch):
    from PIL import Image
    w.cfg["freeze"].update(pause_game=False, pause_mode="lookup")
    def profile():
        app._lookup_profile = {"exe": "Example-Win64-Shipping.exe", "pause_game": True}
    monkeypatch.setattr(app, "_prepare_game_profile", profile)
    targets = []
    def game(name):
        targets.append(name)
        return 1234, 100, None
    monkeypatch.setattr(w, "foreground_game", game)
    guard = FakeGuard()
    monkeypatch.setattr(w, "GamePauseGuard", lambda *args: guard)
    monkeypatch.setattr(w, "_grab", lambda *args: (Image.new("RGB", (1280, 800)), 0, 0))
    completed = threading.Event()
    monkeypatch.setattr(app, "_lookup_thread", lambda *args: completed.set())
    app.do_action("lookup_word")
    assert completed.wait(2)
    assert targets == ["Example-Win64-Shipping.exe"]
    assert guard.active and app._game_pause_mode == "lookup"
    # A preference edit does not extend or shorten the guard already holding this lookup.
    w.cfg["freeze"]["pause_mode"] = "snapshot"
    app._after_capture_pause()
    assert guard.active


def test_free_cursor_confirmation_preserves_lookup_pause_until_dismiss(app, monkeypatch):
    from PIL import Image
    guard = app._game_pause = FakeGuard()
    app._game_pause_mode = "lookup"
    app.free_cursor = type("Cursor", (), {"profile": {}})()
    completed = threading.Event()
    monkeypatch.setattr(app, "_lookup_thread", lambda *args: completed.set())
    app._free_cursor_done((400, 500), (Image.new("RGB", (1280, 800)), 0, 0))
    app.root.update()
    assert completed.wait(2)
    assert guard.active
    app.dismiss_lookup()
    assert not guard.active


def test_lookup_pause_released_when_ui_result_handler_fails(app, monkeypatch):
    guard = app._game_pause = FakeGuard()
    app._game_pause_mode = "lookup"
    def broken_result(*args):
        raise RuntimeError("Cannot render card")
    monkeypatch.setattr(app, "_handle", broken_result)
    w.events.put(("show",))
    app._pump()
    assert not guard.active and app._game_pause is None


def test_profile_of_our_exact_stopped_foreground_reuses_original_guard(app, monkeypatch):
    w.cfg["freeze"].update(pause_game=False, pause_mode="lookup")
    w.cfg["game_profiles"] = {"example.exe": {"pause_game": True, "anki_tag": "Example"}}
    guard = app._game_pause = FakeGuard()
    guard.pid, guard.created = 1234, 100
    app._game_pause_hwnd = 77
    app._game_pause_mode = "lookup"
    info = {"name": "Example.exe", "pid": 1234, "created": 100, "hwnd": 77, "title": "Example game"}
    monkeypatch.setattr(w, "foreground_process_info", lambda: dict(info))
    monkeypatch.setattr(w, "_foreground_window", lambda: 77)
    def no_new_pause(*args):
        raise AssertionError("The existing exact-owner guard must be reused")
    monkeypatch.setattr(w, "foreground_game", no_new_pause)
    monkeypatch.setattr(w, "GamePauseGuard", no_new_pause)
    app._prepare_game_profile()
    assert app._lookup_profile["anki_tag"] == "Example"
    assert app._begin_game_pause()
    assert guard.active and guard.released == 0
    # A reused PID or another foreground window cannot inherit the old pause.
    info["created"] += 1
    app._prepare_game_profile()
    assert app._lookup_profile == {}
    w.cfg["freeze"]["pause_game"] = True
    assert not app._begin_game_pause()
    assert not guard.active


def test_free_cursor_uses_snapshot_and_resumes_before_selection(app, monkeypatch):
    from PIL import Image
    w.cfg["freeze"]["pause_mode"] = "snapshot"
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
        wait_for_pause(owned_process, True)
        assert list(tmp_path.glob("*.json"))
        watchdog = psutil.Process(watchdog_pid)
        watchdog.kill()
        wait_until(lambda: not watchdog.is_running() or watchdog.status() == psutil.STATUS_ZOMBIE)
        parent.kill()
        parent.wait(timeout=4)
        wait_for_pause(owned_process, True)
        outcomes = pause.recover_paused_games(folder)
        assert any("Відновлено" in outcome for outcome in outcomes)
        wait_for_pause(owned_process, False)
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
        wait_for_pause(owned_process, True)
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
        wait_for_pause(owned_process, True)
        assert not list(tmp_path.glob("*.json"))
    finally:
        owned_process.resume()
