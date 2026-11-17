"""Frozen-screen OCR selection with keyboard and pointer navigation."""
import logging
import queue
import sys
import threading
import tkinter as tk
from word_lookup_utils import clamp
from wordlookup.network.budget import LookupCancelled, check_cancelled, network_scope
from word_lookup_profiles import profile_scope
from word_lookup_theme import (FONT)
import word_lookup_platform as platform

from word_lookup_geometry import clean_word, cluster_lines


def _cancel_view_timers(win):
    """Remove delayed overlay callbacks before Tcl deletes their commands."""
    commands = set(win._tclCommands or ())
    for timer in win.tk.call("after", "info"):
        script = win.tk.call("after", "info", timer)[0]
        if win.tk.splitlist(script)[0] in commands:
            win.after_cancel(timer)


def _monitor_at(monitors, point):
    """Select a physical monitor without combining unrelated display pixels."""
    px, py = point
    for monitor in monitors:
        x, y = monitor["left"], monitor["top"]
        right, bottom = x + monitor["width"], y + monitor["height"]
        if x <= px < right and y <= py < bottom:
            return x, y, right, bottom
    return None


def _active_monitor_bounds(app, hwnd, fallback):
    """Prefer the foreground game's display, then the pointer's display."""
    if sys.platform.startswith("win") and hwnd:
        try:
            import ctypes
            from ctypes import wintypes
            class MonitorInfo(ctypes.Structure):
                _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                            ("work", wintypes.RECT), ("flags", wintypes.DWORD)]
            user = ctypes.windll.user32
            user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user.MonitorFromWindow.restype = wintypes.HANDLE
            user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            monitor = user.MonitorFromWindow(hwnd, 2)
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            if monitor and user.GetMonitorInfoW(monitor, ctypes.byref(info)):
                box = info.monitor
                return box.left, box.top, box.right, box.bottom
        except Exception:
            logging.getLogger(__name__).exception("Монітор активної гри")
    try:
        import mss
        with mss.MSS() as capture:
            return _monitor_at(capture.monitors[1:], app.ms.position) or fallback
    except Exception:
        logging.getLogger(__name__).exception("Монітор для стоп-кадру")
        return fallback


def _hit_at(hits, x, y):
    """Containment comes first; overlapping boxes use the nearest word center."""
    candidates = (hit for hit in hits if hit is not None and
                  hit["box"][0] <= x < hit["box"][2] and hit["box"][1] <= y < hit["box"][3])
    return min(candidates, key=lambda hit: ((hit["box"][0] + hit["box"][2]) / 2 - x) ** 2 +
               ((hit["box"][1] + hit["box"][3]) / 2 - y) ** 2, default=None)


class WordNavigator:
    """Reading-order words and geometry for selection without cursor movement."""

    def __init__(self, words):
        self.words = tuple(words)
        self.lines = []
        for line in cluster_lines(self.words):
            hits = []
            line_text = " ".join(w.text for w in line)
            for word in line:
                cleaned, lead, trail = clean_word(word.text)
                if cleaned:
                    width = word.x1 - word.x0
                    hits.append({"word": cleaned, "line_text": line_text,
                                 "_frame_words": self.words,
                                 "box": (int(word.x0 + width * lead), int(word.y0),
                                         int(word.x1 - width * trail), int(word.y1))})
            if hits:
                self.lines.append(hits)
        self.row = self.column = 0
        self.preferred_x = self._center(self.current) if self.current else None

    @staticmethod
    def _center(hit):
        return (hit["box"][0] + hit["box"][2]) / 2

    @property
    def current(self):
        return self.lines[self.row][self.column] if self.lines else None

    def move(self, key):
        if not self.lines:
            return None
        if key in ("left", "right"):
            self.column = max(0, min(self.column + (-1 if key == "left" else 1),
                                     len(self.lines[self.row]) - 1))
            self.preferred_x = self._center(self.current)
        elif key in ("up", "down"):
            self.row = max(0, min(self.row + (-1 if key == "up" else 1), len(self.lines) - 1))
            self.column = min(range(len(self.lines[self.row])),
                              key=lambda i: abs(self._center(self.lines[self.row][i]) - self.preferred_x))
        return self.current

    def hit_at(self, x, y):
        return _hit_at((hit for line in self.lines for hit in line), x, y)


class RegionSelector:
    """Frozen screenshot with word/line keyboard navigation and pointer selection.

    OCR runs on the captured frame in a worker. Global keyboard input is queued
    for Tk, so navigation also works when a game retains keyboard focus.
    """

    HOVER_DELAY_MS = 250  # wait for a deliberate stop instead of every passing word
    HOVER_CACHE_SIZE = 64
    HINT_BACKGROUND = "#111318"
    HINT_PADDING = (16, 10)

    def __init__(self, app, on_done):
        self.app = app
        self.on_done, self.start, self.done = on_done, None, False
        self.hover_hit, self.mouse = None, (-9999, -9999)
        self._after_id, self._req, self._busy = None, None, False
        self._lock, self._hq = threading.Lock(), queue.Queue()
        self._active_req, self._hover_cache = None, []
        self.navigator = None
        self.profile = dict(getattr(app, "_lookup_profile", {}) or {})
        mode = self.profile.get("selection_mode", self.app.settings.data["freeze"].get("selection_mode", "keyboard"))
        self.keyboard_enabled = mode == "keyboard"
        self._manager = app.manager
        self._key_callback = self._queue_key
        self._prev_hwnd = platform._foreground_window()  # повернути фокус грі після закриття
        r = app.root
        try:
            x, y = r.winfo_vrootx(), r.winfo_vrooty()
            w, h = r.winfo_vrootwidth(), r.winfo_vrootheight()
            if w < 100 or h < 100:
                raise ValueError
        except Exception:
            x, y, w, h = 0, 0, r.winfo_screenwidth(), r.winfo_screenheight()
        if self.app.settings.data.get("performance", {}).get("capture_active_monitor", True):
            x, y, right, bottom = _active_monitor_bounds(app, self._prev_hwnd, (x, y, x + w, y + h))
            w, h = right - x, bottom - y
        self.w, self.h = w, h
        self.ox, self.oy = x, y  # початок віртуального екрана: екранні координати -> координати полотна
        self.frozen = None  # (img, ox, oy) — заповниться нижче, якщо знімок вдався

        self.win = win = tk.Toplevel(r)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="#111318")
        win.geometry(f"{w}x{h}+{x}+{y}")
        c = self.canvas = tk.Canvas(win, bg="#111318", highlightthickness=0, cursor="arrow")
        c.pack(fill="both", expand=True)

        frozen_ok = False
        try:
            from PIL import ImageTk
            shot_img, sx0, sy0, *_ = self.app.ocr._grab(x, y, x + w, y + h)
            app._after_capture_pause()
            self._photo = ImageTk.PhotoImage(shot_img, master=win)  # тримаємо посилання — інакше Tk «забуде» картинку
            c.create_image(sx0 - self.ox, sy0 - self.oy, image=self._photo, anchor="nw")
            self.frozen = (shot_img, sx0, sy0)
            frozen_ok = True
        except Exception as e:
            app._resume_game()
            self.app.logger.write(f"Стоп-кадр не вдався, буде звичайне затемнення: {type(e).__name__}: {e}")
            try:
                win.attributes("-alpha", 0.32)
            except tk.TclError:
                self.app.logger.exception('word_lookup.__init__')

        self.hover_enabled = (not self.keyboard_enabled and
                              bool(self.app.settings.data["freeze"].get("hover_highlight", True)) and
                              self.frozen is not None)
        hl_color = self.app.settings.data["popup"]["highlight_color"]
        # Draw outside the OCR box: a tint/stipple obscures the actual glyphs,
        # especially black text on a light game scene.
        self.hl_rect = c.create_rectangle(0, 0, 0, 0, fill="", outline=hl_color,
                                          width=2, state="hidden")
        self.hl_underline = c.create_line(0, 0, 0, 0, fill=hl_color, width=2, state="hidden")
        self.vline = c.create_line(0, 0, 0, h, fill="#ffd43b", state="hidden")  # перехрестя вимкнено:
        self.hline = c.create_line(0, 0, w, 0, fill="#ffd43b", state="hidden")  # курсор — звичайна стрілка
        self.rect = c.create_rectangle(0, 0, 0, 0, outline="#ffd43b", width=2, state="hidden")
        self.size = c.create_text(0, 0, text="", fill="#ffd43b", anchor="sw",
                                  font=(FONT, 11, "bold"), state="hidden")
        hint = ("⏸ Стоп-кадр — екран заморожено, підказки (тултіпи) нікуди не зникнуть.  "
                if frozen_ok else "") + \
               ("Розпізнаю слова…  ·  ←/→ — слова  ·  ↑/↓ — рядки  ·  Enter — картка  ·  Esc — скасувати"
                if self.keyboard_enabled else
                "Наведи курсор на слово — воно підсвітиться  ·  клік лівою кнопкою — шукати  ·  "
                "або обведи рамкою кілька слів  ·  Esc / права кнопка — скасувати"
                if self.hover_enabled else
                "Тягни рамку навколо слова чи рядка  ·  клік — одне слово  ·  Esc / права кнопка — скасувати")
        if app._game_pause is not None and app._game_pause.active:
            hint = "⏸ Процес гри призупинений · Esc — відновити.  " + hint
        self._hint_screen_size = (r.winfo_screenwidth(), r.winfo_screenheight())
        self._hint_text = hint
        self._hint_layout_key = None
        self.hint_backplate = c.create_rectangle(0, 0, 0, 0, fill=self.HINT_BACKGROUND,
                                                outline="#3a3f4b", width=1)
        self.hint = c.create_text(0, 0, fill="#f1f1f5", anchor="n", justify="center",
                                 font=(FONT, 13, "bold"), text=hint)
        self._layout_hint()
        c.bind("<Configure>", self._layout_hint)
        c.bind("<Motion>", self._motion)
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Button-3>", lambda _e: self.finish(None))
        win.bind("<Escape>", lambda _e: self.finish(None))
        win.focus_force()
        c.focus_set()
        win.after(50, self._poll)
        if not self.keyboard_enabled:
            platform.release_cursor_clip()
            win.after(100, self._unclip_tick)
        if self.keyboard_enabled:
            if self.frozen is not None:
                threading.Thread(target=self._keyboard_ocr, daemon=True).start()
            else:
                self._set_keyboard_status("Не вдалося зробити стоп-кадр. Esc — скасувати; можна виділити мишею.")
            self._manager.start_raw_mode(self._key_callback)

    def _queue_key(self, name):
        resume = self.app.settings.data["hotkeys"].get("resume_game", "")
        if resume and frozenset(self._manager.down) == frozenset(resume.split("+")):
            name = "esc"
        self._hq.put(("key", name))

    def _keyboard_ocr(self):
        """Read the frozen frame once; never capture a newer screen here."""
        image, x, y = self.frozen
        try:
            with network_scope(lambda: self.done), profile_scope(self.profile):
                check_cancelled()
                words = self.app.ocr.ocr_region(x, y, x + image.width, y + image.height, frozen=self.frozen)
                check_cancelled()
                self._hq.put(("words", words))
        except LookupCancelled:
            return
        except Exception as exc:
            if self.done:
                return
            self.app.logger.exception("Розпізнавання слів для клавіатури")
            self._hq.put(("error", str(exc)))

    def _set_keyboard_status(self, text):
        self._hint_text = text
        self._layout_hint()

    def _layout_hint(self, event=None):
        """Keep a padded opaque hint readable within the visible screen area."""
        canvas = self.canvas
        width = event.width if event is not None else canvas.winfo_width()
        height = event.height if event is not None else canvas.winfo_height()
        width, height = width if width > 1 else self.w, height if height > 1 else self.h
        key = (width, height, self._hint_screen_size, self._hint_text,
               tuple(self.hover_hit["box"]) if self.hover_hit else None,
               float(canvas.tk.call("tk", "scaling")))
        if key == self._hint_layout_key:
            return
        screen_w, screen_h = self._hint_screen_size
        left = min(max(0, -self.ox), max(0, width - screen_w))
        top = min(max(0, -self.oy), max(0, height - screen_h))
        area_w, area_h = min(screen_w, width - left), min(screen_h, height - top)
        margin = min(16, max(2, min(area_w, area_h) // 10))
        pad_x, pad_y = self.HINT_PADDING
        wrap = max(1, min(1000, area_w - 2 * (margin + pad_x) - 4))
        canvas.coords(self.hint, left + area_w / 2, top + margin + pad_y)
        canvas.itemconfigure(self.hint, text=self._hint_text, width=wrap)

        # OCR can return an exceptionally long line or error. Keep cancellation
        # visible instead of letting that text spill outside a smaller display.
        available_h = max(1, area_h - 2 * (margin + pad_y) - 2)
        bounds = canvas.bbox(self.hint)
        if bounds and bounds[3] - bounds[1] > available_h:
            tail = "…\nEsc — скасувати" if "Esc" in self._hint_text else "…"
            low, high = 0, len(self._hint_text)
            while low < high:
                middle = (low + high + 1) // 2
                canvas.itemconfigure(self.hint, text=self._hint_text[:middle].rstrip() + tail)
                box = canvas.bbox(self.hint)
                if box[3] - box[1] <= available_h:
                    low = middle
                else:
                    high = middle - 1
            canvas.itemconfigure(self.hint, text=self._hint_text[:low].rstrip() + tail)
            bounds = canvas.bbox(self.hint)
        if bounds:
            plate = (bounds[0] - pad_x, bounds[1] - pad_y, bounds[2] + pad_x, bounds[3] + pad_y)
            if self.hover_hit:
                bx0, by0, bx1, by1 = self.hover_hit["box"]
                word = (bx0 - self.ox, by0 - self.oy, bx1 - self.ox, by1 - self.oy)
                if self._overlaps(plate, word):
                    shift = top + area_h - margin - plate[3]
                    bottom_plate = (plate[0], plate[1] + shift, plate[2], plate[3] + shift)
                    if bottom_plate[1] >= top + margin and not self._overlaps(bottom_plate, word):
                        canvas.move(self.hint, 0, shift)
                        plate = bottom_plate
            canvas.coords(self.hint_backplate, *plate)
        canvas.tag_raise(self.hint_backplate)
        canvas.tag_raise(self.hint)
        self._hint_layout_key = key

    @staticmethod
    def _overlaps(first, second):
        return (first[0] < second[2] and first[2] > second[0]
                and first[1] < second[3] and first[3] > second[1])

    def _show_keyboard_word(self):
        hit = self.navigator.current if self.navigator else None
        if hit:
            self._hint_text = (
                f"←/→ — слова  ·  ↑/↓ — рядки  ·  Enter — картка  ·  Esc — скасувати\n"
                f"Рядок {self.navigator.row + 1}/{len(self.navigator.lines)}: {hit['line_text']}")
            self._set_hover(hit)

    def key(self, name):
        if self.done:
            return
        if name == "esc":
            self.finish(None)
        elif name == "enter" and self.navigator and self.navigator.current:
            hit = self.navigator.current
            x = (hit["box"][0] + hit["box"][2]) // 2
            y = (hit["box"][1] + hit["box"][3]) // 2
            self.finish(("word", x, y, hit))
        elif name in ("left", "right", "up", "down") and self.navigator:
            self.navigator.move(name)
            self._show_keyboard_word()

    def _unclip_tick(self):
        """Деякі ігри самі повертають обмеження курсора — відпускаємо знову, поки накладка відкрита."""
        if self.done:
            return
        platform.release_cursor_clip()
        try:
            self.win.after(100, self._unclip_tick)
        except Exception:
            self.app.logger.exception('word_lookup._unclip_tick')

    # ---------- підсвітка слова під курсором ----------
    @staticmethod
    def _inside(box, x, y, margin=0) -> bool:
        return box[0] - margin <= x < box[2] + margin and box[1] - margin <= y < box[3] + margin

    def _clear_hover(self):
        if self.hover_hit is None:
            return
        self.hover_hit = None
        self.canvas.itemconfigure(self.hl_rect, state="hidden")
        self.canvas.itemconfigure(self.hl_underline, state="hidden")
        self._layout_hint()

    def _set_hover(self, hit):
        previous = self.hover_hit
        self.hover_hit = hit
        if previous and previous["box"] == hit["box"] and previous["word"] == hit["word"]:
            self._layout_hint()
            return
        bx0, by0, bx1, by1 = hit["box"]
        self.canvas.coords(self.hl_rect, bx0 - self.ox - 3, by0 - self.oy - 3,
                           bx1 - self.ox + 3, by1 - self.oy + 3)
        self.canvas.coords(self.hl_underline, bx0 - self.ox - 3, by1 - self.oy + 6,
                           bx1 - self.ox + 3, by1 - self.oy + 6)
        self.canvas.itemconfigure(self.hl_rect, state="normal")
        self.canvas.itemconfigure(self.hl_underline, state="normal")
        self._layout_hint()

    def _motion(self, e):
        previous = self.mouse
        self.mouse = (e.x_root, e.y_root)
        if not self.hover_enabled or self.start:  # під час перетягування рамки не розпізнаємо
            return
        if self.hover_hit and not self._inside(self.hover_hit["box"], e.x_root, e.y_root):
            self._clear_hover()
        if self.hover_hit is None:
            if self._after_id is not None and max(abs(e.x_root - previous[0]), abs(e.y_root - previous[1])) <= 2:
                return  # stationary/jitter events must not keep postponing OCR
            if self._after_id is not None:
                try:
                    self.win.after_cancel(self._after_id)
                except Exception:
                    self.app.logger.exception('word_lookup._motion')
            delay = self.app.settings.data.get("performance", {}).get("hover_delay_ms", self.HOVER_DELAY_MS)
            self._after_id = self.win.after(int(clamp(delay, 100, 1500)), lambda: self._request(*self.mouse))

    def _cached_hover(self, x, y):
        """Reuse real word boxes; a miss is valid only for its exact point."""
        hit = _hit_at((hit for _, hit in reversed(self._hover_cache)), x, y)
        if hit is not None:
            return True, hit
        for request, hit in reversed(self._hover_cache):
            if hit is None and request == (x, y):
                return True, None
        return False, None

    def _request(self, x, y):
        self._after_id = None
        if self.done:
            return
        with self._lock:
            cached, hit = self._cached_hover(x, y)
            if cached:
                self._req = None
                self._hq.put(("hover", (x, y), hit))
                return
            if self._active_req == (x, y):
                self._req = None
                return
            self._req = (x, y)
            spawn = not self._busy
            self._busy = True
        if spawn:
            threading.Thread(target=self._ocr_worker, daemon=True).start()

    def _ocr_worker(self):
        """Один фоновий потік: бере лише НАЙНОВІШИЙ запит, старі пропускає."""
        while True:
            with self._lock:
                if self.done:
                    self._req, self._active_req, self._busy = None, None, False
                    return
                req, self._req = self._req, None
                if req is None:
                    self._busy = False
                    return
                self._active_req = req
            try:
                failed = False
                with network_scope(lambda: self.done), profile_scope(self.profile):
                    check_cancelled()
                    hit = self.app.ocr.find_word_at(req[0], req[1], frozen=self.frozen)
                    check_cancelled()
            except LookupCancelled:
                with self._lock:
                    self._req, self._active_req, self._busy = None, None, False
                return
            except Exception:
                if not self.done:
                    self.app.logger.exception("Підсвічування слова на стоп-кадрі")
                failed = True
                hit = None
            with self._lock:
                self._active_req = None
                if self.done:
                    self._req, self._busy = None, False
                    return
                if not failed:
                    self._hover_cache.append((req, hit))
                    self._hover_cache = self._hover_cache[-self.HOVER_CACHE_SIZE:]
            self._hq.put(("hover", req, hit))

    def _poll(self):
        if self.done:
            return
        hover_results = []
        while True:
            try:
                item = self._hq.get_nowait()
            except queue.Empty:
                break
            if item[0] == "hover":
                hover_results.append(item[1:])
            elif item[0] == "words":
                self.navigator = WordNavigator(item[1])
                if self.navigator.current:
                    self._show_keyboard_word()
                else:
                    self._set_keyboard_status("Слів не знайдено. Esc — скасувати; можна виділити мишею.")
            elif item[0] == "error":
                self._set_keyboard_status(f"Помилка OCR: {item[1]}  ·  Esc — скасувати")
            elif item[0] == "key":
                self.key(item[1])
                if self.done:
                    return
        if hover_results and self.start is None:
            mx, my = self.mouse
            candidates = [hit for _, hit in reversed(hover_results)] + [self.hover_hit]
            hit = _hit_at(candidates, mx, my)
            if hit is not None:
                self._set_hover(hit)
            else:
                if self.hover_hit and not self._inside(self.hover_hit["box"], mx, my):
                    self._clear_hover()
        try:
            self.win.after(50, self._poll)
        except Exception:
            self.app.logger.exception('word_lookup._poll')

    def _move(self, e):
        self.canvas.coords(self.vline, e.x, 0, e.x, self.h)
        self.canvas.coords(self.hline, 0, e.y, self.w, e.y)

    def _press(self, e):
        self.mouse = (e.x_root, e.y_root)
        if self.hover_hit and not self._inside(self.hover_hit["box"], *self.mouse):
            self._clear_hover()
        if self._after_id is not None:
            self.win.after_cancel(self._after_id)
            self._after_id = None
        with self._lock:
            self._req = None
        self.start = (e.x, e.y, e.x_root, e.y_root)
        self.canvas.itemconfigure(self.rect, state="normal")

    def _drag(self, e):
        self._move(e)
        if self.start:
            sx, sy = self.start[:2]
            if max(abs(e.x - sx), abs(e.y - sy)) >= 6:
                self._clear_hover()
            self.canvas.coords(self.rect, sx, sy, e.x, e.y)
            self.canvas.coords(self.size, min(sx, e.x), min(sy, e.y) - 4)
            self.canvas.itemconfigure(self.size, text=f"{abs(e.x - sx)}×{abs(e.y - sy)}",
                                      state="normal")

    def _release(self, e):
        if not self.start:
            return
        self.mouse = (e.x_root, e.y_root)
        x0, x1 = sorted((self.start[2], e.x_root))
        y0, y1 = sorted((self.start[3], e.y_root))
        if x1 - x0 < 6 and y1 - y0 < 6:
            hit = self.navigator.hit_at(*self.mouse) if self.navigator else None
            if hit is None:
                with self._lock:
                    _, hit = self._cached_hover(*self.mouse)
            if hit is None:
                hit = self.hover_hit
            if hit and self._inside(hit["box"], e.x_root, e.y_root):
                self.finish(("word", e.x_root, e.y_root, hit))  # слово вже розпізнано — без повторного OCR
            else:
                self.finish(("point", e.x_root, e.y_root))
        else:
            self.finish(("rect", x0, y0, x1, y1))

    def finish(self, result):
        if self.done:
            return
        self.done = True
        with self._lock:
            self._req = None
            self._hover_cache.clear()
        if self._manager.raw_mode_cb is self._key_callback:
            self._manager.stop_raw_mode()
        frozen = self.frozen
        if getattr(self, "_after_id", None) is not None:
            try:
                self.win.after_cancel(self._after_id)
            except Exception:
                self.app.logger.exception('word_lookup.finish')
        try:
            _cancel_view_timers(self.win)
            self.win.destroy()
        except Exception:
            self.app.logger.exception('word_lookup.finish')
        platform._restore_foreground(self._prev_hwnd)
        self.on_done(result, frozen)


