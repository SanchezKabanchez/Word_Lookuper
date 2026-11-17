"""Desktop views with explicit settings, library, cache and provider services."""
from word_lookup_i18n import _ as tr_ui
import logging
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
import pyperclip
from word_lookup_utils import clamp
from word_lookup_theme import (ACTIONS, AUDIO_OK, CARD_CONTENT_OPTIONS, ENGINE_NAMES, FONT, KEY, LANG_EN, LANG_NAMES, MOUSE_BTN_NAMES, MOUSE_MOD_NAMES, THEMES, THEME_NAMES)
from word_lookup_input import pretty
from wordlookup.network.providers import DeeplError
import word_lookup_platform as platform

from word_lookup_models import as_event
from word_lookup_scrolling import register_panel
from word_lookup_translator_settings import TranslatorSettingsView
from word_lookup_library import LibraryViewMixin
from word_lookup_settings import PrivacySettingsMixin
from word_lookup_profiles_ui import GameProfilesViewMixin
from word_lookup_lexicon import lookup as word_metadata
from word_lookup_selection import _cancel_view_timers


class ScrolledPanel(tk.Frame):
    """Scroll the visible content under the pointer, including child controls."""

    def __init__(self, parent, width=560, height=500, bg=None):
        bg = bg or parent.cget("background")
        super().__init__(parent, bg=bg, width=width, height=height)
        self.pack_propagate(False)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg=bg, yscrollincrement=18)
        self._word_lookup_scroll_canvas = self.canvas
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = tk.Frame(self.canvas, bg=bg)
        self.item = self.canvas.create_window(0, 0, anchor="nw", window=self.content)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.content.bind("<Configure>", self._sync)
        self.canvas.bind("<Configure>", self._resize)
        self._wheel_router = register_panel(self)
        self.bind("<Destroy>", self._cleanup)

    def _sync(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize(self, event):
        self.canvas.itemconfigure(self.item, width=max(1, event.width))

    def _wheel(self, event):
        return self._wheel_router.route(event)

    def _cleanup(self, event):
        if event.widget is self:
            self._wheel_router.unregister(self)


def screen_workarea(root, anchor):
    """Монітор біля слова, включно з моніторами ліворуч від основного."""
    x, y = int((anchor[0] + anchor[2]) / 2), int((anchor[1] + anchor[3]) / 2)
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from ctypes import wintypes
            class MonitorInfo(ctypes.Structure):
                _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                            ("work", wintypes.RECT), ("flags", wintypes.DWORD)]
            user = ctypes.windll.user32
            user.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
            user.MonitorFromPoint.restype = wintypes.HANDLE
            user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
            monitor = user.MonitorFromPoint(wintypes.POINT(x, y), 2)
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            if user.GetMonitorInfoW(monitor, ctypes.byref(info)):
                r = info.work
                return r.left, r.top, r.right, r.bottom
        except Exception:
            logging.getLogger(__name__).exception('word_lookup.screen_workarea')
    try:
        import mss
        with mss.MSS() as sct:
            for m in sct.monitors[1:]:
                if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
                    return m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"]
    except Exception:
        logging.getLogger(__name__).exception('word_lookup.screen_workarea')
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def place_popup(anchor, width, height, area, toast=False):
    left, top, right, bottom = area
    width, height = min(width, right - left - 16), min(height, bottom - top - 16)
    cx = (anchor[0] + anchor[2]) / 2
    above = not toast and anchor[1] - height - 6 >= top + 8
    x = anchor[0] + 12 if toast else cx - width / 2
    y = anchor[1] + 20 if toast else anchor[1] - height - 6 if above else anchor[3] + 6
    return (int(clamp(x, left + 8, right - width - 8)),
            int(clamp(y, top + 8, bottom - height - 8)), int(width), int(height), above)


class Highlight:
    """Жовте підсвічування слова (як у macOS Look Up)."""

    def __init__(self, app, text, box):
        self.app = app
        color = self.app.settings.data["popup"]["highlight_color"]
        x0, y0, x1, y1 = box
        self.win = w = tk.Toplevel(app.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=color)
        h = max(12, y1 - y0)
        font = tkfont.Font(family=FONT, size=-int(h * 0.95))
        tk.Label(w, text=text, font=font, fg="#000000", bg=color, padx=5, pady=0).pack()
        w.update_idletasks()
        lw, lh = w.winfo_reqwidth(), w.winfo_reqheight()
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        px, py = cx - lw // 2, cy - lh // 2
        w.geometry(f"{lw}x{lh}+{px}+{py}")
        self.rect = (px, py, px + lw, py + lh)

    def close(self):
        try:
            self.win.destroy()
        except Exception:
            self.app.logger.exception('word_lookup.close')


class Popup:
    """Картка словника: заокруглена панель зі стрілкою до слова."""

    def __init__(self, app, card, anchor, expanded=(), toast=False, timeout=None, pinned=False):
        self.app, self.card, self.anchor, self.toast = app, card, anchor, toast
        self.expanded = set(expanded)
        self.pinned = pinned
        self._timer = None
        P = self.app.settings.data["popup"]
        self.th = th = THEMES.get(P["theme"], THEMES["light"])
        self.fs = int(clamp(P["font_size"], 9, 24))
        self.cw = int(clamp(P["width"], 300, 900))
        self.workarea = screen_workarea(app.root, anchor)
        self.cw = min(self.cw, self.workarea[2] - self.workarea[0] - 64)

        self.win = w = tk.Toplevel(app.root)
        w.withdraw()
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        self.rounded = False
        if sys.platform.startswith("win"):
            try:
                w.attributes("-transparentcolor", KEY)
                self.rounded = True
            except tk.TclError:
                self.app.logger.exception('word_lookup.__init__')
        base_bg = KEY if self.rounded else th["bg"]
        w.configure(bg=base_bg)
        alpha = clamp(P["opacity"], 0.4, 1.0)
        if alpha < 1.0:
            try:
                w.attributes("-alpha", alpha)
            except tk.TclError:
                self.app.logger.exception('word_lookup.__init__')

        self.canvas = tk.Canvas(w, bg=base_bg, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.viewport = ScrolledPanel(self.canvas, width=self.cw + 18, bg=th["bg"])
        scrollbar_style = f"Popup{P['theme']}.Vertical.TScrollbar"
        style = ttk.Style(w)
        style.configure(scrollbar_style, background=th["line"], troughcolor=th["bg"],
                        arrowcolor=th["dim"], bordercolor=th["bg"], lightcolor=th["line"],
                        darkcolor=th["line"], borderwidth=0, relief="flat")
        style.map(scrollbar_style, background=[("pressed", th["head"]), ("active", th["border"])],
                  arrowcolor=[("active", th["fg"]), ("pressed", th["fg"])])
        self.viewport.bar.configure(style=scrollbar_style)
        self.content = self.viewport.content
        tk.Frame(self.content, width=self.cw, height=0, bg=th["bg"]).pack()  # мін. ширина

        if not toast:
            if card.get("kind") != "word":
                self._toolbar()
            if getattr(app, "_game_pause", None) is not None and app._game_pause.active:
                self.lbl("Гра призупинена · Esc — закрити переклад і відновити гру",
                         size=-2, color=th["dim"], pady=(0, 6))
        if toast:
            self.lbl(card["text"], size=-1)
        elif card["kind"] == "word":
            self._build_word()
        else:
            self._build_text()

        self._layout()
        w.deiconify()
        w.attributes("-topmost", True)
        if timeout is None:
            timeout = 1.8 if toast else float(clamp(P["timeout"], 0, 300))
        self._timeout_ms = int(timeout * 1000)
        self._arm_timeout()

    def _toolbar(self):
        # Actions get their own row: a long headword must never compete with
        # buttons for horizontal space. Narrow/high-DPI cards use two rows.
        self.toolbar = row = tk.Frame(self.content, bg=self.th["bg"])
        row.pack(fill="x", pady=(0, 6))
        self.pin_btn = self._tool_button(row, "Відкріпити" if self.pinned else "Закріпити",
                                         self.toggle_pin)
        copy_btn = self._tool_button(row, "Копіювати", self.copy_translation)
        self.options_btn = self._tool_button(row, "Вигляд ▾", self._show_options)
        close_btn = self._tool_button(row, "✕", self.app.dismiss_lookup)
        self.toolbar_buttons = (self.pin_btn, copy_btn, self.options_btn, close_btn)
        self._toolbar_columns = None
        self._arrange_toolbar(self.cw)
        row.bind("<Configure>", lambda event: self._arrange_toolbar(event.width))
        self.win.bind("<Escape>", lambda _e: self.app.dismiss_lookup())
        self.win.bind("<Control-Return>", lambda _e: self.save_current())

    def _arrange_toolbar(self, width):
        sizes = [button.winfo_reqwidth() + 8 for button in self.toolbar_buttons]
        columns = 4 if sum(sizes) <= width else 2
        if columns == 2 and max(sum(sizes[:2]), sum(sizes[2:])) > width:
            columns = 1
        if columns == self._toolbar_columns:
            return
        self._toolbar_columns = columns
        for col in range(4):
            self.toolbar.columnconfigure(col, weight=1 if col < columns else 0)
        for index, button in enumerate(self.toolbar_buttons):
            button.grid(row=index // columns, column=index % columns,
                        sticky="e" if index == 3 else "w", padx=4, pady=1)

    def _tool_button(self, parent, text, command):
        return tk.Button(parent, text=text, command=command, font=(FONT, 9),
                         bg=self.th["bg"], fg=self.th["link"], relief="flat", bd=0, highlightthickness=1,
                         highlightbackground=self.th["bg"], highlightcolor=self.th["link"],
                         activebackground=self.th["line"], activeforeground=self.th["fg"],
                         padx=5, pady=3, cursor="hand2", takefocus=True)

    def _show_options(self):
        # Під час налаштування картка залишається відкритою, зокрема при кліку в меню.
        if not self.pinned:
            self.toggle_pin()
        menu = getattr(self, "options_menu", None)
        if menu is None:
            self.options_menu = menu = tk.Menu(self.win, tearoff=False, font=(FONT, 9))
            for path, label in CARD_CONTENT_OPTIONS:
                menu.add_checkbutton(label=label, variable=self.app._var(tk.BooleanVar, path,
                                                             after=self.app._refresh_preview))
            menu.add_separator()
            menu.add_command(label=tr_ui("Усі налаштування картки…"), command=self.app.open_card_settings)
        try:
            menu.tk_popup(self.options_btn.winfo_rootx(),
                          self.options_btn.winfo_rooty() + self.options_btn.winfo_height())
        finally:
            menu.grab_release()

    def copy_translation(self):
        text = self.card.get("translation") or self.app.library._card_primary_translation(self.card)
        if text:
            try:
                pyperclip.copy(text)
                self.app.toast("Переклад скопійовано")
            except Exception as e:
                self.app.toast(f"Буфер обміну недоступний: {e}")

    def save_current(self):
        if self.card.get("kind") in ("word", "text") and self.app.library._entry_complete({
                "translation": self.app.library._card_primary_translation(self.card),
                "definition": self.app.library._card_definition(self.card)}):
            self.app.library.add_saved_word(self.card)
            self.app.refresh_saved_count()
            self.app.rerender(self)

    def toggle_pin(self):
        self.pinned = not self.pinned
        self.pin_btn.configure(text="Відкріпити" if self.pinned else "Закріпити")
        self._arrange_toolbar(self.toolbar.winfo_width())
        self._arm_timeout()

    def _arm_timeout(self):
        if self._timer:
            self.win.after_cancel(self._timer)
            self._timer = None
        if self._timeout_ms > 0 and not self.pinned:
            self._timer = self.win.after(self._timeout_ms, self._on_timeout)

    # ---------- допоміжні віджети ----------
    def lbl(self, text, size=0, bold=False, italic=False, color=None, indent=0, pady=(0, 0)):
        style = " ".join(s for s, f in (("bold", bold), ("italic", italic)) if f)
        font = (FONT, self.fs + size) + ((style,) if style else ())
        lb = tk.Label(self.content, text=text, font=font, fg=color or self.th["fg"],
                      bg=self.th["bg"], justify="left", anchor="w",
                      wraplength=self.cw - indent)
        lb.pack(anchor="w", padx=(indent, 0), pady=pady)
        return lb

    def rich_text(self, parts, color=None, bold=False, size=0, indent=6, pady=(0, 0)):
        """Абзац із підсвіченими фрагментами (слово в реченні, його переклад).
        parts: [(текст, виділити?), ...]."""
        th = self.th
        parts = [(t, h) for t, h in parts if t]
        if not parts:
            return
        font = tkfont.Font(family=FONT, size=self.fs + size, weight="bold" if bold else "normal")
        char_px = max(1, font.measure("0"))
        chars = max(10, (self.cw - indent) // char_px)
        total_px = font.measure("".join(t for t, _ in parts))
        lines = max(1, int(total_px * 1.15 / (chars * char_px)) + 1)
        w = tk.Text(self.content, wrap="word", width=chars, height=lines, bd=0,
                    highlightthickness=0, padx=0, pady=0, bg=th["bg"], fg=color or th["fg"],
                    font=font, cursor="arrow", takefocus=0)
        w.tag_configure("hl", background=self.app.settings.data["popup"]["highlight_color"], foreground="#000000")
        for t, h in parts:
            w.insert("end", t, "hl" if h else "")
        w.config(state="disabled")
        w.pack(anchor="w", padx=(indent, 0), pady=pady)

    def section(self, title, first):
        th = self.th
        row = tk.Frame(self.content, bg=th["bg"])
        row.pack(fill="x", pady=(0 if first else 9, 3))
        tk.Label(row, text=title.upper(), font=(FONT, max(9, self.fs - 2)), fg=th["head"],
                 bg=th["bg"]).pack(side="left")

    def definition_text(self, text, translated=False):
        """Give the original and translated explanation distinct, readable roles."""
        row = tk.Frame(self.content, bg=self.th["bg"])
        row.pack(fill="x", padx=6, pady=(3 if translated else 1, 0))
        language = self.app.settings.data["languages"]["target"].upper() if translated else "EN"
        tag = tk.Label(row, text=language, font=(FONT, max(8, self.fs - 2), "bold"),
                               fg=self.th["link"] if translated else self.th["dim"],
                               bg=self.th["line"] if translated else self.th["bg"], padx=4, pady=2)
        tag.pack(side="left", anchor="n", padx=(0, 6))
        paragraph = tk.Label(row, text=text, font=(FONT, self.fs if translated else max(9, self.fs - 1)),
                                     fg=self.th["fg"] if translated else self.th["dim"], bg=self.th["bg"],
                                     justify="left", anchor="w", bd=0, padx=0,
                                     wraplength=max(1, self.cw - tag.winfo_reqwidth() - 18))
        paragraph.pack(side="left", fill="x", expand=True, anchor="n")
        row.bind("<Configure>", lambda event: paragraph.configure(
            wraplength=max(1, event.width - tag.winfo_reqwidth() - 6)))
        self.definition_blocks = getattr(self, "definition_blocks", [])
        self.definition_blocks.append((language, tag, paragraph))

    def chips(self, title, terms, muted=False):
        """Wrap vocabulary pills by their measured pixel sizes, including high DPI."""
        self.lbl(title, size=-1, color=self.th["head"], indent=6, pady=(6, 2))
        flow = tk.Frame(self.content, bg=self.th["bg"], width=max(1, self.cw - 12), height=1)
        flow.pack(fill="x", padx=6)
        labels = [tk.Label(flow, text=term, bg=self.th["line"],
                                    fg=self.th["dim"] if muted else self.th["link"],
                                    font=(FONT, max(9, self.fs - 1)), bd=0,
                                    padx=7, pady=3, justify="left", anchor="w") for term in terms]
        group = getattr(self, "antonym_chips" if muted else "synonym_chips", [])
        group.extend(labels)
        setattr(self, "antonym_chips" if muted else "synonym_chips", group)
        previous_width = None

        def arrange(event=None):
            nonlocal previous_width
            width = max(1, min(self.cw - 12, event.width if event else self.cw - 12))
            if width == previous_width:
                return
            previous_width = width
            x = y = row_height = 0
            for chip in labels:
                chip.configure(wraplength=max(1, width - 14))
                chip_width, chip_height = min(width, chip.winfo_reqwidth()), chip.winfo_reqheight()
                if x and x + chip_width > width:
                    x, y, row_height = 0, y + row_height + 5, 0
                chip.place(x=x, y=y, width=chip_width, height=chip_height)
                x += chip_width + 6
                row_height = max(row_height, chip_height)
            flow.configure(height=y + row_height)

        flow.bind("<Configure>", arrange)
        arrange()

    def headword(self, word, with_audio, show_phonetic=True, with_save=False):
        th = self.th
        self._toolbar()
        row = tk.Frame(self.content, bg=th["bg"])
        row.pack(fill="x", padx=(6, 0), pady=(0, 3))
        self.word_title = tk.Label(row, text=word, font=(FONT, self.fs + 8, "bold"),
                                   fg=th["fg"], bg=th["bg"], cursor="hand2", anchor="w",
                                   justify="left", wraplength=max(1, self.cw - 6), bd=0,
                                   padx=0, takefocus=True, highlightthickness=1,
                                   highlightbackground=th["bg"], highlightcolor=th["link"])
        self.word_title.pack(fill="x")
        self.word_title.bind("<Button-1>", self.edit_word)
        self.word_title.bind("<Return>", self.edit_word)
        self.word_title.bind("<space>", self.edit_word)
        metadata = word_metadata(word)
        if metadata.get("frequency_label"):
            self.frequency_badge = tk.Label(row, text=metadata["frequency_label"],
                font=(FONT, max(9, self.fs - 1)), fg=th["link"], bg=th["line"], padx=7, pady=2)
            self.frequency_badge.pack(anchor="w", pady=(3, 0))
        self.correction_row = tk.Frame(self.content, bg=th["bg"])
        self.word_var = tk.StringVar(master=self.win, value=word)
        self.word_editor = tk.Entry(self.correction_row, textvariable=self.word_var,
                                    font=(FONT, self.fs + 1), bg=th["line"], fg=th["fg"],
                                    insertbackground=th["link"], relief="flat", bd=6)
        self.word_editor.pack(side="left", fill="x", expand=True)
        self._tool_button(self.correction_row, "Знайти →", self.submit_word).pack(side="left", padx=5)
        self.word_editor.bind("<Return>", lambda _e: self.submit_word())
        self.word_editor.bind("<Escape>", self.cancel_word_edit)
        helper = tk.Frame(self.content, bg=th["bg"])
        helper.pack(fill="x", padx=6, pady=(0, 3))
        self._tool_button(helper, "Виправити слово", self.edit_word).pack(side="left")
        if with_audio and AUDIO_OK and self.app.settings.data["popup"]["btn_speak"]:
            for label, url in self.card.get("audios", []):
                self._tool_button(helper, f"🔊 {label}", lambda u=url: self.app.providers.play_audio(u)).pack(side="right")
        if show_phonetic and self.card.get("phonetic"):
            self.lbl(f"/{self.card['phonetic']}/", size=-1, color=th["dim"], indent=6)

    def edit_word(self, _event=None):
        if not self.pinned:
            self.toggle_pin()
        self.correction_row.pack(fill="x", padx=6, pady=(4, 8), after=self.word_title.master)
        self.word_editor.focus_force()
        self.word_editor.selection_range(0, "end")
        return "break"

    def cancel_word_edit(self, _event=None):
        if _event is not None:
            # The global pynput hook also observes this Escape. Consume its
            # queued event without dismissing the card after the Tk binding.
            self.app._word_edit_escape_until = time.monotonic() + 0.3
        self.correction_row.pack_forget()
        self.word_var.set(self.card["word"])
        return "break"

    def submit_word(self):
        word = self.word_var.get().strip()
        # One English word (including apostrophe/hyphen), never a second OCR pass.
        if not re.fullmatch(r"[A-Za-z]+(?:['’\-][A-Za-z]+)*", word) or len(word) > 80:
            self.app.toast("Введи одне англійське слово")
            self.word_editor.focus_force()
            return "break"
        self.app.retry_word(word, self.card, self.anchor)
        return "break"

    def save_button(self, row):
        """Кнопка ⭐ Зберегти / ✓ Збережено — праворуч у рядку слова."""
        word = self.card["word"]
        entry = self.app.library.saved_entry(word)
        saved = entry is not None and self.app.library._entry_complete(entry)
        btn = tk.Label(row, text=("✓ Збережено" if saved else "⭐ Зберегти"),
                       font=(FONT, self.fs - 2),
                       fg=(self.th["dim"] if saved else self.th["link"]),
                       bg=self.th["bg"], cursor=("arrow" if saved else "hand2"))
        btn.pack(side="right", padx=(6, 0))
        if not saved:
            def on_click(_e):
                if not (self.app.library._card_primary_translation(self.card) or self.app.library._card_definition(self.card)):
                    if self.card.get("dict_pending"):
                        self.app.toast("Переклад ще не завантажився — зачекай і спробуй ще раз")
                    else:
                        reason = self.card.get("note") or self.card.get("dict_note") or \
                                 "ні переклад, ні визначення не знайдені"
                        self.app.toast(f"Не вдалося зберегти «{self.card['word']}»: {reason}")
                    return
                if self.app.library.add_saved_word(self.card):
                    btn.config(text=tr_ui("✓ Збережено"), fg=self.th["dim"], cursor="arrow")
                    btn.unbind("<Button-1>")
                    app = self.app
                    if hasattr(app, "refresh_saved_count"):
                        app.refresh_saved_count()
            btn.bind("<Button-1>", on_click)

    def more(self, key):
        b = tk.Label(self.content, text=tr_ui("Показати ще"), font=(FONT, self.fs - 1, "bold"),
                     fg=self.th["link"], bg=self.th["bg"], cursor="hand2")
        b.pack(anchor="w", padx=(6, 0), pady=(3, 0))

        def expand(_e):
            self.expanded.add(key)
            self.app.rerender(self)
        b.bind("<Button-1>", expand)

    # ---------- вміст ----------
    def _build_text(self):
        tl = self.app.settings.data["languages"]["target"]
        self.section(f"English – {LANG_EN.get(tl, tl)}", True)
        row = tk.Frame(self.content, bg=self.th["bg"])
        row.pack(fill="x", pady=4)
        self.save_button(row)
        if self.app.settings.data["popup"]["sentence"]:
            self.lbl(self.card["original"], size=-1, color=self.th["dim"], indent=6)
        self.lbl(self.card["translation"], bold=True, indent=6, pady=(4, 0))
        if self.card.get("translator"):
            self.lbl("Перекладач: " + self.card["translator"], size=-1, color=self.th["dim"], indent=6, pady=(6, 0))
        self.context_photo = (self.app.library.context_photo(self.card.get("image_file", ""), self.win,
                                            size=(max(1, self.cw - 12), 220))
                              if self.app.settings.data["popup"]["context_image"] else None)
        if self.context_photo:
            tk.Label(self.content, image=self.context_photo, bg=self.th["bg"]).pack(anchor="w", padx=6)

    def _build_word(self):
        """Render the card in reading order; each section owns its preferences."""
        meanings = self.card.get("meanings", [])
        options = self.app.settings.data["dictionary"]
        max_def = int(clamp(options["max_definitions"], 1, 6))
        max_syn = int(clamp(options["max_synonyms"], 1, 20))
        self._build_word_heading(meanings)
        self._build_word_translation()
        self._build_word_context()
        self._build_word_definitions(meanings, max_def)
        self._build_word_thesaurus(meanings, max_def, max_syn)
        self._build_word_alternates(self.card.get("groups", []))
        self._build_word_footer()

    def _build_word_heading(self, meanings):
        c, th = self.card, self.th
        # 1) Слово, озвучення, ⭐ Зберегти
        self.headword(c["word"], with_audio=True, show_phonetic=False, with_save=False)

        # 3) Вимова і частина мови
        pos0 = meanings[0].get("pos", "") if meanings else ""
        bits = [b for b in ((f"/{c['phonetic']}/" if c.get("phonetic") and self.app.settings.data["popup"]["phonetic"] else ""), pos0) if b]
        if bits:
            self.lbl("  ·  ".join(bits), size=-1, color=th["dim"], indent=6, pady=(1, 0))


        if c.get("corrected_from"):
            self.lbl(f"Схоже, гра показала «{c['corrected_from']}» — знайдено найближче "
                     f"слово «{c.get('base', '')}»", size=-1, color=th["dim"], indent=6,
                     pady=(2, 0))
        elif c.get("base") and c["base"].lower() != c["word"].lower():
            self.lbl(f"Форма слова «{c['base']}»", size=-1, color=th["dim"], indent=6,
                     pady=(2, 0))

    def _build_word_context(self):
        c, th = self.card, self.th
        # речення з гри + його переклад (слово підсвічене в обох) — як у Translator
        sentence = c.get("sentence") or ""
        if sentence:
            if self.app.settings.data["popup"]["sentence"] or self.app.settings.data["popup"]["sentence_translation"]:
                self.section("Речення", False)
            if self.app.settings.data["popup"]["sentence"]:
                self.rich_text(self.app.library.split_around_word(sentence, c["word"]), color=th["dim"], size=-1,
                               pady=(6, 0))
            stl = c.get("sentence_tr") or ""
            if stl and self.app.settings.data["popup"]["sentence_translation"]:
                span = self.app.library.align_word_span(stl, self.app.library.primary_translations(c))
                parts = ([(stl[:span[0]], False), (stl[span[0]:span[1]], True), (stl[span[1]:], False)]
                         if span else [(stl, False)])
                self.rich_text(parts, pady=(2, 0))
            elif c.get("sentence_pending") and self.app.settings.data["popup"]["sentence_translation"]:
                self.lbl("Перекладаю речення…", size=-1, color=th["dim"], indent=6, pady=(2, 0))

    def _build_word_translation(self):
        c, S = self.card, self.app.settings.data["sections"]
        # The headline contains one primary translation. The optional alternatives
        # section keeps every additional sense available without crowding it.
        primary = self.app.library._card_primary_translation(c)
        if S["bilingual"] and primary:
            self.primary_translation = self.lbl(primary, size=3, bold=True, indent=6, pady=(8, 2))
        elif S["bilingual"] and c.get("note"):
            self.lbl("⚠ " + c["note"], color="#c2410c", indent=6, pady=(2, 0))

    def _build_word_definitions(self, meanings, max_def):
        c, S, th = self.card, self.app.settings.data["sections"], self.th
        # 4-5) Основне значення й приклад використання
        if S["definitions"] and meanings:
            self.section("Значення", False)
            exp = "def" in self.expanded
            shown = meanings if exp else meanings[:max_def]
            first_def = True
            for m in shown:
                self.lbl(m["pos"], size=-1, bold=True, color=th["head"], indent=6, pady=(8, 0))
                for d in (m["defs"][:3] if exp else m["defs"][:1]):
                    self.definition_text(d["d"])
                    if first_def:  # переклад першого визначення — одразу під ним
                        first_def = False
                        if c.get("definition_tr") and self.app.settings.data["popup"]["translate_definition"]:
                            self.definition_text(c["definition_tr"], translated=True)
                        elif c.get("def_tr_pending") and self.app.settings.data["popup"]["translate_definition"]:
                            self.lbl("…", color=th["dim"], indent=6)
                    if d["e"] and self.app.settings.data["popup"]["examples"]:
                        self.lbl(d["e"], italic=True, color=th["example"], indent=6)
            if not exp and (len(meanings) > max_def or any(len(m["defs"]) > 1 for m in shown)):
                self.more("def")
        elif (S["definitions"] or S["thesaurus"]) and c.get("dict_note"):
            self.lbl("⚠ " + c["dict_note"], color="#c2410c", indent=6, pady=(8, 0))
            self.lbl('Спробуй кнопку «Діагностика словника» на вкладці «Головна».',
                     size=-1, color=th["dim"], indent=6, pady=(2, 0))
        elif (S["definitions"] or S["thesaurus"]) and c.get("dict_pending"):
            self.lbl("Завантажую визначення…", color=th["dim"], indent=6, pady=(8, 0))

    def _build_word_thesaurus(self, meanings, max_def, max_syn):
        S = self.app.settings.data["sections"]
        # 6) Синоніми / антоніми
        thes = [m for m in meanings if m["syn"] or m["ant"]]
        if S["thesaurus"] and thes:
            exp = "thes" in self.expanded
            shown = thes if exp else thes[:max_def]
            for m in shown:
                syn = m["syn"][: (15 if exp else max_syn)]
                if syn:
                    self.chips("Синоніми", syn)
                if m["ant"]:
                    self.chips("Антоніми", m["ant"][: (10 if exp else 5)], muted=True)
            if not exp and (len(thes) > max_def or any(len(m["syn"]) > max_syn for m in shown)):
                self.more("thes")

    def _build_word_alternates(self, groups):
        S, th = self.app.settings.data["sections"], self.th
        # 7) Додаткові варіанти перекладу — унизу, другорядно
        primary = self.app.library._card_primary_translation(self.card).strip().casefold()
        alternatives = [(pos, [term for term in terms if term.strip().casefold() != primary])
                        for pos, terms in groups]
        alternatives = [(pos, terms) for pos, terms in alternatives if terms]
        if S["bilingual"] and alternatives and self.app.settings.data["popup"]["alternate_translations"]:
            self.lbl("Інші варіанти перекладу", size=-1, bold=True, color=th["head"],
                     indent=6, pady=(8, 2))
            for pos, terms in alternatives:
                label = f"{pos}: " if pos else ""
                self.lbl(label + ", ".join(terms), indent=6)

    def _build_word_footer(self):
        c, th = self.card, self.th
        if c.get("translator"):
            self.lbl("Перекладач: " + c["translator"], size=-1, color=th["dim"], indent=6, pady=(6, 0))
        self.context_photo = (self.app.library.context_photo(c.get("image_file", ""), self.win,
                                            size=(max(1, self.cw - 12), 220))
                              if self.app.settings.data["popup"]["context_image"] else None)
        if self.context_photo:
            self.lbl("Контекст із гри", size=-1, color=th["dim"], indent=6, pady=(8, 2))
            tk.Label(self.content, image=self.context_photo, bg=th["bg"]).pack(anchor="w", padx=6)

        footer = tk.Frame(self.content, bg=th["bg"])
        footer.pack(fill="x", padx=6, pady=(12, 2))
        tk.Label(footer, text=c.get("source", ""), font=(FONT, max(9, self.fs - 1)),
                 fg=th["dim"], bg=th["bg"], wraplength=max(100, self.cw - 185),
                 justify="left").pack(side="left")
        saved = self.app.library.saved_entry(c["word"])
        tk.Button(footer, text="✓ Збережено" if saved and self.app.library._entry_complete(saved) else "Зберегти слово",
                  command=self.save_current, bg="#ffd43b", fg="#17191e", activebackground="#ffe37a",
                  relief="flat", bd=0, highlightthickness=1, highlightbackground=self.th["bg"],
                  highlightcolor=self.th["link"], takefocus=True, padx=14, pady=7, cursor="hand2",
                  font=(FONT, self.fs, "bold")).pack(side="right")

    # ---------- розмітка й позиціювання ----------
    def _layout(self):
        c, th = self.canvas, self.th
        self.content.update_idletasks()
        cw = self.cw + 18
        ch = self.content.winfo_reqheight()
        pad = 14
        R = 14 if self.rounded else 0
        AH = 10 if (self.rounded and not self.toast) else 0
        max_height = self.workarea[3] - self.workarea[1] - 16
        ch = min(ch, max_height - 2 * pad - AH)
        self.viewport.configure(width=cw, height=ch)
        PW, PH = cw + 2 * pad, ch + 2 * pad
        W_, H_ = PW, PH + AH
        ax0, ay0, ax1, ay1 = self.anchor
        cx = (ax0 + ax1) / 2

        x, y, W_, H_, above = place_popup(self.anchor, W_, H_, self.workarea, self.toast)

        c.config(width=W_, height=H_)
        top = 0 if above else AH
        if self.rounded:
            ax = clamp(cx - x, R + AH + 4, PW - R - AH - 4)

            def rrect(x1, y1, x2, y2, r, fill):
                pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
                       x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
                c.create_polygon(pts, smooth=True, fill=fill, outline="")

            rrect(0, top, PW, top + PH, R, th["border"])
            rrect(1, top + 1, PW - 1, top + PH - 1, R - 1, th["bg"])
            if AH:
                if above:
                    c.create_polygon(ax - AH, top + PH - 1, ax + AH, top + PH - 1,
                                     ax, top + PH + AH, fill=th["border"], outline="")
                    c.create_polygon(ax - AH + 1, top + PH - 2, ax + AH - 1, top + PH - 2,
                                     ax, top + PH + AH - 2, fill=th["bg"], outline="")
                else:
                    c.create_polygon(ax - AH, top + 1, ax + AH, top + 1,
                                     ax, 0, fill=th["border"], outline="")
                    c.create_polygon(ax - AH + 1, top + 2, ax + AH - 1, top + 2,
                                     ax, 2, fill=th["bg"], outline="")
        else:
            c.create_rectangle(0, 0, W_ - 1, H_ - 1, fill=th["bg"], outline=th["border"])
        c.create_window(pad, top + pad, window=self.viewport, anchor="nw")
        # '+-N' означає абсолютну від'ємну координату, '-N' — відступ від правого краю.
        self.win.geometry(f"{W_}x{H_}+{x}+{y}")

    def contains(self, x, y) -> bool:
        try:
            w = self.win
            return (w.winfo_rootx() <= x <= w.winfo_rootx() + w.winfo_width()
                    and w.winfo_rooty() <= y <= w.winfo_rooty() + w.winfo_height())
        except Exception:
            return False

    def _on_timeout(self):
        self._timer = None
        if not self.toast and (self.pinned or self.contains(*self.win.winfo_pointerxy())):
            self._arm_timeout()
            return
        if self.toast:
            self.close()
        else:
            self.app.dismiss_lookup()

    def close(self):
        try:
            if self._timer:
                self.win.after_cancel(self._timer)
        except Exception:
            self.app.logger.exception('word_lookup.close')
        try:
            self.win.destroy()
        except Exception:
            self.app.logger.exception('word_lookup.close')


class FreeCursorOverlay:
    """Приціл для ігор, де миша захоплена грою (raw input) і системний курсор не
    рухається (напр. Cyberpunk 2077). Керування йде ЗЗОВНІ (через App): стрілки/WASD —
    з глобального хуку клавіатури, рух — від фізичної миші (метод nudge). Це вікно
    саме по собі НЕ приймає клавіші — звичайні Tk-прив'язки клавіш працюють лише
    коли вікно у фокусі, а повноекранні ігри фокус не віддають, тому раніше приціл
    «не рухався». Глобальний хук працює незалежно від фокусу."""

    STEP, STEP_FAST = 8, 40

    def __init__(self, app, start_pos, on_done):
        self.app = app
        self.on_done, self.done = on_done, False
        self.profile = dict(getattr(app, "_lookup_profile", {}) or {})
        self.frozen = None
        self._prev_hwnd = platform._foreground_window()  # повернути фокус грі після закриття
        r = app.root
        try:
            x, y = r.winfo_vrootx(), r.winfo_vrooty()
            w, h = r.winfo_vrootwidth(), r.winfo_vrootheight()
            if w < 100 or h < 100:
                raise ValueError
        except Exception:
            x, y, w, h = 0, 0, r.winfo_screenwidth(), r.winfo_screenheight()
        self.ox, self.oy, self.w, self.h = x, y, w, h
        sx, sy = start_pos
        self.x = min(max(sx, x), x + w - 1)
        self.y = min(max(sy, y), y + h - 1)

        try:
            image, sx, sy, *_ = self.app.ocr._grab(x, y, x + w, y + h)
            self.frozen = (image, sx, sy)
        except Exception:
            app._resume_game()
            raise
        app._after_capture_pause()

        self.win = win = tk.Toplevel(r)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        self.rounded = False
        if sys.platform.startswith("win"):
            try:
                win.attributes("-transparentcolor", KEY)  # майже невидиме вікно — видно лише приціл
                self.rounded = True
            except tk.TclError:
                self.app.logger.exception('word_lookup.__init__')
        bg = KEY if self.rounded else "black"
        if not self.rounded and self.frozen is None:
            try:
                win.attributes("-alpha", 0.05)
            except tk.TclError:
                self.app.logger.exception('word_lookup.__init__')
        win.configure(bg=bg)
        win.geometry(f"{w}x{h}+{x}+{y}")
        c = self.canvas = tk.Canvas(win, bg=bg, highlightthickness=0)
        c.pack(fill="both", expand=True)
        from PIL import ImageTk
        self._photo = ImageTk.PhotoImage(self.frozen[0], master=win)
        c.create_image(0, 0, image=self._photo, anchor="nw")

        self.hint_plate = c.create_rectangle(0, 0, 0, 0, fill="#171a22", outline="#343a48", width=1)
        self.hint = c.create_text(w // 2, 30, fill="#f1f1f5", font=(FONT, 13, "bold"),
                      width=max(1, w - 64), anchor="n", justify="center",
                      text=tr_ui("Рухай мишею або стрілками/WASD · Enter/Пробіл — шукати слово · "
                           "Esc — скасувати"))
        self._layout_hint()
        c.bind("<Configure>", lambda event: self._layout_hint(event.width))
        self.h1 = c.create_line(0, 0, 0, 0, fill="#ffd43b", width=2)
        self.h2 = c.create_line(0, 0, 0, 0, fill="#ffd43b", width=2)
        self.v1 = c.create_line(0, 0, 0, 0, fill="#ffd43b", width=2)
        self.v2 = c.create_line(0, 0, 0, 0, fill="#ffd43b", width=2)
        self.ring = c.create_oval(0, 0, 0, 0, outline="#ffd43b", width=2)
        self._redraw()
        # необов'язковий бонус: якщо вікно все ж отримає фокус (віконний режим гри),
        # Esc спрацює і напряму — але керування НЕ залежить від цього
        win.bind("<Escape>", lambda _e: self.finish(None))
        win.focus_force()
        c.focus_set()
        platform.release_cursor_clip()  # інакше миша «застрягає» в центрі й нічого не рухає
        win.after(100, self._unclip_tick)

    def _layout_hint(self, width=None):
        width = self.w if width is None else width
        self.canvas.itemconfigure(self.hint, width=max(1, width - 64))
        self.canvas.coords(self.hint, width // 2, 24)
        bounds = self.canvas.bbox(self.hint)
        if bounds:
            x0, y0, x1, y1 = bounds
            self.canvas.coords(self.hint_plate, x0 - 16, y0 - 10, x1 + 16, y1 + 10)
            self.canvas.tag_raise(self.hint_plate)
            self.canvas.tag_raise(self.hint)

    def _unclip_tick(self):
        if self.done:
            return
        platform.release_cursor_clip()
        try:
            self.win.after(100, self._unclip_tick)
        except Exception:
            self.app.logger.exception('word_lookup._unclip_tick')

    def _redraw(self):
        x, y, R, gap = self.x, self.y, 14, 5
        self.canvas.coords(self.h1, x - R, y, x - gap, y)
        self.canvas.coords(self.h2, x + gap, y, x + R, y)
        self.canvas.coords(self.v1, x, y - R, x, y - gap)
        self.canvas.coords(self.v2, x, y + gap, x, y + R)
        self.canvas.coords(self.ring, x - R, y - R, x + R, y + R)

    def nudge(self, dx, dy):
        """dx, dy — уже готове зміщення в пікселях (від фізичної миші або клавіші)."""
        if self.done:
            return
        self.x = min(max(self.x + dx, self.ox), self.ox + self.w - 1)
        self.y = min(max(self.y + dy, self.oy), self.oy + self.h - 1)
        self._redraw()

    def key(self, name, fast):
        """Викликає App при отриманні клавіші з глобального хуку (незалежно від фокусу)."""
        step = self.STEP_FAST if fast else self.STEP
        if name in ("up", "w"):
            self.nudge(0, -step)
        elif name in ("down", "s"):
            self.nudge(0, step)
        elif name in ("left", "a"):
            self.nudge(-step, 0)
        elif name in ("right", "d"):
            self.nudge(step, 0)
        elif name in ("enter", "space"):
            self.finish((self.x, self.y))
        elif name == "esc":
            self.finish(None)

    def finish(self, result):
        if self.done:
            return
        self.done = True
        try:
            _cancel_view_timers(self.win)
            self.win.destroy()
        except Exception:
            self.app.logger.exception('word_lookup.finish')
        platform._restore_foreground(self._prev_hwnd)
        self.on_done(result, self.frozen)


class ReviewWindow:
    """Спочатку згадати, потім перевірити; складні слова — ще раз. Без мережі."""

    def __init__(self, app, entries):
        self.app, self.pending = app, list(entries)
        self.total, self.completed, self.revealed = len(entries), 0, False
        self.win = tk.Toplevel(app.root)
        self.win.title(tr_ui("Повторення слів — Word Lookup"))
        self.win.configure(bg="#111318")
        self.win.geometry("680x660")
        self.win.minsize(540, 380)
        self.progress = tk.StringVar()
        ttk.Label(self.win, textvariable=self.progress, padding=12).pack(fill="x")
        self.mode = tk.StringVar(value="Англійська → Переклад")
        mode_row = ttk.Frame(self.win, padding=(12, 0, 12, 8))
        mode_row.pack(fill="x")
        mode_box = ttk.Combobox(mode_row, textvariable=self.mode, state="readonly", width=32,
                                values=["Англійська → Переклад", "Переклад → Англійська", "Пропуск у реченні"])
        mode_box.pack(side="left")
        mode_box.bind("<<ComboboxSelected>>", lambda _e: self.next_card())
        self.panel = ScrolledPanel(self.win)
        self.panel.pack(fill="both", expand=True, padx=12)
        self.body = self.panel.content
        buttons = ttk.Frame(self.win, padding=12)
        buttons.pack(fill="x")
        self.reveal_btn = ttk.Button(buttons, text=tr_ui("Показати відповідь (Пробіл)"), style="Accent.TButton", command=self.reveal)
        self.reveal_btn.pack(fill="x", pady=(0, 8))
        self.again_btn = ttk.Button(buttons, text=tr_ui("1 · Ще раз"), command=lambda: self.grade(False))
        self.again_btn.pack(side="left", fill="x", expand=True)
        self.known_btn = ttk.Button(buttons, text=tr_ui("2 · Знаю"), command=lambda: self.grade(True))
        self.known_btn.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.win.bind("<space>", lambda _e: self.reveal())
        self.win.bind("<Key-1>", lambda _e: self.grade(False))
        self.win.bind("<Key-2>", lambda _e: self.grade(True))
        self.win.bind("<Escape>", lambda _e: self.win.destroy())
        self.next_card()

    def _label(self, text, font=None):
        font = font or (FONT, 12)
        ttk.Label(self.body, text=text, font=font, wraplength=550, justify="left"
                  ).pack(anchor="w", padx=12, pady=8)

    def next_card(self):
        for child in self.body.winfo_children():
            child.destroy()
        self.panel.canvas.yview_moveto(0)
        self.revealed = False
        self.again_btn.state(["disabled"])
        self.known_btn.state(["disabled"])
        self.reveal_btn.state(["!disabled"] if self.pending else ["disabled"])
        self.progress.set(f"Згадано: {self.completed} / {self.total} · Залишилось: {len(self.pending)}")
        if not self.pending:
            self._label("Повторення завершено!", font=(FONT, 22, "bold"))
            self._label("Усі слова цього набору пройдено. Можна повернутися до гри.")
            return
        entry = self.pending[0]
        prompt = entry["word"]
        if self.mode.get() == "Переклад → Англійська":
            prompt = entry.get("translation") or prompt
        elif self.mode.get() == "Пропуск у реченні":
            sentence = entry.get("context") or entry.get("example") or ""
            pattern = r"(?<!\w)(?:" + "|".join(re.escape(t) for t in sorted(
                {entry["word"], entry.get("base") or entry["word"]}, key=len, reverse=True)) + r")(?!\w)"
            prompt, count = re.subn(pattern, "______", sentence, flags=re.I)
            if not count:
                prompt = entry["word"]
        self._label(prompt, font=(FONT, 22, "bold"))
        self._label("Згадай переклад, потім відкрий відповідь.")

    def reveal(self):
        if not self.pending or self.revealed:
            return "break"
        self.revealed = True
        e = self.pending[0]
        if self.mode.get() != "Англійська → Переклад":
            self._label(e["word"], font=(FONT, 20, "bold"))
        self._label(e.get("translation", "") or "Переклад не збережено", font=(FONT, 18, "bold"))
        for key in ("definition", "definition_tr", "example", "context", "context_tr"):
            if e.get(key):
                self._label(e[key])
        self.photo = self.app.library.context_photo(e.get("image_file", ""), self.win)
        if self.photo:
            ttk.Label(self.body, image=self.photo).pack(padx=12, pady=8)
        self.again_btn.state(["!disabled"])
        self.known_btn.state(["!disabled"])
        self.reveal_btn.state(["disabled"])
        return "break"

    def grade(self, known):
        if not self.pending or not self.revealed:
            return "break"
        e = self.pending[0]
        if self.app.library.saved_entry(e["word"]) is None:
            self._label("Цю картку видалено з бібліотеки.")
            self.pending.pop(0)
            self.next_card()
            return "break"
        try:
            self.app.library.record_review(self.app.library.saved_entry(e["word"]), known)
        except OSError as ex:
            self._label(str(ex))
            return "break"
        self.pending.pop(0)
        if known:
            self.completed += 1
        else:
            self.pending.append(e)
        self.next_card()
        self.app.refresh_saved_count()
        return "break"


class AppViewMixin(PrivacySettingsMixin, GameProfilesViewMixin, LibraryViewMixin):
    """Settings, library/editor and appearance views for the application controller."""

    def _schedule_config_save(self):
        if self._config_save_timer is not None:
            self.root.after_cancel(self._config_save_timer)
        self._config_save_timer = self.root.after(300, self._flush_config_save)

    def _flush_config_save(self):
        if self._config_save_timer is not None:
            self.root.after_cancel(self._config_save_timer)
            self._config_save_timer = None
            saved = self.settings.save_config(self.settings.data)
            if hasattr(self, "deepl_var") and (self.settings.data["deepl"]["key"] or self.settings.credential_status):
                self.deepl_var.set(self.settings.credential_status if saved else
                                   "Не вдалося зберегти налаштування. Дивись журнал.")

    def _var(self, cls, path, mapping=None, after=None):
        path = tuple(path)
        if path in self._setting_vars:
            return self._setting_vars[path]
        inv = {v: k for k, v in mapping.items()} if mapping else None
        init = self.settings.get(path)
        var = cls(value=inv.get(init, init) if inv else init)

        def cb(*_):
            try:
                val = var.get()
            except tk.TclError:
                return
            if mapping:
                if val not in mapping:
                    return
                val = mapping[val]
            self.settings.put(path, val)
            self._schedule_config_save()
            if after:
                after()
        var.trace_add("write", cb)
        self._setting_vars[path] = var
        return var

    def _combo(self, parent, var, mapping, width=26):
        return ttk.Combobox(parent, textvariable=var, values=list(mapping),
                            state="readonly", width=width)

    def _apply_style(self):
        bg, panel, field, fg, dim, accent = "#111318", "#1d2029", "#272b36", "#f1f1f5", "#a2a8b8", "#ffd43b"
        self.root.configure(bg=bg)
        self.root.option_add("*TCombobox*Listbox.background", field)
        self.root.option_add("*TCombobox*Listbox.foreground", fg)
        self.root.option_add("*TCombobox*Listbox.selectBackground", accent)
        self.root.option_add("*TCombobox*Listbox.selectForeground", bg)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=(FONT, 10), background=bg, foreground=fg,
                        troughcolor=panel, bordercolor=field, lightcolor=field, darkcolor=field)
        style.configure("TButton", background=field, bordercolor=field, relief="flat",
                        padding=(12, 8), focusthickness=1, focuscolor=accent)
        style.map("TButton", background=[("active", "#353a48"), ("pressed", "#41485a")],
                  foreground=[("disabled", "#73798a")])
        style.configure("Accent.TButton", foreground=bg, background=accent, font=(FONT, 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#ffe37a"), ("pressed", "#e8bb26")],
                  foreground=[("disabled", "#73798a")])
        style.configure("Sidebar.TFrame", background=panel)
        style.configure("Sidebar.TLabel", background=panel, foreground=dim)
        style.configure("Sidebar.TButton", background=panel, foreground=fg, anchor="w", padding=(14, 12))
        style.map("Sidebar.TButton", background=[("active", field), ("pressed", field)])
        style.configure("Sidebar.TNotebook", borderwidth=0, background=bg)
        style.layout("Sidebar.TNotebook.Tab", [])
        style.configure("TNotebook", borderwidth=0, background=bg)
        style.configure("TNotebook.Tab", padding=(15, 8), background=panel)
        style.map("TNotebook.Tab", background=[("selected", field)], foreground=[("selected", accent)])
        style.configure("TLabelframe", bordercolor=field, relief="solid")
        style.configure("TLabelframe.Label", font=(FONT, 10, "bold"), foreground=dim)
        style.configure("TEntry", fieldbackground=field, foreground=fg, insertcolor=accent, padding=7)
        style.configure("TCombobox", fieldbackground=field, foreground=fg, arrowcolor=dim, padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", field)], foreground=[("readonly", fg)])
        style.configure("TSpinbox", fieldbackground=field, foreground=fg, arrowcolor=dim, padding=5)
        style.configure("TCheckbutton", background=bg, foreground=fg, padding=3)
        style.map("TCheckbutton", background=[("active", bg)], indicatorcolor=[("selected", accent)])
        style.configure("Filter.TRadiobutton", background=panel, foreground=dim, padding=(9, 6))
        style.map("Filter.TRadiobutton", foreground=[("selected", accent)], background=[("active", field)])
        style.configure("Treeview", rowheight=48, fieldbackground=panel, background=panel,
                        foreground=fg, borderwidth=0, font=(FONT, 11))
        style.configure("Treeview.Heading", font=(FONT, 9), foreground=dim, background=panel, padding=8)
        style.map("Treeview", background=[("selected", "#303644")], foreground=[("selected", fg)])
        style.configure("Vertical.TScrollbar", background=field, troughcolor=bg, arrowcolor=dim, borderwidth=0)

    def _build_ui(self):
        self.root.geometry(f"1060x{min(790, self.root.winfo_screenheight() - 60)}")
        self.root.minsize(860, 540)
        shell = ttk.Frame(self.root)
        shell.pack(fill="both", expand=True)
        sidebar = ttk.Frame(shell, style="Sidebar.TFrame", width=225, padding=(18, 24))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        ttk.Label(sidebar, text="Word Lookup", font=(FONT, 16, "bold"),
                  style="Sidebar.TLabel", foreground="#f1f1f5").pack(anchor="w")
        ttk.Label(sidebar, text=tr_ui("Слова з твоїх світів"), style="Sidebar.TLabel").pack(anchor="w", pady=(5, 24))
        ttk.Button(sidebar, text=tr_ui("Бібліотека →"), style="Accent.TButton",
                   command=self.open_saved_words).pack(fill="x", pady=(0, 22))
        ttk.Label(sidebar, text=tr_ui("НАЛАШТУВАННЯ"), style="Sidebar.TLabel", font=(FONT, 9)).pack(anchor="w", pady=(0, 8))
        body = ttk.Frame(shell, padding=(26, 22, 20, 16))
        body.pack(side="left", fill="both", expand=True)
        heading = ttk.Frame(body)
        heading.pack(fill="x", pady=(0, 14))
        self.page_title = tk.StringVar(value="Головна")
        ttk.Label(heading, textvariable=self.page_title, font=(FONT, 22, "bold")).pack(side="left")
        ttk.Button(heading, text=tr_ui("Показати картку"), command=self.show_preview).pack(side="right")
        self.notebook = nb = ttk.Notebook(body, style="Sidebar.TNotebook")
        nb.pack(fill="both", expand=True)
        tabs = {}
        for key, title in (("main", "Головна"), ("keys", "Клавіші та миша"),
                           ("look", "Картка перекладу"), ("dict", "Словник і OCR")):
            panel = ScrolledPanel(nb, bg=self.root.cget("background"))
            f = ttk.Frame(panel.content, padding=(0, 0, 14, 20))
            f.pack(fill="both", expand=True)
            nb.add(panel, text=title)
            tabs[key] = f
            ttk.Button(sidebar, text=title, style="Sidebar.TButton",
                       command=lambda p=panel: nb.select(p)).pack(fill="x", pady=2)
            if key == "look":
                self.look_panel = panel
        nb.bind("<<NotebookTabChanged>>", lambda _e: self.page_title.set(nb.tab(nb.select(), "text")))
        ttk.Label(sidebar, text=tr_ui("Перекладай. Зберігай.\nПовертайся до слів."),
                  style="Sidebar.TLabel", justify="left").pack(side="bottom", anchor="w", pady=(20, 0))
        self._tab_main(tabs["main"])
        self._tab_keys(tabs["keys"])
        self._tab_look(tabs["look"])
        self._tab_dict(tabs["dict"])

    def _tab_main(self, f):
        self._build_main_status(f)
        ocr = self._build_ocr_controls(f)
        self._build_log_controls(f)
        self._build_dictionary_diagnostics(f)
        self._build_test_controls(f, before=ocr)
        self._build_freeze_controls(f)
        self._build_pause_controls(f)
        self._build_profiles_settings(f)
        self._build_free_cursor_controls(f)
        self._build_library_controls(f)
        self._build_cache_controls(f)
        self._build_app_controls(f)

    def _build_main_status(self, f):
        top = ttk.Frame(f)
        top.pack(fill="x")
        self.status_var = tk.StringVar()
        ttk.Label(top, textvariable=self.status_var, font=(FONT, 11, "bold")).pack(side="left")
        ttk.Button(top, text=tr_ui("Пауза / Продовжити"), command=self.toggle_pause).pack(side="right")

        self.hint_var = tk.StringVar()
        ttk.Label(f, textvariable=self.hint_var, wraplength=560, justify="left").pack(
            anchor="w", pady=(12, 0))

    def _build_ocr_controls(self, f):
        ocr = ttk.LabelFrame(f, text=tr_ui("Розпізнавання тексту (OCR)"), padding=10)
        ocr.pack(fill="x", pady=(12, 0))
        self.ocr_var = tk.StringVar()
        ttk.Label(ocr, textvariable=self.ocr_var, wraplength=540, justify="left").pack(anchor="w")
        brow = ttk.Frame(ocr)
        brow.pack(anchor="w", pady=(6, 0))
        ttk.Button(brow, text=tr_ui("Перевірити ще раз"), command=self._check_ocr).pack(side="left")
        ttk.Button(brow, text=tr_ui("Діагностика OCR (3 с)"), command=self.diagnose_ocr
                   ).pack(side="left", padx=(8, 0))
        ttk.Label(ocr, text=tr_ui("Діагностика: натисни, наведи курсор на текст — за 3 секунди буде "
                            "знімок і звіт, що саме бачить OCR."), foreground="#a2a8b8",
                  wraplength=540).pack(anchor="w", pady=(4, 0))
        return ocr

    def _build_log_controls(self, f):
        lg = ttk.LabelFrame(f, text=tr_ui("Журнал (щоб розібратись, що пішло не так)"), padding=10)
        lg.pack(fill="x", pady=(12, 0))
        lrow = ttk.Frame(lg)
        lrow.pack(anchor="w")
        ttk.Button(lrow, text=tr_ui("📋 Скопіювати журнал"), command=self.copy_log).pack(side="left")
        ttk.Button(lrow, text=tr_ui("Відкрити файл журналу"), command=self.open_log
                   ).pack(side="left", padx=(8, 0))
        ttk.Button(lrow, text=tr_ui("Очистити журнал"), command=self.clear_log).pack(side="left", padx=(8, 0))
        ttk.Label(lg, text=tr_ui("Журнал містить шукані слова й частини тексту. Переглянь звіт перед "
                           "надсиланням. Ключ DeepL приховано; файл обмежений ~600 КБ."),
                  foreground="#a2a8b8", wraplength=540).pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(lg, text=tr_ui("Записувати часову шкалу OCR, паузи та ресурсів для діагностики"),
            variable=self._var(tk.BooleanVar, ("diagnostics", "enabled"))).pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(lg, text=tr_ui("Додати останній OCR-фрагмент екрана з рамками слів (може містити приватний текст)"),
            variable=self._var(tk.BooleanVar, ("diagnostics", "include_ocr_image"),
                after=lambda: self.diagnostics.discard_image()
                    if not self.settings.data["diagnostics"]["include_ocr_image"] else None)).pack(anchor="w", pady=4)
        ttk.Button(lg, text=tr_ui("Зберегти діагностичний пакет…"), command=self.save_diagnostic_package
                           ).pack(anchor="w", pady=4)

    def _build_dictionary_diagnostics(self, f):
        dic = ttk.LabelFrame(f, text=tr_ui("Переклад і словник (English – визначення, синоніми)"),
                             padding=10)
        dic.pack(fill="x", pady=(12, 0))
        self.dict_var = tk.StringVar(value="Ще не перевірялось.")
        ttk.Label(dic, textvariable=self.dict_var, wraplength=540, justify="left").pack(anchor="w")
        ttk.Button(dic, text=tr_ui("Діагностика словника"), command=self.diagnose_dict).pack(
            anchor="w", pady=(6, 0))
        ttk.Label(dic, text=tr_ui("Якщо в грі показується лише переклад без визначень і синонімів "
                            "(секцій English / English Thesaurus), ця перевірка покаже, чи "
                            "доступний dictionaryapi.dev з твоєї мережі."), foreground="#a2a8b8",
                  wraplength=540).pack(anchor="w", pady=(4, 0))

    def _build_test_controls(self, f, before):
        test = ttk.LabelFrame(f, text=tr_ui("Спробувати"), padding=10)
        test.pack(fill="x", pady=(12, 0), before=before)
        self.test_var = tk.StringVar(value="understatement")
        self.test_entry = ttk.Entry(test, textvariable=self.test_var)
        self.test_entry.pack(side="left", fill="x", expand=True)
        self.test_entry.bind("<Return>", lambda _e: self.test_lookup())
        ttk.Button(test, text=tr_ui("Знайти"), command=self.test_lookup).pack(side="left", padx=(8, 0))
        self.preview_btn = ttk.Button(test, text=tr_ui("Показати приклад"), command=self.show_preview)
        self.preview_btn.pack(side="left", padx=(8, 0))

    def _build_freeze_controls(self, f):
        ttk.Button(f, text=tr_ui("✂  Виділити область на екрані (як скріншот)"),
                   command=self.start_region_select).pack(anchor="w", pady=(12, 0))
        ttk.Label(f, text=tr_ui("Екран одразу заморожується на місці — тому підказка (тултіп), яка "
                        "в грі зникає при русі миші, залишиться на скріншоті, поки ти спокійно "
                        "виділяєш потрібне слово."), foreground="#a2a8b8", wraplength=520
                  ).pack(anchor="w", pady=(2, 0))
        ttk.Checkbutton(f, text=tr_ui("Підсвічувати слово під курсором на застиглому екрані (клік — шукати)"),
                        variable=self._var(tk.BooleanVar, ("freeze", "hover_highlight"))
                        ).pack(anchor="w", pady=(6, 0))
        mode_row = ttk.Frame(f)
        mode_row.pack(fill="x", pady=(8, 0))
        ttk.Label(mode_row, text=tr_ui("Вибір на стоп-кадрі:")).pack(side="left")
        modes = {"Клавіатура": "keyboard", "Наведення мишею": "hover"}
        self._combo(mode_row, self._var(tk.StringVar, ("freeze", "selection_mode"), modes), modes, 22).pack(side="left", padx=8)
        ttk.Label(f, text=tr_ui("Ctrl+Alt+R → ←/→ слова, ↑/↓ рядки, Enter — картка, Esc — закрити."),
                          wraplength=520).pack(anchor="w", pady=(4, 0))

    def _build_pause_controls(self, f):
        pause_box = ttk.LabelFrame(f, text=tr_ui("Пауза гри під час пошуку (Windows)"), padding=10)
        pause_box.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(pause_box, text=tr_ui("Призупиняти активну гру при OCR-пошуку"),
                        variable=self._var(tk.BooleanVar, ("freeze", "pause_game"),
                                           after=self._pause_option_changed)).pack(anchor="w")
        mode_row = ttk.Frame(pause_box)
        mode_row.pack(fill="x", pady=(6, 0))
        ttk.Label(mode_row, text=tr_ui("Тривалість:")).pack(side="left")
        pause_modes = {"До закриття картки / Esc (з лімітом)": "lookup",
                       "Лише для захоплення кадру": "snapshot"}
        self._combo(mode_row, self._var(tk.StringVar, ("freeze", "pause_mode"), pause_modes),
                    pause_modes, 37).pack(side="left", padx=8)
        ttk.Checkbutton(pause_box, text=tr_ui("Автоматично визначати активну гру"),
                        variable=self._var(tk.BooleanVar, ("freeze", "auto_game"),
                                           after=self._game_mode_changed)).pack(anchor="w", pady=(4, 0))
        process_row = ttk.Frame(pause_box)
        process_row.pack(fill="x", pady=6)
        ttk.Label(process_row, text=tr_ui("Вручну (.exe):")).pack(side="left")
        self.game_exe_entry = ttk.Entry(process_row,
            textvariable=self._var(tk.StringVar, ("freeze", "game_exe")), width=28)
        self.game_exe_entry.pack(side="left", padx=6)
        self.game_detection_var = tk.StringVar(value="Гра визначається при натисканні гарячої клавіші у її вікні.")
        ttk.Label(pause_box, textvariable=self.game_detection_var, wraplength=520).pack(anchor="w", pady=4)
        ttk.Button(pause_box, text=tr_ui("Перевірити визначення через 3 с"),
                   command=self.schedule_game_detection).pack(anchor="w", pady=(0, 6))
        self._game_mode_changed()
        limit_row = ttk.Frame(pause_box)
        limit_row.pack(fill="x")
        ttk.Label(limit_row, text=tr_ui("Відновити автоматично через, с:")).pack(side="left")
        ttk.Spinbox(limit_row, from_=5, to=300, width=6,
                    textvariable=self._var(tk.IntVar, ("freeze", "pause_seconds"))).pack(side="left", padx=6)
        ttk.Button(pause_box, text=tr_ui("Відновити гру зараз"),
                   command=lambda: self.do_action("resume_game")).pack(anchor="w", pady=(6, 0))
        ttk.Label(pause_box, text=tr_ui("Пауза діє для пошуку слова/рядка, Ctrl+Alt+R та Ctrl+Alt+G. "
                                  "Режим до закриття картки тримає гру до Esc, закриття або ліміту часу. "
                                  "Профіль гри має пріоритет над загальним перемикачем паузи. Це заморозка процесу, "
                                  "не внутрішня пауза гри. Спершу тестуй на несуттєвій одиночній грі; "
                                  "не вмикай в онлайні."),
                  wraplength=520, foreground="#a2a8b8").pack(anchor="w", pady=(6, 0))

    def _build_free_cursor_controls(self, f):
        fc = ttk.LabelFrame(f, text=tr_ui("Гра з захопленою мишею (наприклад, Cyberpunk 2077)"), padding=10)
        fc.pack(fill="x", pady=(10, 0))
        ttk.Button(fc, text=tr_ui("🎯  Керований курсор (миша + клавіатура)"),
                   command=self.start_free_cursor).pack(anchor="w")
        ttk.Label(fc, text=tr_ui("Якщо миша прив'язана до камери в грі й наведення курсора не працює: "
                          "натисни цю кнопку (або гарячу клавішу) — з'явиться приціл, який можна "
                          "рухати фізичною мишею або стрілками/WASD (Shift — швидше), Enter/Пробіл — "
                          "знайти слово, Esc — скасувати. Працює незалежно від того, чи гра тримає "
                          "фокус вікна, і незалежно від системного курсора миші."),
                  foreground="#a2a8b8", wraplength=520
                  ).pack(anchor="w", pady=(4, 0))

    def _build_library_controls(self, f):
        self.saved_btn = ttk.Button(f, command=self.open_saved_words)
        self.saved_btn.pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(f, text=tr_ui("Зберігати скриншот контексту зі словом (локально, для повторення)"),
                        variable=self._var(tk.BooleanVar, ("learning", "capture_context"))
                        ).pack(anchor="w", pady=(6, 0))

    def _build_cache_controls(self, f):
        cache = ttk.LabelFrame(f, text=tr_ui("Кеш на диску"), padding=10)
        cache.pack(fill="x", pady=(12, 0))
        self.cache_var = tk.StringVar()
        ttk.Label(cache, textvariable=self.cache_var).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(cache, text=tr_ui("Зберігати до")).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(cache, from_=0, to=5000, increment=100, width=6,
                    textvariable=self._var(tk.IntVar, ("cache", "max_entries"))
                    ).grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(cache, text=tr_ui("слів (0 — не кешувати)")).grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Button(cache, text=tr_ui("Очистити кеш зараз"), command=self.clear_cache
                   ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(cache, text=tr_ui("Кожне вже знайдене слово зберігається на диску, щоб наступного "
                             "разу відкриватись миттєво й без інтернету. Розмір обмежений і "
                             "зазвичай не перевищує кілька мегабайтів."), foreground="#a2a8b8",
                  wraplength=520).grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def _build_app_controls(self, f):
        ttk.Checkbutton(f, text=tr_ui("Запускати згорнутою"),
                        variable=self._var(tk.BooleanVar, ("app", "start_minimized"))
                        ).pack(anchor="w", pady=(12, 0))
        bottom = ttk.Frame(f)
        bottom.pack(fill="x", side="bottom", pady=(14, 0))
        ttk.Button(bottom, text=tr_ui("Згорнути"), command=self.hide_window).pack(side="left")
        ttk.Button(bottom, text=tr_ui("Вийти з програми"), command=self.quit).pack(side="right")

    def _tab_keys(self, f):
        abox = ttk.LabelFrame(f, text=tr_ui("Автоматичний словник при виділенні тексту"), padding=10)
        abox.pack(fill="x")
        ttk.Checkbutton(abox, text=tr_ui("Показувати переклад/словник одразу, щойно виділено текст "
                                   "(без гарячої клавіші)"),
                        variable=self._var(tk.BooleanVar, ("auto", "enabled"),
                                           after=self._refresh_hint)
                        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(abox, text=tr_ui("Виділи слово чи фразу мишею (перетягуванням) або клацни двічі по "
                            "слову — і картка з'явиться сама. Працює там, де текст можна "
                            "виділити насправді: у браузері, PDF, Word тощо. Для гри, де текст "
                            "не виділити, лишається пошук по наведенню курсора вище."),
                  foreground="#a2a8b8", wraplength=540).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        mbox = ttk.LabelFrame(f, text=tr_ui("Кнопка миші для пошуку слова (наведення, як в іграх)"), padding=10)
        mbox.pack(fill="x", pady=(12, 0))
        ttk.Label(mbox, text=tr_ui("Кнопка:")).grid(row=0, column=0, sticky="w")
        self._combo(mbox, self._var(tk.StringVar, ("mouse", "button"), MOUSE_BTN_NAMES,
                                    self._refresh_hint), MOUSE_BTN_NAMES
                    ).grid(row=0, column=1, padx=8, pady=3, sticky="w")
        ttk.Label(mbox, text=tr_ui("Разом із клавішею:")).grid(row=1, column=0, sticky="w")
        self._combo(mbox, self._var(tk.StringVar, ("mouse", "modifier"), MOUSE_MOD_NAMES,
                                    self._refresh_hint), MOUSE_MOD_NAMES
                    ).grid(row=1, column=1, padx=8, pady=3, sticky="w")
        ttk.Label(mbox, text=tr_ui("Клік передається і грі/програмі — вибери кнопку, яка там не потрібна."),
                  foreground="#a2a8b8").grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        kbox = ttk.LabelFrame(f, text=tr_ui("Гарячі клавіші"), padding=10)
        kbox.pack(fill="x", pady=(12, 0))
        ttk.Label(kbox, text=tr_ui("Натисни «Записати», потім комбінацію. Esc — скасувати."),
                  foreground="#a2a8b8").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        self.key_vars, self.key_btns = {}, {}
        for i, (action, label) in enumerate(ACTIONS, start=1):
            ttk.Label(kbox, text=label).grid(row=i, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=pretty(self.settings.data["hotkeys"][action]))
            self.key_vars[action] = v
            ttk.Label(kbox, textvariable=v, width=20, anchor="center",
                      relief="sunken").grid(row=i, column=1, padx=8)
            b = ttk.Button(kbox, text=tr_ui("Записати"), command=lambda a=action: self.start_record(a))
            b.grid(row=i, column=2)
            self.key_btns[action] = b
            ttk.Button(kbox, text=tr_ui("Очистити"),
                       command=lambda a=action: self.set_hotkey(a, "")).grid(row=i, column=3, padx=(6, 0))
        ttk.Button(f, text=tr_ui("Скинути гарячі клавіші до стандартних"),
                   command=self.reset_hotkeys).pack(anchor="w", pady=(12, 0))

    def _tab_look(self, f):
        after = self._refresh_preview
        ttk.Label(f, text=tr_ui("Збери зручну картку"), font=(FONT, 13, "bold")).pack(anchor="w")
        ttk.Label(f, text=tr_ui("Зміни одразу видно у відкритій картці. Власні переклади залишаються збереженими."),
                  foreground="#a2a8b8", wraplength=570).pack(anchor="w", pady=(4, 10))
        presets = ttk.Frame(f)
        presets.pack(fill="x", pady=(0, 12))
        for key, label in (("compact", "Компактна"), ("balanced", "Збалансована"),
                           ("detailed", "Докладна")):
            ttk.Button(presets, text=label, command=lambda k=key: self.apply_card_preset(k)
                       ).pack(side="left", padx=(0, 6))
        g = ttk.LabelFrame(f, text=tr_ui("Розмір і оформлення"), padding=10)
        g.pack(fill="x")
        rows = [
            ("Тема", lambda p: self._combo(p, self._var(tk.StringVar, ("popup", "theme"),
                                                        THEME_NAMES, after), THEME_NAMES, 18)),
            ("Розмір шрифту", lambda p: ttk.Spinbox(p, from_=9, to=24, width=6,
                                                   textvariable=self._var(tk.IntVar, ("popup", "font_size"), after=after))),
            ("Ширина картки (px)", lambda p: ttk.Spinbox(p, from_=300, to=900, increment=20, width=6,
                                                         textvariable=self._var(tk.IntVar, ("popup", "width"), after=after))),
            ("Прозорість", lambda p: ttk.Scale(p, from_=0.5, to=1.0, length=160,
                                               variable=self._var(tk.DoubleVar, ("popup", "opacity"), after=after))),
            ("Закривати через (с, 0 — ні)", lambda p: ttk.Spinbox(p, from_=0, to=300, width=6,
                                                                  textvariable=self._var(tk.IntVar, ("popup", "timeout")))),
        ]
        for i, (text, make) in enumerate(rows):
            ttk.Label(g, text=text).grid(row=i, column=0, sticky="w", pady=4)
            make(g).grid(row=i, column=1, sticky="w", padx=10)
        r = len(rows)
        ttk.Label(g, text=tr_ui("Колір підсвічування")).grid(row=r, column=0, sticky="w", pady=4)
        self.color_btn = tk.Button(g, width=8, bg=self.settings.data["popup"]["highlight_color"],
                                   command=self.pick_color)
        self.color_btn.grid(row=r, column=1, sticky="w", padx=10)
        content = ttk.LabelFrame(f, text=tr_ui("Що показувати в картці"), padding=10)
        content.pack(fill="x", pady=(12, 0))
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        for i, (path, label) in enumerate(CARD_CONTENT_OPTIONS):
            ttk.Checkbutton(content, text=label,
                            variable=self._var(tk.BooleanVar, path, after=after)
                            ).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 10), pady=4)
        ttk.Label(content, text=tr_ui("Приклади й переклад визначення видно, коли увімкнені визначення."),
                  foreground="#a2a8b8", wraplength=540).grid(
                      row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(f, text=tr_ui("Підсвічувати знайдене слово на екрані"),
                        variable=self._var(tk.BooleanVar, ("ocr", "highlight"))
                        ).pack(anchor="w", pady=(10, 0))
        ttk.Button(f, text=tr_ui("Показати приклад картки"), style="Accent.TButton",
                   command=self.show_preview).pack(
            anchor="w", pady=(14, 0))

    def open_card_settings(self):
        self.show_window()
        self.notebook.select(self.look_panel)

    def apply_card_preset(self, name):
        if name not in ("compact", "balanced", "detailed"):
            return
        secondary = {("sections", "thesaurus"), ("popup", "examples"),
                     ("popup", "alternate_translations"), ("popup", "context_image")}
        hidden = secondary | {("sections", "definitions")} if name == "compact" else secondary
        for path, _label in CARD_CONTENT_OPTIONS:
            visible = name == "detailed" or path not in hidden
            self._var(tk.BooleanVar, path, after=self._refresh_preview).set(visible)
        for key, value in (("max_definitions", 2 if name == "detailed" else 1),
                           ("max_synonyms", 5 if name == "detailed" else 4)):
            self._var(tk.IntVar, ("dictionary", key), after=self._refresh_preview).set(value)
        self._var(tk.IntVar, ("popup", "font_size"), after=self._refresh_preview).set(
            11 if name == "detailed" else 10)
        self._var(tk.IntVar, ("popup", "width"), after=self._refresh_preview).set(
            {"compact": 360, "balanced": 400, "detailed": 460}[name])

    def _tab_dict(self, f):
        self._build_network_settings(f)
        box = ttk.LabelFrame(f, text=tr_ui("Словник"), padding=10)
        box.pack(fill="x")
        ttk.Label(box, text=tr_ui("Переклад на:")).grid(row=0, column=0, sticky="w")
        self._combo(box, self._var(tk.StringVar, ("languages", "target"), LANG_NAMES,
                                   self._language_changed), LANG_NAMES, 18
                    ).grid(row=0, column=1, sticky="w", padx=10, pady=3)
        ttk.Button(box, text=tr_ui("Налаштувати вміст картки…"), command=self.open_card_settings
                   ).grid(row=1, column=0, columnspan=2, sticky="w", pady=6)
        for i, (key, text) in enumerate((("max_translations", "Варіантів перекладу на частину мови"),
                                         ("max_definitions", "Визначень у згорнутому вигляді"),
                                         ("max_synonyms", "Синонімів у згорнутому вигляді")), start=2):
            ttk.Label(box, text=text).grid(row=i, column=0, sticky="w", pady=3)
            ttk.Spinbox(box, from_=1, to=20, width=5,
                        textvariable=self._var(tk.IntVar, ("dictionary", key),
                                               after=self.cache.cards.clear)
                        ).grid(row=i, column=1, sticky="w", padx=10)

        ob = ttk.LabelFrame(f, text=tr_ui("OCR (розпізнавання тексту на екрані)"), padding=10)
        ob.pack(fill="x", pady=(12, 0))
        ttk.Label(ob, text=tr_ui("Рушій:")).grid(row=0, column=0, sticky="w")
        self._combo(ob, self._var(tk.StringVar, ("ocr", "engine"), ENGINE_NAMES, self._check_ocr),
                    ENGINE_NAMES, 14).grid(row=0, column=1, sticky="w", padx=10, pady=3)
        ttk.Label(ob, text=tr_ui("Шлях до tesseract:")).grid(row=1, column=0, sticky="w")
        row = ttk.Frame(ob)
        row.grid(row=1, column=1, sticky="w", padx=10)
        ttk.Entry(row, width=34, textvariable=self._var(tk.StringVar, ("ocr", "tesseract_cmd"))
                  ).pack(side="left")
        ttk.Button(row, text="…", width=3, command=self.browse_tesseract).pack(side="left", padx=4)
        for i, (key, text, lo, hi, inc) in enumerate((
                ("region_w", "Ширина області пошуку слова (px)", 200, 2000, 50),
                ("region_h", "Висота області пошуку слова (px)", 40, 600, 10)), start=2):
            ttk.Label(ob, text=text).grid(row=i, column=0, sticky="w", pady=3)
            ttk.Spinbox(ob, from_=lo, to=hi, increment=inc, width=7,
                        textvariable=self._var(tk.IntVar, ("ocr", key))
                        ).grid(row=i, column=1, sticky="w", padx=10)
        ttk.Label(ob, text=tr_ui("Збільшення знімка (1–4, лише Tesseract)")).grid(row=4, column=0, sticky="w", pady=3)
        ttk.Spinbox(ob, from_=1.0, to=4.0, increment=0.5, width=7,
                    textvariable=self._var(tk.DoubleVar, ("ocr", "upscale"))
                    ).grid(row=4, column=1, sticky="w", padx=10)
        ttk.Label(ob, text=tr_ui("RapidOCR масштабує знімок сам; збільшення потрібне лише для Tesseract."),
                  foreground="#a2a8b8").grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Checkbutton(ob, text=tr_ui("Розпізнавати злиті слова без пробілу (напр. «Nexttime» → «Next»)"),
                        variable=self._var(tk.BooleanVar, ("ocr", "split_merged_words"))
                        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(ob, text=tr_ui("Іноді OCR губить пробіл між двома сусідніми словами. Ця перевірка "
                          "шукає видиму прогалину на зображенні й уточнює лише те слово, де "
                          "курсор. Вимкни, якщо вона починає помилятись на звичайних словах."),
                  foreground="#a2a8b8", wraplength=520).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(2, 0))

        self._build_ocr_budget_settings(f)
        dl = ttk.LabelFrame(f, text=tr_ui("DeepL — переклад з урахуванням контексту (необов'язково)"),
                            padding=10)
        dl.pack(fill="x", pady=(12, 0))
        ttk.Checkbutton(dl, text=tr_ui("Використовувати DeepL, коли ключ вказано"),
                        variable=self._var(tk.BooleanVar, ("deepl", "enabled"),
                                           after=self._on_deepl_changed)
                        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(dl, text=tr_ui("API-ключ:")).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.deepl_entry = ttk.Entry(dl, width=42, show="•",
                                     textvariable=self._var(tk.StringVar, ("deepl", "key"),
                                                            after=self._on_deepl_changed))
        self.deepl_entry.grid(row=1, column=1, sticky="w", padx=8, pady=(6, 0))
        show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(dl, text=tr_ui("показати"), variable=show_var,
                        command=lambda: self.deepl_entry.config(show="" if show_var.get() else "•")
                        ).grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Button(dl, text=tr_ui("Перевірити ключ"), command=self.check_deepl
                   ).grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Button(dl, text=tr_ui("Зберегти ключ"), command=self.retry_deepl_key_storage
                   ).grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.deepl_var = tk.StringVar(value=self.settings.credential_status)
        ttk.Label(dl, textvariable=self.deepl_var, wraplength=380, justify="left"
                  ).grid(row=2, column=1, columnspan=2, sticky="w", padx=8, pady=(8, 0))
        ttk.Label(dl, text=tr_ui("DeepL часто краще розуміє контекст: при пошуку слова по OCR "
                          "програма передає йому весь рядок навколо слова, тож «chest» у "
                          "Minecraft має перекладатися як «сундук». Потрібен безкоштовний ключ "
                          "«DeepL API Free» (deepl.com → API; до 500 000 символів на місяць, "
                          "при реєстрації можуть попросити картку для верифікації). Текст "
                          "надсилається на сервери DeepL. Windows: ключ зберігається у Credential Manager "
                          "або в локальному файлі, зашифрованому Windows DPAPI для твого облікового запису. "
                          "macOS використовує Keychain, Linux — Secret Service за наявності "
                          "розблокованого сховища сеансу. Ключ підхоплюється після запуску. "
                          "Без захищеного сховища — лише цей сеанс, без запису у config.json. "
                          "Якщо DeepL недоступний або ліміт вичерпано, "
                          "резервні джерела працюють лише коли їх дозволено вище."),
                  foreground="#a2a8b8", wraplength=520, justify="left"
                  ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.translator_settings = TranslatorSettingsView(f, root=self.root, settings=lambda: self.settings.data,
            variable=self._var, credentials=self.settings.credentials, save_key=self.save_provider_key,
            official=self.providers.official, offline=self.providers.offline,
            target_language=self.providers._target_language, events=self.events, log_error=self.logger.exception,
            reset_cooldown=self.providers.clear_provider_cooldown)

        ak = ttk.LabelFrame(f, text=tr_ui("Anki — тег гри для карток"), padding=10)
        ak.pack(fill="x", pady=(12, 0))
        ttk.Label(ak, text=tr_ui("Назва гри:")).grid(row=0, column=0, sticky="w")
        ttk.Entry(ak, width=28, textvariable=self._var(tk.StringVar, ("anki", "game"))
                  ).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Checkbutton(ak, text=tr_ui("Якщо порожньо — брати назву з заголовка вікна гри (лише Windows)"),
                        variable=self._var(tk.BooleanVar, ("anki", "auto_game_tag"))
                        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(ak, text=tr_ui("Тег додається до карток .apkg: в Anki за ним можна відфільтрувати "
                          "слова з конкретної гри. Назва, вписана тут, ставиться всім "
                          "експортованим карткам. На картці також буде блок «From the game» "
                          "з реченням, у якому ти знайшов слово."),
                  foreground="#a2a8b8", wraplength=520, justify="left"
                  ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _on_deepl_changed(self):
        self.diagnostics.record("deepl.settings_changed", configured=bool(self.settings.data["deepl"]["key"]),
                                    enabled=self.settings.data["deepl"]["enabled"])
        self.providers.clear_provider_cooldown("DeepL")
        if hasattr(self, "deepl_var"):
            self.deepl_var.set("")

    def save_provider_key(self, provider):
        if self._config_save_timer is not None:
            self.root.after_cancel(self._config_save_timer)
            self._config_save_timer = None
        saved = self.settings.save_config(self.settings.data, retry_provider=provider)
        return self.settings.credentials.status(provider) if saved else "Не вдалося записати налаштування. Дивись журнал."

    def retry_deepl_key_storage(self):
        """Retry protected storage only for a key action, never a checkbox save."""
        if self._config_save_timer is not None:
            self.root.after_cancel(self._config_save_timer)
            self._config_save_timer = None
        saved = self.settings.save_config(self.settings.data, retry_key=True)
        self.deepl_var.set(self.settings.credential_status if saved else
                          "Не вдалося зберегти налаштування. Дивись журнал.")

    def check_deepl(self):
        self.deepl_var.set("Перевіряю…")

        def work():
            try:
                used, limit = self.providers.deepl_usage()
                txt = (f"✓ Ключ працює. Використано {used:,} з {limit:,} символів за місяць"
                       ).replace(",", " ")
            except DeeplError as e:
                txt = f"✗ {e}"
            except Exception as e:
                txt = f"✗ Не вдалося перевірити: {type(e).__name__}: {e}"
            self.events.put(as_event(("deepl_status", txt)))
        threading.Thread(target=work, daemon=True).start()

    def _refresh_status(self):
        self.status_var.set("⏸ На паузі" if self.paused else "● Працює")

    def _refresh_hint(self):
        parts = []
        if self.settings.data["auto"]["enabled"]:
            parts.append("Виділи текст мишею (або двічі клацни по слову) — картка з'явиться сама.")
        btn = self.settings.data["mouse"]["button"]
        if btn != "none":
            names = {v: k for k, v in MOUSE_BTN_NAMES.items()}
            mod = self.settings.data["mouse"]["modifier"]
            mod_txt = "" if mod == "none" else f"{mod.capitalize()} + "
            parts.append(f"Наведи курсор на слово і натисни: {mod_txt}{names[btn]}.")
        hk = self.settings.data["hotkeys"]["lookup_word"]
        if hk:
            parts.append(f"Або гаряча клавіша: {pretty(hk)}.")
        tl = self.settings.data["hotkeys"]["translate_line"]
        if tl:
            parts.append(f"Переклад усього рядка: {pretty(tl)}.")
        sr = self.settings.data["hotkeys"]["select_region"]
        if sr:
            parts.append(f"Виділити рамкою слово чи рядок (як скріншот): {pretty(sr)}.")
        if not parts:
            parts.append("Кнопки пошуку не задано — налаштуй їх на вкладці «Кнопки».")
        self.hint_var.set(" ".join(parts))

    def _refresh_cache_label(self):
        kb = self.cache.cache_disk_kb()
        size = f"{kb:.0f} КБ" if kb < 1024 else f"{kb / 1024:.1f} МБ"
        self.cache_var.set(f"На диску зараз: {size}  ·  слів у кеші: {len(self.cache.cards)}  ·  "
                           f"файл: {self.cache._cache_path()}")

    def clear_cache(self):
        self.cache.clear_card_cache()
        self._refresh_cache_label()
        self.toast("Кеш очищено")
