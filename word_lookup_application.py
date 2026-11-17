from __future__ import annotations
import threading
import itertools
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, colorchooser
from pynput import keyboard, mouse
from word_lookup_theme import (APP_NAME, FONT, TRAY_OK)
from word_lookup_input import HotkeyManager, stop_input_listener
from word_lookup_models import (as_event)
from word_lookup_ui import (ScrolledPanel, ReviewWindow, AppViewMixin)
try:
    if TRAY_OK:
        import pystray
except (ImportError, RuntimeError):
    pystray = None

from word_lookup_input_controller import InputController
from word_lookup_window_controller import WindowController
from word_lookup_pause_controller import PauseController
from word_lookup_event_controller import EventController
from word_lookup_search_controller import SearchController


class Application(AppViewMixin, InputController, WindowController, PauseController, EventController, SearchController):
    def __init__(self, *, settings, cache, library, providers, ocr, anki, logger, diagnostics, support, events, instance=None):
        self.settings, self.cache, self.library = settings, cache, library
        self.providers, self.ocr, self.anki = providers, ocr, anki
        self.logger, self.diagnostics, self.support, self.events = logger, diagnostics, support, events
        self._token_counter = itertools.count(1)
        self.scrolled_panel, self.review_window = ScrolledPanel, ReviewWindow
        self.instance = instance
        from word_lookup_dpi import configure_dpi
        self.dpi_mode = configure_dpi()
        self.root = tk.Tk()
        self.root.report_callback_exception = lambda kind, value, tb: self.logger.exception("Tk callback", (kind, value, tb))
        self.root.title(APP_NAME)
        tkfont.nametofont("TkDefaultFont").configure(family=FONT, size=9)
        tkfont.nametofont("TkTextFont").configure(family=FONT, size=10)
        self._setting_vars = {}
        self._config_save_timer = None
        self._preview_timer = None
        self._apply_style()
        self.kb = keyboard.Controller()
        self.ms = mouse.Controller()
        self.popup = self.highlight = self.toast_popup = None
        self.selector = None
        self._game_pause = None
        self._game_pause_hwnd = None
        self._game_pause_mode = "snapshot"
        self.free_cursor = None
        self.free_cursor_pos = None
        self.saved_checks = {}
        self._fc_lock = threading.Lock()
        self._fc_delta = [0, 0]
        self._fc_mouse_listener = None
        self._fc_mouse_pos = None
        self.hl_hit = None
        self.active_token = None
        self._lookup_profile = {}
        self._lookup_profiles = {}
        self.paused = False
        self.last_card = None
        self.busy = threading.Lock()
        self.recording = None
        self.tray = None
        self.press_state = {"pos": (0, 0), "t": 0.0}
        self.auto_state = {"press": (0, 0), "last_release": 0.0}

        self.manager = HotkeyManager(lambda a: self.events.put(as_event(("action", a))))
        self.manager.set_bindings(self.settings.data["hotkeys"])
        self.manager.on_esc = lambda: self.events.put(as_event(("esc",)))

        self._build_ui()
        self._refresh_hint()
        self._refresh_status()
        self._refresh_cache_label()
        self.refresh_saved_count()
        self._check_ocr()
        self.manager.start()
        self.mouse_listener = mouse.Listener(on_click=self._on_click)
        self.mouse_listener.daemon = True
        self.mouse_listener.start()
        self._setup_tray()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(40, self._pump)
        if self.settings.data["app"]["start_minimized"]:
            self.root.after(300, self.hide_window)


    def run(self):
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self.logger.exception('word_lookup.run')


    def pick_color(self):
        res = colorchooser.askcolor(color=self.settings.data["popup"]["highlight_color"], title="Колір підсвічування")
        if res and res[1]:
            self.settings.data["popup"]["highlight_color"] = res[1]
            self.settings.save_config(self.settings.data)
            self.color_btn.config(bg=res[1])


    def browse_tesseract(self):
        path = filedialog.askopenfilename(title="Вкажи tesseract (.exe)")
        if path:
            self.settings.data["ocr"]["tesseract_cmd"] = path
            self.settings.save_config(self.settings.data)
            self._check_ocr()


    def _language_changed(self):
        self.dismiss_lookup()


    def quit(self):
        if not self._commit_or_discard_editor():
            return
        self._flush_config_save()
        if self._preview_timer is not None:
            self.root.after_cancel(self._preview_timer)
            self._preview_timer = None
        self._new_token()
        self._resume_game()
        self.cache.save_card_cache()
        self._stop_free_cursor_input()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                self.logger.exception("Завершення трея")
        for listener in (self.manager.listener, self.mouse_listener):
            stop_input_listener(listener)
        self.close_all()
        # Cancel each job through the widget that registered its Tcl command.
        # Cancelling a Toplevel job through root would leave a duplicate entry
        # in the child's command list and break destruction of that child.
        owners = {}
        def collect(widget):
            owners.update((name, widget) for name in (widget._tclCommands or ()))
            for child in widget.winfo_children():
                collect(child)
        collect(self.root)
        for timer in self.root.tk.call("after", "info"):
            script = self.root.tk.call("after", "info", timer)[0]
            command = self.root.tk.splitlist(script)[0]
            owner = owners.get(command)
            if owner is not None:
                owner.after_cancel(timer)
        self.root.destroy()
        self.kb = self.ms = None


    def post_lookup(self, token, kind, *args):
        self.events.put(as_event(("lookup_event", token, (kind, *args))))

