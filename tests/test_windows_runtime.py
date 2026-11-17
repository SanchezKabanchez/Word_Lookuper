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


def test_xrecord_control_close_waits_for_listener_thread_to_finish():
    class Control:
        def record_disable_context(self, context):
            assert context == 7
        def flush(self):
            raise TypeError('control connection already closed by backend')
    class Listener:
        __module__ = 'pynput.mouse._xorg'
        _ready = True
        _context = 7
        _display_stop = Control()
        def __init__(self):
            self.alive = True
            self.stopping = False
        def is_alive(self):
            return self.alive
        def stop(self):
            self.stopping = True
        def join(self, timeout):
            assert self.stopping and timeout > 0
            self.alive = False
    listener = Listener()
    assert w.stop_input_listener(listener)
    assert not listener.is_alive()


def test_free_cursor_hint_has_padded_dark_plate_on_bright_scene(app, monkeypatch):
    from PIL import Image
    monkeypatch.setattr(w, '_grab', lambda *args, **kwargs: (Image.new('RGB', (1280, 800), 'white'), 0, 0, 1280, 800))
    overlay = w.FreeCursorOverlay(app, (200, 200), lambda *args: None)
    try:
        app.root.update()
        overlay._layout_hint(640)
        canvas = overlay.canvas
        plate = canvas.coords(overlay.hint_plate)
        text = canvas.bbox(overlay.hint)
        assert plate[0] < text[0] and plate[1] < text[1]
        assert plate[2] > text[2] and plate[3] > text[3]
        assert 0 <= plate[0] < plate[2] <= 640
        assert canvas.itemcget(overlay.hint_plate, 'fill') == '#171a22'
    finally:
        overlay.finish(None)
