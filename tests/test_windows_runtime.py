"""Real Tk overlays and pynput events; uses only our own windows/processes."""
import queue
import threading

import word_lookup as w


def test_region_overlay_accepts_cancel_and_retains_frozen_image(app):
    completed = []
    selector = w.RegionSelector(app, lambda *args: completed.append(args))
    app.root.update()
    assert selector.win.winfo_exists()
    assert selector.frozen is not None
    selector.finish(None)
    app.root.update()
    assert completed[0][0] is None
    assert completed[0][1] is selector.frozen


def test_free_cursor_overlay_moves_and_returns_snapshot(app):
    completed = []
    overlay = w.FreeCursorOverlay(app, (200, 200), lambda *args: completed.append(args))
    app.root.update()
    overlay.key("right", False)
    overlay.key("enter", False)
    assert completed[0][0] == (208, 200)
    assert completed[0][1] is not None


def test_global_hotkey_listener_receives_owned_synthetic_key(app):
    received = queue.Queue()
    manager = w.HotkeyManager(received.put)
    manager.set_bindings({"lookup_word": "ctrl+alt+f9"})
    manager.start()
    ready = threading.Event()
    threading.Thread(target=lambda: (manager.listener.wait(), ready.set()), daemon=True).start()
    try:
        controller = w.keyboard.Controller()
        # Exercise the actual OS listener, never a game or third-party window.
        assert ready.wait(timeout=4), "Global keyboard listener did not become ready"
        with controller.pressed(w.keyboard.Key.ctrl):
            with controller.pressed(w.keyboard.Key.alt):
                controller.press(w.keyboard.Key.f9)
                controller.release(w.keyboard.Key.f9)
        assert received.get(timeout=4) == "lookup_word"
    finally:
        if ready.is_set():
            assert w.stop_input_listener(manager.listener)


def test_owned_listeners_terminate_on_quit():
    app = w.App()
    app.root.update()
    listeners = (app.manager.listener, app.mouse_listener)
    # Readiness is bounded inside the adapter; failed startup cannot hang quit.
    app.quit()
    assert all(not listener.is_alive() for listener in listeners)


def test_free_cursor_listener_stops_without_leaking_xrecord(app):
    listener = app._fc_mouse_listener = w.mouse.Listener(on_move=lambda x, y: None)
    listener.start()
    app._stop_free_cursor_input()
    assert not listener.is_alive()
    assert app._fc_mouse_listener is None
