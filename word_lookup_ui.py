"""Desktop views, popup/selection overlays and the settings/library view mixin.

The application runtime supplies configuration, state and controller actions.
"""
from __future__ import annotations
import tkinter as tk

runtime = None


def configure(application):
    global runtime
    runtime = application


class ScrolledPanel(tk.Frame):
    """Прокручування лише панелі під курсором, без глобального bind_all."""

    def __init__(self, parent, width=560, height=500, bg=None):
        bg = bg or parent.cget("background")
        super().__init__(parent, bg=bg, width=width, height=height)
        self.pack_propagate(False)
        self.canvas = runtime.tk.Canvas(self, highlightthickness=0, bd=0, bg=bg)
        self.bar = runtime.ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = runtime.tk.Frame(self.canvas, bg=bg)
        self.item = self.canvas.create_window(0, 0, anchor="nw", window=self.content)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.content.bind("<Configure>", self._sync)
        self.canvas.bind("<Configure>", self._resize)
        self._top = self.winfo_toplevel()
        self._wheel_ids = {seq: self._top.bind(seq, self._wheel, add="+")
                           for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>")}
        self.bind("<Destroy>", self._cleanup)

    def _sync(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize(self, event):
        self.canvas.itemconfigure(self.item, width=max(1, event.width))

    def _wheel(self, event):
        x, y = event.x_root, event.y_root
        if not (self.winfo_rootx() <= x < self.winfo_rootx() + self.winfo_width()
                and self.winfo_rooty() <= y < self.winfo_rooty() + self.winfo_height()):
            return
        if self.canvas.yview() == (0.0, 1.0):
            return
        delta = getattr(event, "delta", 0)
        units = (-max(1, abs(int(delta / 120))) if delta > 0 else
                 max(1, abs(int(delta / 120))) if delta < 0 else
                 -3 if event.num == 4 else 3)
        self.canvas.yview_scroll(units, "units")
        return "break"

    def _cleanup(self, event):
        if event.widget is self:
            for seq, callback in self._wheel_ids.items():
                try:
                    self._top.unbind(seq, callback)
                except runtime.tk.TclError:
                    runtime.log_exc('word_lookup._cleanup')


def screen_workarea(root, anchor):
    """Монітор біля слова, включно з моніторами ліворуч від основного."""
    x, y = int((anchor[0] + anchor[2]) / 2), int((anchor[1] + anchor[3]) / 2)
    if runtime.sys.platform.startswith("win"):
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
            runtime.log_exc('word_lookup.screen_workarea')
    try:
        import mss
        with mss.mss() as sct:
            for m in sct.monitors[1:]:
                if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
                    return m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"]
    except Exception:
        runtime.log_exc('word_lookup.screen_workarea')
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def place_popup(anchor, width, height, area, toast=False):
    left, top, right, bottom = area
    width, height = min(width, right - left - 16), min(height, bottom - top - 16)
    cx = (anchor[0] + anchor[2]) / 2
    above = not toast and anchor[1] - height - 6 >= top + 8
    x = anchor[0] + 12 if toast else cx - width / 2
    y = anchor[1] + 20 if toast else anchor[1] - height - 6 if above else anchor[3] + 6
    return (int(runtime.clamp(x, left + 8, right - width - 8)),
            int(runtime.clamp(y, top + 8, bottom - height - 8)), int(width), int(height), above)


class Highlight:
    """Жовте підсвічування слова (як у macOS Look Up)."""

    def __init__(self, app, text, box):
        color = runtime.cfg["popup"]["highlight_color"]
        x0, y0, x1, y1 = box
        self.win = w = runtime.tk.Toplevel(app.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=color)
        h = max(12, y1 - y0)
        font = runtime.tkfont.Font(family=runtime.FONT, size=-int(h * 0.95))
        runtime.tk.Label(w, text=text, font=font, fg="#000000", bg=color, padx=5, pady=0).pack()
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
            runtime.log_exc('word_lookup.close')


class Popup:
    """Картка словника: заокруглена панель зі стрілкою до слова."""

    def __init__(self, app, card, anchor, expanded=(), toast=False, timeout=None, pinned=False):
        self.app, self.card, self.anchor, self.toast = app, card, anchor, toast
        self.expanded = set(expanded)
        self.pinned = pinned
        self._timer = None
        P = runtime.cfg["popup"]
        self.th = th = runtime.THEMES.get(P["theme"], runtime.THEMES["light"])
        self.fs = int(runtime.clamp(P["font_size"], 9, 24))
        self.cw = int(runtime.clamp(P["width"], 300, 900))
        self.workarea = runtime.screen_workarea(app.root, anchor)
        self.cw = min(self.cw, self.workarea[2] - self.workarea[0] - 64)

        self.win = w = runtime.tk.Toplevel(app.root)
        w.withdraw()
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        self.rounded = False
        if runtime.sys.platform.startswith("win"):
            try:
                w.attributes("-transparentcolor", runtime.KEY)
                self.rounded = True
            except runtime.tk.TclError:
                runtime.log_exc('word_lookup.__init__')
        base_bg = runtime.KEY if self.rounded else th["bg"]
        w.configure(bg=base_bg)
        alpha = runtime.clamp(P["opacity"], 0.4, 1.0)
        if alpha < 1.0:
            try:
                w.attributes("-alpha", alpha)
            except runtime.tk.TclError:
                runtime.log_exc('word_lookup.__init__')

        self.canvas = runtime.tk.Canvas(w, bg=base_bg, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.viewport = runtime.ScrolledPanel(self.canvas, width=self.cw + 18, bg=th["bg"])
        self.content = self.viewport.content
        runtime.tk.Frame(self.content, width=self.cw, height=0, bg=th["bg"]).pack()  # мін. ширина

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
            timeout = 1.8 if toast else float(runtime.clamp(P["timeout"], 0, 300))
        self._timeout_ms = int(timeout * 1000)
        self._arm_timeout()

    def _toolbar(self):
        # Actions get their own row: a long headword must never compete with
        # buttons for horizontal space. Narrow/high-DPI cards use two rows.
        self.toolbar = row = runtime.tk.Frame(self.content, bg=self.th["bg"])
        row.pack(fill="x", pady=(0, 9))
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
        return runtime.tk.Button(parent, text=text, command=command, font=(runtime.FONT, 9),
                         bg=self.th["bg"], fg=self.th["link"], relief="flat", bd=0, highlightthickness=0,
                         activebackground=self.th["line"], activeforeground=self.th["fg"],
                         padx=5, pady=3, cursor="hand2", takefocus=True)

    def _show_options(self):
        # Під час налаштування картка залишається відкритою, зокрема при кліку в меню.
        if not self.pinned:
            self.toggle_pin()
        menu = getattr(self, "options_menu", None)
        if menu is None:
            self.options_menu = menu = runtime.tk.Menu(self.win, tearoff=False, font=(runtime.FONT, 9))
            for path, label in runtime.CARD_CONTENT_OPTIONS:
                menu.add_checkbutton(label=label, variable=self.app._var(runtime.tk.BooleanVar, path,
                                                             after=self.app._refresh_preview))
            menu.add_separator()
            menu.add_command(label="Усі налаштування картки…", command=self.app.open_card_settings)
        try:
            menu.tk_popup(self.options_btn.winfo_rootx(),
                          self.options_btn.winfo_rooty() + self.options_btn.winfo_height())
        finally:
            menu.grab_release()

    def copy_translation(self):
        text = self.card.get("translation") or runtime._card_primary_translation(self.card)
        if text:
            try:
                runtime.pyperclip.copy(text)
                self.app.toast("Переклад скопійовано")
            except Exception as e:
                self.app.toast(f"Буфер обміну недоступний: {e}")

    def save_current(self):
        if self.card.get("kind") in ("word", "text") and runtime._entry_complete({
                "translation": runtime._card_primary_translation(self.card),
                "definition": runtime._card_definition(self.card)}):
            runtime.add_saved_word(self.card)
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
        font = (runtime.FONT, self.fs + size) + ((style,) if style else ())
        lb = runtime.tk.Label(self.content, text=text, font=font, fg=color or self.th["fg"],
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
        font = runtime.tkfont.Font(family=runtime.FONT, size=self.fs + size, weight="bold" if bold else "normal")
        char_px = max(1, font.measure("0"))
        chars = max(10, (self.cw - indent) // char_px)
        total_px = font.measure("".join(t for t, _ in parts))
        lines = max(1, int(total_px * 1.15 / (chars * char_px)) + 1)
        w = runtime.tk.Text(self.content, wrap="word", width=chars, height=lines, bd=0,
                    highlightthickness=0, padx=0, pady=0, bg=th["bg"], fg=color or th["fg"],
                    font=font, cursor="arrow", takefocus=0)
        w.tag_configure("hl", background=runtime.cfg["popup"]["highlight_color"], foreground="#000000")
        for t, h in parts:
            w.insert("end", t, "hl" if h else "")
        w.config(state="disabled")
        w.pack(anchor="w", padx=(indent, 0), pady=pady)

    def section(self, title, first):
        th = self.th
        row = runtime.tk.Frame(self.content, bg=th["bg"])
        row.pack(fill="x", pady=(0 if first else 12, 4))
        runtime.tk.Label(row, text=title.upper(), font=(runtime.FONT, max(9, self.fs - 2)), fg=th["head"],
                 bg=th["bg"]).pack(side="left")

    def headword(self, word, with_audio, show_phonetic=True, with_save=False):
        th = self.th
        self._toolbar()
        row = runtime.tk.Frame(self.content, bg=th["bg"])
        row.pack(fill="x", padx=(6, 0), pady=(0, 3))
        self.word_title = runtime.tk.Label(row, text=word, font=(runtime.FONT, self.fs + 10, "bold"),
                                   fg=th["fg"], bg=th["bg"], cursor="hand2", anchor="w",
                                   justify="left", wraplength=max(1, self.cw - 6), bd=0,
                                   padx=0, highlightthickness=0)
        self.word_title.pack(fill="x")
        self.word_title.bind("<Button-1>", self.edit_word)
        self.correction_row = runtime.tk.Frame(self.content, bg=th["bg"])
        self.word_var = runtime.tk.StringVar(master=self.win, value=word)
        self.word_editor = runtime.tk.Entry(self.correction_row, textvariable=self.word_var,
                                    font=(runtime.FONT, self.fs + 1), bg=th["line"], fg=th["fg"],
                                    insertbackground=th["link"], relief="flat", bd=6)
        self.word_editor.pack(side="left", fill="x", expand=True)
        self._tool_button(self.correction_row, "Знайти →", self.submit_word).pack(side="left", padx=5)
        self.word_editor.bind("<Return>", lambda _e: self.submit_word())
        self.word_editor.bind("<Escape>", self.cancel_word_edit)
        helper = runtime.tk.Frame(self.content, bg=th["bg"])
        helper.pack(fill="x", padx=6, pady=(0, 8))
        self._tool_button(helper, "Не те слово? Виправити", self.edit_word).pack(side="left")
        if with_audio and runtime.AUDIO_OK and runtime.cfg["popup"]["btn_speak"]:
            for label, url in self.card.get("audios", []):
                self._tool_button(helper, f"🔊 {label}", lambda u=url: runtime.play_audio(u)).pack(side="right")
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
            self.app._word_edit_escape_until = runtime.time.monotonic() + 0.3
        self.correction_row.pack_forget()
        self.word_var.set(self.card["word"])
        return "break"

    def submit_word(self):
        word = self.word_var.get().strip()
        # One English word (including apostrophe/hyphen), never a second OCR pass.
        if not runtime.re.fullmatch(r"[A-Za-z]+(?:['’\-][A-Za-z]+)*", word) or len(word) > 80:
            self.app.toast("Введи одне англійське слово")
            self.word_editor.focus_force()
            return "break"
        self.app.retry_word(word, self.card, self.anchor)
        return "break"

    def save_button(self, row):
        """Кнопка ⭐ Зберегти / ✓ Збережено — праворуч у рядку слова."""
        word = self.card["word"]
        entry = runtime.saved_entry(word)
        saved = entry is not None and runtime._entry_complete(entry)
        btn = runtime.tk.Label(row, text=("✓ Збережено" if saved else "⭐ Зберегти"),
                       font=(runtime.FONT, self.fs - 2),
                       fg=(self.th["dim"] if saved else self.th["link"]),
                       bg=self.th["bg"], cursor=("arrow" if saved else "hand2"))
        btn.pack(side="right", padx=(6, 0))
        if not saved:
            def on_click(_e):
                if not (runtime._card_primary_translation(self.card) or runtime._card_definition(self.card)):
                    if self.card.get("dict_pending"):
                        self.app.toast("Переклад ще не завантажився — зачекай і спробуй ще раз")
                    else:
                        reason = self.card.get("note") or self.card.get("dict_note") or \
                                 "ні переклад, ні визначення не знайдені"
                        self.app.toast(f"Не вдалося зберегти «{self.card['word']}»: {reason}")
                    return
                if runtime.add_saved_word(self.card):
                    btn.config(text="✓ Збережено", fg=self.th["dim"], cursor="arrow")
                    btn.unbind("<Button-1>")
                    app = self.app
                    if hasattr(app, "refresh_saved_count"):
                        app.refresh_saved_count()
            btn.bind("<Button-1>", on_click)

    def more(self, key):
        b = runtime.tk.Label(self.content, text="Показати ще", font=(runtime.FONT, self.fs - 1, "bold"),
                     fg=self.th["link"], bg=self.th["bg"], cursor="hand2")
        b.pack(anchor="w", padx=(6, 0), pady=(3, 0))

        def expand(_e):
            self.expanded.add(key)
            self.app.rerender(self)
        b.bind("<Button-1>", expand)

    # ---------- вміст ----------
    def _build_text(self):
        tl = runtime.cfg["languages"]["target"]
        self.section(f"English – {runtime.LANG_EN.get(tl, tl)}", True)
        row = runtime.tk.Frame(self.content, bg=self.th["bg"])
        row.pack(fill="x", pady=4)
        self.save_button(row)
        if runtime.cfg["popup"]["sentence"]:
            self.lbl(self.card["original"], size=-1, color=self.th["dim"], indent=6)
        self.lbl(self.card["translation"], bold=True, indent=6, pady=(4, 0))
        self.context_photo = (runtime.context_photo(self.card.get("image_file", ""), self.win,
                                            size=(max(1, self.cw - 12), 220))
                              if runtime.cfg["popup"]["context_image"] else None)
        if self.context_photo:
            runtime.tk.Label(self.content, image=self.context_photo, bg=self.th["bg"]).pack(anchor="w", padx=6)

    def _build_word(self):
        """Слово й вимова, контекст, переклад, значення та джерело з дією збереження."""
        c, S, D, th = self.card, runtime.cfg["sections"], runtime.cfg["dictionary"], self.th
        max_def = int(runtime.clamp(D["max_definitions"], 1, 6))
        max_syn = int(runtime.clamp(D["max_synonyms"], 1, 20))
        meanings = c.get("meanings", [])
        groups = c.get("groups", [])

        # 1) Слово, озвучення, ⭐ Зберегти
        self.headword(c["word"], with_audio=True, show_phonetic=False, with_save=False)

        # 3) Вимова і частина мови
        pos0 = meanings[0].get("pos", "") if meanings else ""
        bits = [b for b in ((f"/{c['phonetic']}/" if c.get("phonetic") and runtime.cfg["popup"]["phonetic"] else ""), pos0) if b]
        if bits:
            self.lbl("  ·  ".join(bits), size=-1, color=th["dim"], indent=6, pady=(1, 0))


        if c.get("corrected_from"):
            self.lbl(f"Схоже, гра показала «{c['corrected_from']}» — знайдено найближче "
                     f"слово «{c.get('base', '')}»", size=-1, color=th["dim"], indent=6,
                     pady=(2, 0))
        elif c.get("base") and c["base"].lower() != c["word"].lower():
            self.lbl(f"Форма слова «{c['base']}»", size=-1, color=th["dim"], indent=6,
                     pady=(2, 0))

        # речення з гри + його переклад (слово підсвічене в обох) — як у Translator
        sentence = c.get("sentence") or ""
        if sentence:
            if runtime.cfg["popup"]["sentence"] or runtime.cfg["popup"]["sentence_translation"]:
                self.section("Речення", False)
            if runtime.cfg["popup"]["sentence"]:
                self.rich_text(runtime.split_around_word(sentence, c["word"]), color=th["dim"], size=-1,
                               pady=(6, 0))
            stl = c.get("sentence_tr") or ""
            if stl and runtime.cfg["popup"]["sentence_translation"]:
                span = runtime.align_word_span(stl, runtime.primary_translations(c))
                parts = ([(stl[:span[0]], False), (stl[span[0]:span[1]], True), (stl[span[1]:], False)]
                         if span else [(stl, False)])
                self.rich_text(parts, pady=(2, 0))
            elif c.get("sentence_pending") and runtime.cfg["popup"]["sentence_translation"]:
                self.lbl("Перекладаю речення…", size=-1, color=th["dim"], indent=6, pady=(2, 0))

        # 2) Головний переклад — кілька варіантів одразу, якщо слово має різні значення
        # (напр. "chest" — і "груди", і "сундук"; один-єдиний переклад від Google
        # часто вгадує не той контекст)
        alts = runtime.primary_translations(c)
        if S["bilingual"] and alts:
            self.lbl(" · ".join(alts), size=6, bold=True, indent=6, pady=(14, 4))
        elif S["bilingual"] and c.get("note"):
            self.lbl("⚠ " + c["note"], color="#c2410c", indent=6, pady=(2, 0))

        # 4-5) Основне значення й приклад використання
        if S["definitions"] and meanings:
            self.section("Значення", False)
            exp = "def" in self.expanded
            shown = meanings if exp else meanings[:max_def]
            first_def = True
            for m in shown:
                self.lbl(m["pos"], size=-1, bold=True, color=th["head"], indent=6, pady=(8, 0))
                for d in (m["defs"][:3] if exp else m["defs"][:1]):
                    self.lbl(d["d"], indent=6)
                    if first_def:  # переклад першого визначення — одразу під ним
                        first_def = False
                        if c.get("definition_tr") and runtime.cfg["popup"]["translate_definition"]:
                            self.lbl(c["definition_tr"], indent=6, pady=(3, 0))
                        elif c.get("def_tr_pending") and runtime.cfg["popup"]["translate_definition"]:
                            self.lbl("…", color=th["dim"], indent=6)
                    if d["e"] and runtime.cfg["popup"]["examples"]:
                        self.lbl(d["e"], italic=True, color=th["example"], indent=6)
            if not exp and (len(meanings) > max_def or any(len(m["defs"]) > 1 for m in shown)):
                self.more("def")
        elif (S["definitions"] or S["thesaurus"]) and c.get("dict_note"):
            self.lbl("⚠ " + c["dict_note"], color="#c2410c", indent=6, pady=(8, 0))
            self.lbl('Спробуй кнопку «Діагностика словника» на вкладці «Головна».',
                     size=-1, color=th["dim"], indent=6, pady=(2, 0))
        elif (S["definitions"] or S["thesaurus"]) and c.get("dict_pending"):
            self.lbl("Завантажую визначення…", color=th["dim"], indent=6, pady=(8, 0))

        # 6) Синоніми / антоніми
        thes = [m for m in meanings if m["syn"] or m["ant"]]
        if S["thesaurus"] and thes:
            exp = "thes" in self.expanded
            shown = thes if exp else thes[:max_def]
            for m in shown:
                syn = m["syn"][: (15 if exp else max_syn)]
                if syn:
                    self.lbl("Синоніми: " + ", ".join(syn), indent=6, pady=(6, 0))
                if m["ant"]:
                    self.lbl("Антоніми: " + ", ".join(m["ant"][: (10 if exp else 5)]),
                             color=th["dim"], indent=6, pady=(2, 0))
            if not exp and (len(thes) > max_def or any(len(m["syn"]) > max_syn for m in shown)):
                self.more("thes")

        # 7) Додаткові варіанти перекладу — унизу, другорядно
        if S["bilingual"] and groups and runtime.cfg["popup"]["alternate_translations"]:
            self.lbl("Інші варіанти перекладу", size=-1, bold=True, color=th["head"],
                     indent=6, pady=(8, 2))
            for pos, terms in groups:
                label = f"{pos}: " if pos else ""
                self.lbl(label + ", ".join(terms), indent=6)
        self.context_photo = (runtime.context_photo(c.get("image_file", ""), self.win,
                                            size=(max(1, self.cw - 12), 220))
                              if runtime.cfg["popup"]["context_image"] else None)
        if self.context_photo:
            self.lbl("Контекст із гри", size=-1, color=th["dim"], indent=6, pady=(8, 2))
            runtime.tk.Label(self.content, image=self.context_photo, bg=th["bg"]).pack(anchor="w", padx=6)

        footer = runtime.tk.Frame(self.content, bg=th["bg"])
        footer.pack(fill="x", padx=6, pady=(18, 2))
        runtime.tk.Label(footer, text=c.get("source", ""), font=(runtime.FONT, max(9, self.fs - 1)),
                 fg=th["dim"], bg=th["bg"], wraplength=max(100, self.cw - 185),
                 justify="left").pack(side="left")
        saved = runtime.saved_entry(c["word"])
        runtime.tk.Button(footer, text="✓ Збережено" if saved and runtime._entry_complete(saved) else "Зберегти слово",
                  command=self.save_current, bg="#ffd43b", fg="#17191e", activebackground="#ffe37a",
                  relief="flat", bd=0, highlightthickness=0, padx=14, pady=9, cursor="hand2",
                  font=(runtime.FONT, self.fs, "bold")).pack(side="right")

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

        x, y, W_, H_, above = runtime.place_popup(self.anchor, W_, H_, self.workarea, self.toast)

        c.config(width=W_, height=H_)
        top = 0 if above else AH
        if self.rounded:
            ax = runtime.clamp(cx - x, R + AH + 4, PW - R - AH - 4)

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
            runtime.log_exc('word_lookup.close')
        try:
            self.win.destroy()
        except Exception:
            runtime.log_exc('word_lookup.close')


def _cancel_view_timers(win):
    """Remove delayed overlay callbacks before Tcl deletes their commands."""
    commands = set(win._tclCommands or ())
    for timer in win.tk.call("after", "info"):
        script = win.tk.call("after", "info", timer)[0]
        if win.tk.splitlist(script)[0] in commands:
            win.after_cancel(timer)


class RegionSelector:
    """Накладка на весь екран: тягни рамку навколо слова чи рядка (як у скріншотері).
    Наведи звичайний курсор на слово — воно підсвітиться, лівий клік шукає саме його;
    можна й обвести рамкою кілька слів. Esc або права кнопка — скасувати.
    Одразу при відкритті робить «стоп-кадр» усього екрана й показує саме його — тому
    підказка (тултіп), яка в грі зникає при русі миші, лишається на місці, поки ти
    спокійно виділяєш потрібне слово."""

    HOVER_DELAY_MS = 150  # слово розпізнається, коли курсор на мить зупинився

    def __init__(self, app, on_done):
        self.on_done, self.start, self.done = on_done, None, False
        self.hover_hit, self.mouse = None, (-9999, -9999)
        self._after_id, self._req, self._busy = None, None, False
        self._lock, self._hq = runtime.threading.Lock(), runtime.queue.Queue()
        self._prev_hwnd = runtime._foreground_window()  # повернути фокус грі після закриття
        r = app.root
        try:
            x, y = r.winfo_vrootx(), r.winfo_vrooty()
            w, h = r.winfo_vrootwidth(), r.winfo_vrootheight()
            if w < 100 or h < 100:
                raise ValueError
        except Exception:
            x, y, w, h = 0, 0, r.winfo_screenwidth(), r.winfo_screenheight()
        self.w, self.h = w, h
        self.ox, self.oy = x, y  # початок віртуального екрана: екранні координати -> координати полотна
        self.frozen = None  # (img, ox, oy) — заповниться нижче, якщо знімок вдався

        self.win = win = runtime.tk.Toplevel(r)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="#111318")
        win.geometry(f"{w}x{h}+{x}+{y}")
        c = self.canvas = runtime.tk.Canvas(win, bg="#111318", highlightthickness=0, cursor="arrow")
        c.pack(fill="both", expand=True)

        frozen_ok = False
        try:
            from PIL import ImageTk
            shot_img, sx0, sy0, *_ = runtime._grab(x, y, x + w, y + h)
            app._resume_game()
            self._photo = ImageTk.PhotoImage(shot_img, master=win)  # тримаємо посилання — інакше Tk «забуде» картинку
            c.create_image(0, 0, image=self._photo, anchor="nw")
            self.frozen = (shot_img, sx0, sy0)
            frozen_ok = True
        except Exception as e:
            app._resume_game()
            runtime.log(f"Стоп-кадр не вдався, буде звичайне затемнення: {type(e).__name__}: {e}")
            try:
                win.attributes("-alpha", 0.32)
            except runtime.tk.TclError:
                runtime.log_exc('word_lookup.__init__')

        self.hover_enabled = bool(runtime.cfg["freeze"].get("hover_highlight", True)) and self.frozen is not None
        hl_color = runtime.cfg["popup"]["highlight_color"]
        self.hl_rect = c.create_rectangle(0, 0, 0, 0, fill=hl_color, stipple="gray50", outline=hl_color,
                                          state="hidden")  # підсвітка слова під курсором
        self.vline = c.create_line(0, 0, 0, h, fill="#ffd43b", state="hidden")  # перехрестя вимкнено:
        self.hline = c.create_line(0, 0, w, 0, fill="#ffd43b", state="hidden")  # курсор — звичайна стрілка
        self.rect = c.create_rectangle(0, 0, 0, 0, outline="#ffd43b", width=2, state="hidden")
        self.size = c.create_text(0, 0, text="", fill="#ffd43b", anchor="sw",
                                  font=(runtime.FONT, 11, "bold"), state="hidden")
        hint = ("⏸ Стоп-кадр — екран заморожено, підказки (тултіпи) нікуди не зникнуть.  "
                if frozen_ok else "") + \
               ("Наведи курсор на слово — воно підсвітиться  ·  клік лівою кнопкою — шукати  ·  "
                "або обведи рамкою кілька слів  ·  Esc / права кнопка — скасувати"
                if self.hover_enabled else
                "Тягни рамку навколо слова чи рядка  ·  клік — одне слово  ·  Esc / права кнопка — скасувати")
        if app._game_pause is not None and app._game_pause.active:
            hint = "⏸ Процес гри призупинений · Esc — відновити.  " + hint
        c.create_text(-x + r.winfo_screenwidth() // 2, -y + 34, fill="#f1f1f5",
                      font=(runtime.FONT, 13, "bold"), text=hint, width=r.winfo_screenwidth() - 80)
        c.bind("<Motion>", self._motion)
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Button-3>", lambda _e: self.finish(None))
        win.bind("<Escape>", lambda _e: self.finish(None))
        win.focus_force()
        c.focus_set()
        if self.hover_enabled:
            win.after(50, self._poll)
        runtime.release_cursor_clip()  # гра тримає курсор у центрі — відпускаємо, щоб він рухався
        win.after(100, self._unclip_tick)

    def _unclip_tick(self):
        """Деякі ігри самі повертають обмеження курсора — відпускаємо знову, поки накладка відкрита."""
        if self.done:
            return
        runtime.release_cursor_clip()
        try:
            self.win.after(100, self._unclip_tick)
        except Exception:
            runtime.log_exc('word_lookup._unclip_tick')

    # ---------- підсвітка слова під курсором ----------
    @staticmethod
    def _inside(box, x, y, margin=4) -> bool:
        return box[0] - margin <= x <= box[2] + margin and box[1] - margin <= y <= box[3] + margin

    def _clear_hover(self):
        self.hover_hit = None
        self.canvas.itemconfigure(self.hl_rect, state="hidden")

    def _set_hover(self, hit):
        self.hover_hit = hit
        bx0, by0, bx1, by1 = hit["box"]
        self.canvas.coords(self.hl_rect, bx0 - self.ox - 2, by0 - self.oy - 1,
                           bx1 - self.ox + 2, by1 - self.oy + 1)
        self.canvas.itemconfigure(self.hl_rect, state="normal")

    def _motion(self, e):
        self.mouse = (e.x_root, e.y_root)
        if not self.hover_enabled or self.start:  # під час перетягування рамки не розпізнаємо
            return
        if self.hover_hit and not self._inside(self.hover_hit["box"], e.x_root, e.y_root):
            self._clear_hover()
        if self.hover_hit is None:
            if self._after_id is not None:
                try:
                    self.win.after_cancel(self._after_id)
                except Exception:
                    runtime.log_exc('word_lookup._motion')
            x, y = e.x_root, e.y_root
            self._after_id = self.win.after(self.HOVER_DELAY_MS, lambda: self._request(x, y))

    def _request(self, x, y):
        self._after_id = None
        if self.done:
            return
        with self._lock:
            self._req = (x, y)
            spawn = not self._busy
            self._busy = True
        if spawn:
            runtime.threading.Thread(target=self._ocr_worker, daemon=True).start()

    def _ocr_worker(self):
        """Один фоновий потік: бере лише НАЙНОВІШИЙ запит, старі пропускає."""
        while True:
            with self._lock:
                req, self._req = self._req, None
                if req is None:
                    self._busy = False
                    return
            try:
                hit = runtime.find_word_at(req[0], req[1], frozen=self.frozen)
            except Exception:
                hit = None
            self._hq.put((req, hit))

    def _poll(self):
        if self.done:
            return
        latest = None
        while True:
            try:
                latest = self._hq.get_nowait()
            except runtime.queue.Empty:
                break
        if latest is not None:
            (rx, ry), hit = latest
            mx, my = self.mouse
            if abs(rx - mx) <= 12 and abs(ry - my) <= 12:  # відповідь ще актуальна
                if hit:
                    self._set_hover(hit)
                else:
                    self._clear_hover()
        try:
            self.win.after(50, self._poll)
        except Exception:
            runtime.log_exc('word_lookup._poll')

    def _move(self, e):
        self.canvas.coords(self.vline, e.x, 0, e.x, self.h)
        self.canvas.coords(self.hline, 0, e.y, self.w, e.y)

    def _press(self, e):
        self.start = (e.x, e.y, e.x_root, e.y_root)
        self.canvas.itemconfigure(self.rect, state="normal")

    def _drag(self, e):
        self._move(e)
        if self.start:
            sx, sy = self.start[:2]
            self.canvas.coords(self.rect, sx, sy, e.x, e.y)
            self.canvas.coords(self.size, min(sx, e.x), min(sy, e.y) - 4)
            self.canvas.itemconfigure(self.size, text=f"{abs(e.x - sx)}×{abs(e.y - sy)}",
                                      state="normal")

    def _release(self, e):
        if not self.start:
            return
        x0, x1 = sorted((self.start[2], e.x_root))
        y0, y1 = sorted((self.start[3], e.y_root))
        if x1 - x0 < 6 and y1 - y0 < 6:
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
        frozen = self.frozen
        if getattr(self, "_after_id", None) is not None:
            try:
                self.win.after_cancel(self._after_id)
            except Exception:
                runtime.log_exc('word_lookup.finish')
        try:
            _cancel_view_timers(self.win)
            self.win.destroy()
        except Exception:
            runtime.log_exc('word_lookup.finish')
        runtime._restore_foreground(self._prev_hwnd)
        self.on_done(result, frozen)


class FreeCursorOverlay:
    """Приціл для ігор, де миша захоплена грою (raw input) і системний курсор не
    рухається (напр. Cyberpunk 2077). Керування йде ЗЗОВНІ (через App): стрілки/WASD —
    з глобального хуку клавіатури, рух — від фізичної миші (метод nudge). Це вікно
    саме по собі НЕ приймає клавіші — звичайні Tk-прив'язки клавіш працюють лише
    коли вікно у фокусі, а повноекранні ігри фокус не віддають, тому раніше приціл
    «не рухався». Глобальний хук працює незалежно від фокусу."""

    STEP, STEP_FAST = 8, 40

    def __init__(self, app, start_pos, on_done):
        self.on_done, self.done = on_done, False
        self.frozen = None
        self._prev_hwnd = runtime._foreground_window()  # повернути фокус грі після закриття
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
            image, sx, sy, *_ = runtime._grab(x, y, x + w, y + h)
            self.frozen = (image, sx, sy)
        finally:
            app._resume_game()

        self.win = win = runtime.tk.Toplevel(r)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        self.rounded = False
        if runtime.sys.platform.startswith("win"):
            try:
                win.attributes("-transparentcolor", runtime.KEY)  # майже невидиме вікно — видно лише приціл
                self.rounded = True
            except runtime.tk.TclError:
                runtime.log_exc('word_lookup.__init__')
        bg = runtime.KEY if self.rounded else "black"
        if not self.rounded and self.frozen is None:
            try:
                win.attributes("-alpha", 0.05)
            except runtime.tk.TclError:
                runtime.log_exc('word_lookup.__init__')
        win.configure(bg=bg)
        win.geometry(f"{w}x{h}+{x}+{y}")
        c = self.canvas = runtime.tk.Canvas(win, bg=bg, highlightthickness=0)
        c.pack(fill="both", expand=True)
        from PIL import ImageTk
        self._photo = ImageTk.PhotoImage(self.frozen[0], master=win)
        c.create_image(0, 0, image=self._photo, anchor="nw")

        c.create_text(w // 2, 30, fill="#f1f1f5", font=(runtime.FONT, 13, "bold"),
                      text="Рухай мишею або стрілками/WASD · Enter/Пробіл — шукати слово · "
                           "Esc — скасувати")
        R = 14
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
        runtime.release_cursor_clip()  # інакше миша «застрягає» в центрі й нічого не рухає
        win.after(100, self._unclip_tick)

    def _unclip_tick(self):
        if self.done:
            return
        runtime.release_cursor_clip()
        try:
            self.win.after(100, self._unclip_tick)
        except Exception:
            runtime.log_exc('word_lookup._unclip_tick')

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
            runtime.log_exc('word_lookup.finish')
        runtime._restore_foreground(self._prev_hwnd)
        self.on_done(result, self.frozen)


def _get(path):
    d = runtime.cfg
    for p in path:
        d = d[p]
    return d


def _put(path, val):
    d = runtime.cfg
    for p in path[:-1]:
        d = d[p]
    d[path[-1]] = val


class ReviewWindow:
    """Спочатку згадати, потім перевірити; складні слова — ще раз. Без мережі."""

    def __init__(self, app, entries):
        self.app, self.pending = app, list(entries)
        self.total, self.completed, self.revealed = len(entries), 0, False
        self.win = runtime.tk.Toplevel(app.root)
        self.win.title("Повторення слів — Word Lookup")
        self.win.configure(bg="#111318")
        self.win.geometry("680x660")
        self.win.minsize(540, 380)
        self.progress = runtime.tk.StringVar()
        runtime.ttk.Label(self.win, textvariable=self.progress, padding=12).pack(fill="x")
        self.mode = runtime.tk.StringVar(value="Англійська → Переклад")
        mode_row = runtime.ttk.Frame(self.win, padding=(12, 0, 12, 8))
        mode_row.pack(fill="x")
        mode_box = runtime.ttk.Combobox(mode_row, textvariable=self.mode, state="readonly", width=32,
                                values=["Англійська → Переклад", "Переклад → Англійська", "Пропуск у реченні"])
        mode_box.pack(side="left")
        mode_box.bind("<<ComboboxSelected>>", lambda _e: self.next_card())
        self.panel = runtime.ScrolledPanel(self.win)
        self.panel.pack(fill="both", expand=True, padx=12)
        self.body = self.panel.content
        buttons = runtime.ttk.Frame(self.win, padding=12)
        buttons.pack(fill="x")
        self.reveal_btn = runtime.ttk.Button(buttons, text="Показати відповідь (Пробіл)", style="Accent.TButton", command=self.reveal)
        self.reveal_btn.pack(fill="x", pady=(0, 8))
        self.again_btn = runtime.ttk.Button(buttons, text="1 · Ще раз", command=lambda: self.grade(False))
        self.again_btn.pack(side="left", fill="x", expand=True)
        self.known_btn = runtime.ttk.Button(buttons, text="2 · Знаю", command=lambda: self.grade(True))
        self.known_btn.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.win.bind("<space>", lambda _e: self.reveal())
        self.win.bind("<Key-1>", lambda _e: self.grade(False))
        self.win.bind("<Key-2>", lambda _e: self.grade(True))
        self.win.bind("<Escape>", lambda _e: self.win.destroy())
        self.next_card()

    def _label(self, text, font=None):
        font = font or (runtime.FONT, 12)
        runtime.ttk.Label(self.body, text=text, font=font, wraplength=550, justify="left"
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
            self._label("Повторення завершено!", font=(runtime.FONT, 22, "bold"))
            self._label("Усі слова цього набору пройдено. Можна повернутися до гри.")
            return
        entry = self.pending[0]
        prompt = entry["word"]
        if self.mode.get() == "Переклад → Англійська":
            prompt = entry.get("translation") or prompt
        elif self.mode.get() == "Пропуск у реченні":
            sentence = entry.get("context") or entry.get("example") or ""
            pattern = r"(?<!\w)(?:" + "|".join(runtime.re.escape(t) for t in sorted(
                {entry["word"], entry.get("base") or entry["word"]}, key=len, reverse=True)) + r")(?!\w)"
            prompt, count = runtime.re.subn(pattern, "______", sentence, flags=runtime.re.I)
            if not count:
                prompt = entry["word"]
        self._label(prompt, font=(runtime.FONT, 22, "bold"))
        self._label("Згадай переклад, потім відкрий відповідь.")

    def reveal(self):
        if not self.pending or self.revealed:
            return "break"
        self.revealed = True
        e = self.pending[0]
        if self.mode.get() != "Англійська → Переклад":
            self._label(e["word"], font=(runtime.FONT, 20, "bold"))
        self._label(e.get("translation", "") or "Переклад не збережено", font=(runtime.FONT, 18, "bold"))
        for key in ("definition", "definition_tr", "example", "context", "context_tr"):
            if e.get(key):
                self._label(e[key])
        self.photo = runtime.context_photo(e.get("image_file", ""), self.win)
        if self.photo:
            runtime.ttk.Label(self.body, image=self.photo).pack(padx=12, pady=8)
        self.again_btn.state(["!disabled"])
        self.known_btn.state(["!disabled"])
        self.reveal_btn.state(["disabled"])
        return "break"

    def grade(self, known):
        if not self.pending or not self.revealed:
            return "break"
        e = self.pending[0]
        if runtime.saved_entry(e["word"]) is None:
            self._label("Цю картку видалено з бібліотеки.")
            self.pending.pop(0)
            self.next_card()
            return "break"
        try:
            runtime.record_review(runtime.saved_entry(e["word"]), known)
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


class AppViewMixin:
    """Settings, library/editor and appearance views for the application controller."""

    def _schedule_config_save(self):
        if self._config_save_timer is not None:
            self.root.after_cancel(self._config_save_timer)
        self._config_save_timer = self.root.after(300, self._flush_config_save)

    def _flush_config_save(self):
        if self._config_save_timer is not None:
            self.root.after_cancel(self._config_save_timer)
            self._config_save_timer = None
            saved = runtime.save_config(runtime.cfg)
            if hasattr(self, "deepl_var") and runtime.cfg["deepl"]["key"]:
                self.deepl_var.set(runtime._credential_status if saved else
                                   "Не вдалося зберегти налаштування. Дивись журнал.")

    def _var(self, cls, path, mapping=None, after=None):
        path = tuple(path)
        if path in self._setting_vars:
            return self._setting_vars[path]
        inv = {v: k for k, v in mapping.items()} if mapping else None
        init = runtime._get(path)
        var = cls(value=inv.get(init, init) if inv else init)

        def cb(*_):
            try:
                val = var.get()
            except runtime.tk.TclError:
                return
            if mapping:
                if val not in mapping:
                    return
                val = mapping[val]
            runtime._put(path, val)
            self._schedule_config_save()
            if after:
                after()
        var.trace_add("write", cb)
        self._setting_vars[path] = var
        return var

    def _combo(self, parent, var, mapping, width=26):
        return runtime.ttk.Combobox(parent, textvariable=var, values=list(mapping),
                            state="readonly", width=width)

    def _apply_style(self):
        bg, panel, field, fg, dim, accent = "#111318", "#1d2029", "#272b36", "#f1f1f5", "#a2a8b8", "#ffd43b"
        self.root.configure(bg=bg)
        self.root.option_add("*TCombobox*Listbox.background", field)
        self.root.option_add("*TCombobox*Listbox.foreground", fg)
        self.root.option_add("*TCombobox*Listbox.selectBackground", accent)
        self.root.option_add("*TCombobox*Listbox.selectForeground", bg)
        style = runtime.ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=(runtime.FONT, 10), background=bg, foreground=fg,
                        troughcolor=panel, bordercolor=field, lightcolor=field, darkcolor=field)
        style.configure("TButton", background=field, bordercolor=field, relief="flat",
                        padding=(12, 8), focusthickness=1, focuscolor=accent)
        style.map("TButton", background=[("active", "#353a48"), ("pressed", "#41485a")],
                  foreground=[("disabled", "#73798a")])
        style.configure("Accent.TButton", foreground=bg, background=accent, font=(runtime.FONT, 10, "bold"))
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
        style.configure("TLabelframe.Label", font=(runtime.FONT, 10, "bold"), foreground=dim)
        style.configure("TEntry", fieldbackground=field, foreground=fg, insertcolor=accent, padding=7)
        style.configure("TCombobox", fieldbackground=field, foreground=fg, arrowcolor=dim, padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", field)], foreground=[("readonly", fg)])
        style.configure("TSpinbox", fieldbackground=field, foreground=fg, arrowcolor=dim, padding=5)
        style.configure("TCheckbutton", background=bg, foreground=fg, padding=3)
        style.map("TCheckbutton", background=[("active", bg)], indicatorcolor=[("selected", accent)])
        style.configure("Filter.TRadiobutton", background=panel, foreground=dim, padding=(9, 6))
        style.map("Filter.TRadiobutton", foreground=[("selected", accent)], background=[("active", field)])
        style.configure("Treeview", rowheight=48, fieldbackground=panel, background=panel,
                        foreground=fg, borderwidth=0, font=(runtime.FONT, 11))
        style.configure("Treeview.Heading", font=(runtime.FONT, 9), foreground=dim, background=panel, padding=8)
        style.map("Treeview", background=[("selected", "#303644")], foreground=[("selected", fg)])
        style.configure("Vertical.TScrollbar", background=field, troughcolor=bg, arrowcolor=dim, borderwidth=0)

    def _build_ui(self):
        self.root.geometry(f"1060x{min(790, self.root.winfo_screenheight() - 60)}")
        self.root.minsize(860, 540)
        shell = runtime.ttk.Frame(self.root)
        shell.pack(fill="both", expand=True)
        sidebar = runtime.ttk.Frame(shell, style="Sidebar.TFrame", width=225, padding=(18, 24))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        runtime.ttk.Label(sidebar, text="Word Lookup", font=(runtime.FONT, 16, "bold"),
                  style="Sidebar.TLabel", foreground="#f1f1f5").pack(anchor="w")
        runtime.ttk.Label(sidebar, text="Слова з твоїх світів", style="Sidebar.TLabel").pack(anchor="w", pady=(5, 24))
        runtime.ttk.Button(sidebar, text="Бібліотека →", style="Accent.TButton",
                   command=self.open_saved_words).pack(fill="x", pady=(0, 22))
        runtime.ttk.Label(sidebar, text="НАЛАШТУВАННЯ", style="Sidebar.TLabel", font=(runtime.FONT, 9)).pack(anchor="w", pady=(0, 8))
        body = runtime.ttk.Frame(shell, padding=(26, 22, 20, 16))
        body.pack(side="left", fill="both", expand=True)
        heading = runtime.ttk.Frame(body)
        heading.pack(fill="x", pady=(0, 14))
        self.page_title = runtime.tk.StringVar(value="Головна")
        runtime.ttk.Label(heading, textvariable=self.page_title, font=(runtime.FONT, 22, "bold")).pack(side="left")
        runtime.ttk.Button(heading, text="Показати картку", command=self.show_preview).pack(side="right")
        self.notebook = nb = runtime.ttk.Notebook(body, style="Sidebar.TNotebook")
        nb.pack(fill="both", expand=True)
        tabs = {}
        for key, title in (("main", "Головна"), ("keys", "Клавіші та миша"),
                           ("look", "Картка перекладу"), ("dict", "Словник і OCR")):
            panel = runtime.ScrolledPanel(nb, bg=self.root.cget("background"))
            f = runtime.ttk.Frame(panel.content, padding=(0, 0, 14, 20))
            f.pack(fill="both", expand=True)
            nb.add(panel, text=title)
            tabs[key] = f
            runtime.ttk.Button(sidebar, text=title, style="Sidebar.TButton",
                       command=lambda p=panel: nb.select(p)).pack(fill="x", pady=2)
            if key == "look":
                self.look_panel = panel
        nb.bind("<<NotebookTabChanged>>", lambda _e: self.page_title.set(nb.tab(nb.select(), "text")))
        runtime.ttk.Label(sidebar, text="Перекладай. Зберігай.\nПовертайся до слів.",
                  style="Sidebar.TLabel", justify="left").pack(side="bottom", anchor="w", pady=(20, 0))
        self._tab_main(tabs["main"])
        self._tab_keys(tabs["keys"])
        self._tab_look(tabs["look"])
        self._tab_dict(tabs["dict"])

    def copy_log(self):
        report = runtime.diagnostic_report()
        try:
            runtime.pyperclip.copy(report)
        except Exception as e:
            runtime.messagebox.showinfo(runtime.APP_NAME, f"Не вдалося скопіювати: {e}")
            return
        runtime.messagebox.showinfo(runtime.APP_NAME, f"Журнал скопійовано в буфер обміну ({report.count(chr(10)) + 1} "
                                      "рядків). Встав його в чат (Ctrl+V).")

    def open_log(self):
        if not runtime.os.path.exists(runtime.log_path()):
            runtime.messagebox.showinfo(runtime.APP_NAME, "Журнал ще порожній.")
            return
        runtime.open_file(runtime.log_path())

    def _tab_main(self, f):
        top = runtime.ttk.Frame(f)
        top.pack(fill="x")
        self.status_var = runtime.tk.StringVar()
        runtime.ttk.Label(top, textvariable=self.status_var, font=(runtime.FONT, 11, "bold")).pack(side="left")
        runtime.ttk.Button(top, text="Пауза / Продовжити", command=self.toggle_pause).pack(side="right")

        self.hint_var = runtime.tk.StringVar()
        runtime.ttk.Label(f, textvariable=self.hint_var, wraplength=560, justify="left").pack(
            anchor="w", pady=(12, 0))

        ocr = runtime.ttk.LabelFrame(f, text="Розпізнавання тексту (OCR)", padding=10)
        ocr.pack(fill="x", pady=(12, 0))
        self.ocr_var = runtime.tk.StringVar()
        runtime.ttk.Label(ocr, textvariable=self.ocr_var, wraplength=540, justify="left").pack(anchor="w")
        brow = runtime.ttk.Frame(ocr)
        brow.pack(anchor="w", pady=(6, 0))
        runtime.ttk.Button(brow, text="Перевірити ще раз", command=self._check_ocr).pack(side="left")
        runtime.ttk.Button(brow, text="Діагностика OCR (3 с)", command=self.diagnose_ocr
                   ).pack(side="left", padx=(8, 0))
        runtime.ttk.Label(ocr, text="Діагностика: натисни, наведи курсор на текст — за 3 секунди буде "
                            "знімок і звіт, що саме бачить OCR.", foreground="#a2a8b8",
                  wraplength=540).pack(anchor="w", pady=(4, 0))

        lg = runtime.ttk.LabelFrame(f, text="Журнал (щоб розібратись, що пішло не так)", padding=10)
        lg.pack(fill="x", pady=(12, 0))
        lrow = runtime.ttk.Frame(lg)
        lrow.pack(anchor="w")
        runtime.ttk.Button(lrow, text="📋 Скопіювати журнал", command=self.copy_log).pack(side="left")
        runtime.ttk.Button(lrow, text="Відкрити файл журналу", command=self.open_log
                   ).pack(side="left", padx=(8, 0))
        runtime.ttk.Label(lg, text="Після збою натисни «Скопіювати» і встав у чат: там події пошуку, помилки й "
                           "налаштування (ключ DeepL приховано). Файл обмежений ~600 КБ.",
                  foreground="#a2a8b8", wraplength=540).pack(anchor="w", pady=(4, 0))

        dic = runtime.ttk.LabelFrame(f, text="Переклад і словник (English – визначення, синоніми)",
                             padding=10)
        dic.pack(fill="x", pady=(12, 0))
        self.dict_var = runtime.tk.StringVar(value="Ще не перевірялось.")
        runtime.ttk.Label(dic, textvariable=self.dict_var, wraplength=540, justify="left").pack(anchor="w")
        runtime.ttk.Button(dic, text="Діагностика словника", command=self.diagnose_dict).pack(
            anchor="w", pady=(6, 0))
        runtime.ttk.Label(dic, text="Якщо в грі показується лише переклад без визначень і синонімів "
                            "(секцій English / English Thesaurus), ця перевірка покаже, чи "
                            "доступний dictionaryapi.dev з твоєї мережі.", foreground="#a2a8b8",
                  wraplength=540).pack(anchor="w", pady=(4, 0))

        test = runtime.ttk.LabelFrame(f, text="Спробувати", padding=10)
        test.pack(fill="x", pady=(12, 0), before=ocr)
        self.test_var = runtime.tk.StringVar(value="understatement")
        self.test_entry = runtime.ttk.Entry(test, textvariable=self.test_var)
        self.test_entry.pack(side="left", fill="x", expand=True)
        self.test_entry.bind("<Return>", lambda _e: self.test_lookup())
        runtime.ttk.Button(test, text="Знайти", command=self.test_lookup).pack(side="left", padx=(8, 0))
        self.preview_btn = runtime.ttk.Button(test, text="Показати приклад", command=self.show_preview)
        self.preview_btn.pack(side="left", padx=(8, 0))

        runtime.ttk.Button(f, text="✂  Виділити область на екрані (як скріншот)",
                   command=self.start_region_select).pack(anchor="w", pady=(12, 0))
        runtime.ttk.Label(f, text="Екран одразу заморожується на місці — тому підказка (тултіп), яка "
                        "в грі зникає при русі миші, залишиться на скріншоті, поки ти спокійно "
                        "виділяєш потрібне слово.", foreground="#a2a8b8", wraplength=520
                  ).pack(anchor="w", pady=(2, 0))
        runtime.ttk.Checkbutton(f, text="Підсвічувати слово під курсором на застиглому екрані (клік — шукати)",
                        variable=self._var(runtime.tk.BooleanVar, ("freeze", "hover_highlight"))
                        ).pack(anchor="w", pady=(6, 0))
        pause_box = runtime.ttk.LabelFrame(f, text="Пауза гри під час вибору й перекладу (Windows)", padding=10)
        pause_box.pack(fill="x", pady=(10, 0))
        runtime.ttk.Checkbutton(pause_box, text="Призупиняти гру на час вибору й перекладу",
                        variable=self._var(runtime.tk.BooleanVar, ("freeze", "pause_game"),
                                           after=self._pause_option_changed)).pack(anchor="w")
        runtime.ttk.Checkbutton(pause_box, text="Автоматично визначати активну гру",
                        variable=self._var(runtime.tk.BooleanVar, ("freeze", "auto_game"),
                                           after=self._game_mode_changed)).pack(anchor="w", pady=(4, 0))
        process_row = runtime.ttk.Frame(pause_box)
        process_row.pack(fill="x", pady=6)
        runtime.ttk.Label(process_row, text="Вручну (.exe):").pack(side="left")
        self.game_exe_entry = runtime.ttk.Entry(process_row,
            textvariable=self._var(runtime.tk.StringVar, ("freeze", "game_exe")), width=28)
        self.game_exe_entry.pack(side="left", padx=6)
        self.game_detection_var = runtime.tk.StringVar(value="Гра визначається при натисканні гарячої клавіші у її вікні.")
        runtime.ttk.Label(pause_box, textvariable=self.game_detection_var, wraplength=520).pack(anchor="w", pady=4)
        runtime.ttk.Button(pause_box, text="Перевірити визначення через 3 с",
                   command=self.schedule_game_detection).pack(anchor="w", pady=(0, 6))
        self._game_mode_changed()
        limit_row = runtime.ttk.Frame(pause_box)
        limit_row.pack(fill="x")
        runtime.ttk.Label(limit_row, text="Відновити автоматично через, с:").pack(side="left")
        runtime.ttk.Spinbox(limit_row, from_=5, to=300, width=6,
                    textvariable=self._var(runtime.tk.IntVar, ("freeze", "pause_seconds"))).pack(side="left", padx=6)
        runtime.ttk.Button(pause_box, text="Відновити гру зараз",
                   command=lambda: self.do_action("resume_game")).pack(anchor="w", pady=(6, 0))
        runtime.ttk.Label(pause_box, text="Повернись у гру й натисни Ctrl+Alt+R або Ctrl+Alt+G. "
                                  "Гра лишається на паузі до закриття перекладу / Esc. "
                                  "Лише для однокористувацьких ігор; сервер онлайн-гри не зупиняється.",
                  wraplength=520, foreground="#a2a8b8").pack(anchor="w", pady=(6, 0))

        fc = runtime.ttk.LabelFrame(f, text="Гра з захопленою мишею (наприклад, Cyberpunk 2077)", padding=10)
        fc.pack(fill="x", pady=(10, 0))
        runtime.ttk.Button(fc, text="🎯  Керований курсор (миша + клавіатура)",
                   command=self.start_free_cursor).pack(anchor="w")
        runtime.ttk.Label(fc, text="Якщо миша прив'язана до камери в грі й наведення курсора не працює: "
                          "натисни цю кнопку (або гарячу клавішу) — з'явиться приціл, який можна "
                          "рухати фізичною мишею або стрілками/WASD (Shift — швидше), Enter/Пробіл — "
                          "знайти слово, Esc — скасувати. Працює незалежно від того, чи гра тримає "
                          "фокус вікна, і незалежно від системного курсора миші.",
                  foreground="#a2a8b8", wraplength=520
                  ).pack(anchor="w", pady=(4, 0))

        self.saved_btn = runtime.ttk.Button(f, command=self.open_saved_words)
        self.saved_btn.pack(anchor="w", pady=(8, 0))
        runtime.ttk.Checkbutton(f, text="Зберігати скриншот контексту зі словом (локально, для повторення)",
                        variable=self._var(runtime.tk.BooleanVar, ("learning", "capture_context"))
                        ).pack(anchor="w", pady=(6, 0))

        cache = runtime.ttk.LabelFrame(f, text="Кеш на диску", padding=10)
        cache.pack(fill="x", pady=(12, 0))
        self.cache_var = runtime.tk.StringVar()
        runtime.ttk.Label(cache, textvariable=self.cache_var).grid(row=0, column=0, columnspan=3, sticky="w")
        runtime.ttk.Label(cache, text="Зберігати до").grid(row=1, column=0, sticky="w", pady=(6, 0))
        runtime.ttk.Spinbox(cache, from_=0, to=5000, increment=100, width=6,
                    textvariable=self._var(runtime.tk.IntVar, ("cache", "max_entries"))
                    ).grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        runtime.ttk.Label(cache, text="слів (0 — не кешувати)").grid(row=1, column=2, sticky="w", pady=(6, 0))
        runtime.ttk.Button(cache, text="Очистити кеш зараз", command=self.clear_cache
                   ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        runtime.ttk.Label(cache, text="Кожне вже знайдене слово зберігається на диску, щоб наступного "
                             "разу відкриватись миттєво й без інтернету. Розмір обмежений і "
                             "зазвичай не перевищує кілька мегабайтів.", foreground="#a2a8b8",
                  wraplength=520).grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        runtime.ttk.Checkbutton(f, text="Запускати згорнутою",
                        variable=self._var(runtime.tk.BooleanVar, ("app", "start_minimized"))
                        ).pack(anchor="w", pady=(12, 0))
        bottom = runtime.ttk.Frame(f)
        bottom.pack(fill="x", side="bottom", pady=(14, 0))
        runtime.ttk.Button(bottom, text="Згорнути", command=self.hide_window).pack(side="left")
        runtime.ttk.Button(bottom, text="Вийти з програми", command=self.quit).pack(side="right")

    def _tab_keys(self, f):
        abox = runtime.ttk.LabelFrame(f, text="Автоматичний словник при виділенні тексту", padding=10)
        abox.pack(fill="x")
        runtime.ttk.Checkbutton(abox, text="Показувати переклад/словник одразу, щойно виділено текст "
                                   "(без гарячої клавіші)",
                        variable=self._var(runtime.tk.BooleanVar, ("auto", "enabled"),
                                           after=self._refresh_hint)
                        ).grid(row=0, column=0, columnspan=2, sticky="w")
        runtime.ttk.Label(abox, text="Виділи слово чи фразу мишею (перетягуванням) або клацни двічі по "
                            "слову — і картка з'явиться сама. Працює там, де текст можна "
                            "виділити насправді: у браузері, PDF, Word тощо. Для гри, де текст "
                            "не виділити, лишається пошук по наведенню курсора вище.",
                  foreground="#a2a8b8", wraplength=540).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        mbox = runtime.ttk.LabelFrame(f, text="Кнопка миші для пошуку слова (наведення, як в іграх)", padding=10)
        mbox.pack(fill="x", pady=(12, 0))
        runtime.ttk.Label(mbox, text="Кнопка:").grid(row=0, column=0, sticky="w")
        self._combo(mbox, self._var(runtime.tk.StringVar, ("mouse", "button"), runtime.MOUSE_BTN_NAMES,
                                    self._refresh_hint), runtime.MOUSE_BTN_NAMES
                    ).grid(row=0, column=1, padx=8, pady=3, sticky="w")
        runtime.ttk.Label(mbox, text="Разом із клавішею:").grid(row=1, column=0, sticky="w")
        self._combo(mbox, self._var(runtime.tk.StringVar, ("mouse", "modifier"), runtime.MOUSE_MOD_NAMES,
                                    self._refresh_hint), runtime.MOUSE_MOD_NAMES
                    ).grid(row=1, column=1, padx=8, pady=3, sticky="w")
        runtime.ttk.Label(mbox, text="Клік передається і грі/програмі — вибери кнопку, яка там не потрібна.",
                  foreground="#a2a8b8").grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        kbox = runtime.ttk.LabelFrame(f, text="Гарячі клавіші", padding=10)
        kbox.pack(fill="x", pady=(12, 0))
        runtime.ttk.Label(kbox, text="Натисни «Записати», потім комбінацію. Esc — скасувати.",
                  foreground="#a2a8b8").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        self.key_vars, self.key_btns = {}, {}
        for i, (action, label) in enumerate(runtime.ACTIONS, start=1):
            runtime.ttk.Label(kbox, text=label).grid(row=i, column=0, sticky="w", pady=3)
            v = runtime.tk.StringVar(value=runtime.pretty(runtime.cfg["hotkeys"][action]))
            self.key_vars[action] = v
            runtime.ttk.Label(kbox, textvariable=v, width=20, anchor="center",
                      relief="sunken").grid(row=i, column=1, padx=8)
            b = runtime.ttk.Button(kbox, text="Записати", command=lambda a=action: self.start_record(a))
            b.grid(row=i, column=2)
            self.key_btns[action] = b
            runtime.ttk.Button(kbox, text="Очистити",
                       command=lambda a=action: self.set_hotkey(a, "")).grid(row=i, column=3, padx=(6, 0))
        runtime.ttk.Button(f, text="Скинути гарячі клавіші до стандартних",
                   command=self.reset_hotkeys).pack(anchor="w", pady=(12, 0))

    def _tab_look(self, f):
        after = self._refresh_preview
        runtime.ttk.Label(f, text="Збери зручну картку", font=(runtime.FONT, 13, "bold")).pack(anchor="w")
        runtime.ttk.Label(f, text="Зміни одразу видно у відкритій картці. Власні переклади залишаються збереженими.",
                  foreground="#a2a8b8", wraplength=570).pack(anchor="w", pady=(4, 10))
        presets = runtime.ttk.Frame(f)
        presets.pack(fill="x", pady=(0, 12))
        for key, label in (("compact", "Компактна"), ("balanced", "Збалансована"),
                           ("detailed", "Докладна")):
            runtime.ttk.Button(presets, text=label, command=lambda k=key: self.apply_card_preset(k)
                       ).pack(side="left", padx=(0, 6))
        g = runtime.ttk.LabelFrame(f, text="Розмір і оформлення", padding=10)
        g.pack(fill="x")
        rows = [
            ("Тема", lambda p: self._combo(p, self._var(runtime.tk.StringVar, ("popup", "theme"),
                                                        runtime.THEME_NAMES, after), runtime.THEME_NAMES, 18)),
            ("Розмір шрифту", lambda p: runtime.ttk.Spinbox(p, from_=9, to=24, width=6,
                                                   textvariable=self._var(runtime.tk.IntVar, ("popup", "font_size"), after=after))),
            ("Ширина картки (px)", lambda p: runtime.ttk.Spinbox(p, from_=300, to=900, increment=20, width=6,
                                                         textvariable=self._var(runtime.tk.IntVar, ("popup", "width"), after=after))),
            ("Прозорість", lambda p: runtime.ttk.Scale(p, from_=0.5, to=1.0, length=160,
                                               variable=self._var(runtime.tk.DoubleVar, ("popup", "opacity"), after=after))),
            ("Закривати через (с, 0 — ні)", lambda p: runtime.ttk.Spinbox(p, from_=0, to=300, width=6,
                                                                  textvariable=self._var(runtime.tk.IntVar, ("popup", "timeout")))),
        ]
        for i, (text, make) in enumerate(rows):
            runtime.ttk.Label(g, text=text).grid(row=i, column=0, sticky="w", pady=4)
            make(g).grid(row=i, column=1, sticky="w", padx=10)
        r = len(rows)
        runtime.ttk.Label(g, text="Колір підсвічування").grid(row=r, column=0, sticky="w", pady=4)
        self.color_btn = runtime.tk.Button(g, width=8, bg=runtime.cfg["popup"]["highlight_color"],
                                   command=self.pick_color)
        self.color_btn.grid(row=r, column=1, sticky="w", padx=10)
        content = runtime.ttk.LabelFrame(f, text="Що показувати в картці", padding=10)
        content.pack(fill="x", pady=(12, 0))
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        for i, (path, label) in enumerate(runtime.CARD_CONTENT_OPTIONS):
            runtime.ttk.Checkbutton(content, text=label,
                            variable=self._var(runtime.tk.BooleanVar, path, after=after)
                            ).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 10), pady=4)
        runtime.ttk.Label(content, text="Приклади й переклад визначення видно, коли увімкнені визначення.",
                  foreground="#a2a8b8", wraplength=540).grid(
                      row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))
        runtime.ttk.Checkbutton(f, text="Підсвічувати знайдене слово на екрані",
                        variable=self._var(runtime.tk.BooleanVar, ("ocr", "highlight"))
                        ).pack(anchor="w", pady=(10, 0))
        runtime.ttk.Button(f, text="Показати приклад картки", style="Accent.TButton",
                   command=self.show_preview).pack(
            anchor="w", pady=(14, 0))

    def open_card_settings(self):
        self.show_window()
        self.notebook.select(self.look_panel)

    def apply_card_preset(self, name):
        if name not in ("compact", "balanced", "detailed"):
            return
        compact = name == "compact"
        for path, _label in runtime.CARD_CONTENT_OPTIONS:
            visible = not compact or path not in {
                ("sections", "definitions"), ("sections", "thesaurus"),
                ("popup", "alternate_translations"), ("popup", "context_image")}
            self._var(runtime.tk.BooleanVar, path, after=self._refresh_preview).set(visible)
        self._var(runtime.tk.IntVar, ("popup", "font_size"), after=self._refresh_preview).set(
            11 if name == "detailed" else 10)
        self._var(runtime.tk.IntVar, ("popup", "width"), after=self._refresh_preview).set(
            {"compact": 360, "balanced": 400, "detailed": 460}[name])

    def _tab_dict(self, f):
        box = runtime.ttk.LabelFrame(f, text="Словник", padding=10)
        box.pack(fill="x")
        runtime.ttk.Label(box, text="Переклад на:").grid(row=0, column=0, sticky="w")
        self._combo(box, self._var(runtime.tk.StringVar, ("languages", "target"), runtime.LANG_NAMES,
                                   runtime._card_cache.clear), runtime.LANG_NAMES, 18
                    ).grid(row=0, column=1, sticky="w", padx=10, pady=3)
        runtime.ttk.Button(box, text="Налаштувати вміст картки…", command=self.open_card_settings
                   ).grid(row=1, column=0, columnspan=2, sticky="w", pady=6)
        for i, (key, text) in enumerate((("max_translations", "Варіантів перекладу на частину мови"),
                                         ("max_definitions", "Визначень у згорнутому вигляді"),
                                         ("max_synonyms", "Синонімів у згорнутому вигляді")), start=2):
            runtime.ttk.Label(box, text=text).grid(row=i, column=0, sticky="w", pady=3)
            runtime.ttk.Spinbox(box, from_=1, to=20, width=5,
                        textvariable=self._var(runtime.tk.IntVar, ("dictionary", key),
                                               after=runtime._card_cache.clear)
                        ).grid(row=i, column=1, sticky="w", padx=10)

        ob = runtime.ttk.LabelFrame(f, text="OCR (розпізнавання тексту на екрані)", padding=10)
        ob.pack(fill="x", pady=(12, 0))
        runtime.ttk.Label(ob, text="Рушій:").grid(row=0, column=0, sticky="w")
        self._combo(ob, self._var(runtime.tk.StringVar, ("ocr", "engine"), runtime.ENGINE_NAMES, self._check_ocr),
                    runtime.ENGINE_NAMES, 14).grid(row=0, column=1, sticky="w", padx=10, pady=3)
        runtime.ttk.Label(ob, text="Шлях до tesseract:").grid(row=1, column=0, sticky="w")
        row = runtime.ttk.Frame(ob)
        row.grid(row=1, column=1, sticky="w", padx=10)
        runtime.ttk.Entry(row, width=34, textvariable=self._var(runtime.tk.StringVar, ("ocr", "tesseract_cmd"))
                  ).pack(side="left")
        runtime.ttk.Button(row, text="…", width=3, command=self.browse_tesseract).pack(side="left", padx=4)
        for i, (key, text, lo, hi, inc) in enumerate((
                ("region_w", "Ширина області пошуку слова (px)", 200, 2000, 50),
                ("region_h", "Висота області пошуку слова (px)", 40, 600, 10)), start=2):
            runtime.ttk.Label(ob, text=text).grid(row=i, column=0, sticky="w", pady=3)
            runtime.ttk.Spinbox(ob, from_=lo, to=hi, increment=inc, width=7,
                        textvariable=self._var(runtime.tk.IntVar, ("ocr", key))
                        ).grid(row=i, column=1, sticky="w", padx=10)
        runtime.ttk.Label(ob, text="Збільшення знімка (1–4, лише Tesseract)").grid(row=4, column=0, sticky="w", pady=3)
        runtime.ttk.Spinbox(ob, from_=1.0, to=4.0, increment=0.5, width=7,
                    textvariable=self._var(runtime.tk.DoubleVar, ("ocr", "upscale"))
                    ).grid(row=4, column=1, sticky="w", padx=10)
        runtime.ttk.Label(ob, text="RapidOCR масштабує знімок сам; збільшення потрібне лише для Tesseract.",
                  foreground="#a2a8b8").grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))
        runtime.ttk.Checkbutton(ob, text="Розпізнавати злиті слова без пробілу (напр. «Nexttime» → «Next»)",
                        variable=self._var(runtime.tk.BooleanVar, ("ocr", "split_merged_words"))
                        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        runtime.ttk.Label(ob, text="Іноді OCR губить пробіл між двома сусідніми словами. Ця перевірка "
                          "шукає видиму прогалину на зображенні й уточнює лише те слово, де "
                          "курсор. Вимкни, якщо вона починає помилятись на звичайних словах.",
                  foreground="#a2a8b8", wraplength=520).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(2, 0))

        dl = runtime.ttk.LabelFrame(f, text="DeepL — переклад з урахуванням контексту (необов'язково)",
                            padding=10)
        dl.pack(fill="x", pady=(12, 0))
        runtime.ttk.Checkbutton(dl, text="Використовувати DeepL, коли ключ вказано",
                        variable=self._var(runtime.tk.BooleanVar, ("deepl", "enabled"),
                                           after=self._on_deepl_changed)
                        ).grid(row=0, column=0, columnspan=3, sticky="w")
        runtime.ttk.Label(dl, text="API-ключ:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.deepl_entry = runtime.ttk.Entry(dl, width=42, show="•",
                                     textvariable=self._var(runtime.tk.StringVar, ("deepl", "key"),
                                                            after=self._on_deepl_changed))
        self.deepl_entry.grid(row=1, column=1, sticky="w", padx=8, pady=(6, 0))
        show_var = runtime.tk.BooleanVar(value=False)
        runtime.ttk.Checkbutton(dl, text="показати", variable=show_var,
                        command=lambda: self.deepl_entry.config(show="" if show_var.get() else "•")
                        ).grid(row=1, column=2, sticky="w", pady=(6, 0))
        runtime.ttk.Button(dl, text="Перевірити ключ", command=self.check_deepl
                   ).grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.deepl_var = runtime.tk.StringVar(value="")
        runtime.ttk.Label(dl, textvariable=self.deepl_var, wraplength=380, justify="left"
                  ).grid(row=2, column=1, columnspan=2, sticky="w", padx=8, pady=(8, 0))
        runtime.ttk.Label(dl, text="DeepL часто краще розуміє контекст: при пошуку слова по OCR "
                          "програма передає йому весь рядок навколо слова, тож «chest» у "
                          "Minecraft має перекладатися як «сундук». Потрібен безкоштовний ключ "
                          "«DeepL API Free» (deepl.com → API; до 500 000 символів на місяць, "
                          "при реєстрації можуть попросити картку для верифікації). Текст "
                          "надсилається на сервери DeepL. Windows: ключ зберігається у Credential Manager; "
                          "інші ОС або недоступне сховище — лише цей сеанс, без запису у config.json. "
                          "Якщо DeepL недоступний або ліміт вичерпано, "
                          "автоматично працюють попередні джерела.",
                  foreground="#a2a8b8", wraplength=520, justify="left"
                  ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))

        ak = runtime.ttk.LabelFrame(f, text="Anki — тег гри для карток", padding=10)
        ak.pack(fill="x", pady=(12, 0))
        runtime.ttk.Label(ak, text="Назва гри:").grid(row=0, column=0, sticky="w")
        runtime.ttk.Entry(ak, width=28, textvariable=self._var(runtime.tk.StringVar, ("anki", "game"))
                  ).grid(row=0, column=1, sticky="w", padx=8)
        runtime.ttk.Checkbutton(ak, text="Якщо порожньо — брати назву з заголовка вікна гри (лише Windows)",
                        variable=self._var(runtime.tk.BooleanVar, ("anki", "auto_game_tag"))
                        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        runtime.ttk.Label(ak, text="Тег додається до карток .apkg: в Anki за ним можна відфільтрувати "
                          "слова з конкретної гри. Назва, вписана тут, ставиться всім "
                          "експортованим карткам. На картці також буде блок «From the game» "
                          "з реченням, у якому ти знайшов слово.",
                  foreground="#a2a8b8", wraplength=520, justify="left"
                  ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _on_deepl_changed(self):
        runtime._cooldown.pop("DeepL", None)
        if hasattr(self, "deepl_var"):
            self.deepl_var.set("")

    def check_deepl(self):
        self.deepl_var.set("Перевіряю…")

        def work():
            try:
                used, limit = runtime.deepl_usage()
                txt = (f"✓ Ключ працює. Використано {used:,} з {limit:,} символів за місяць"
                       ).replace(",", " ")
            except runtime.DeeplError as e:
                txt = f"✗ {e}"
            except Exception as e:
                txt = f"✗ Не вдалося перевірити: {type(e).__name__}: {e}"
            runtime.events.put(("deepl_status", txt))
        runtime.threading.Thread(target=work, daemon=True).start()

    def _refresh_status(self):
        self.status_var.set("⏸ На паузі" if self.paused else "● Працює")

    def _refresh_hint(self):
        parts = []
        if runtime.cfg["auto"]["enabled"]:
            parts.append("Виділи текст мишею (або двічі клацни по слову) — картка з'явиться сама.")
        btn = runtime.cfg["mouse"]["button"]
        if btn != "none":
            names = {v: k for k, v in runtime.MOUSE_BTN_NAMES.items()}
            mod = runtime.cfg["mouse"]["modifier"]
            mod_txt = "" if mod == "none" else f"{mod.capitalize()} + "
            parts.append(f"Наведи курсор на слово і натисни: {mod_txt}{names[btn]}.")
        hk = runtime.cfg["hotkeys"]["lookup_word"]
        if hk:
            parts.append(f"Або гаряча клавіша: {runtime.pretty(hk)}.")
        tl = runtime.cfg["hotkeys"]["translate_line"]
        if tl:
            parts.append(f"Переклад усього рядка: {runtime.pretty(tl)}.")
        sr = runtime.cfg["hotkeys"]["select_region"]
        if sr:
            parts.append(f"Виділити рамкою слово чи рядок (як скріншот): {runtime.pretty(sr)}.")
        if not parts:
            parts.append("Кнопки пошуку не задано — налаштуй їх на вкладці «Кнопки».")
        self.hint_var.set(" ".join(parts))

    def _refresh_cache_label(self):
        kb = runtime.cache_disk_kb()
        size = f"{kb:.0f} КБ" if kb < 1024 else f"{kb / 1024:.1f} МБ"
        self.cache_var.set(f"На диску зараз: {size}  ·  слів у кеші: {len(runtime._card_cache)}  ·  "
                           f"файл: {runtime._cache_path()}")

    def clear_cache(self):
        runtime.clear_card_cache()
        self._refresh_cache_label()
        self.toast("Кеш очищено")

    def refresh_saved_count(self):
        n = len(runtime.load_saved_words())
        if hasattr(self, "saved_btn"):
            self.saved_btn.config(text=f"⭐ Збережені слова ({n})")
        if getattr(self, "saved_win", None) is not None:
            self._render_saved_list()

    def open_saved_words(self):
        if getattr(self, "saved_win", None) is not None:
            self.saved_win.deiconify()
            self.saved_win.lift()
            self._render_saved_list()
            return
        win = self.saved_win = runtime.tk.Toplevel(self.root)
        win.title("Бібліотека — Word Lookup")
        win.configure(bg="#111318")
        win.geometry("1200x740")
        win.minsize(980, 540)
        win.protocol("WM_DELETE_WINDOW", self._hide_saved_window)
        self._editor_word = None
        self._editor_dirty = False
        self._loading_editor = False
        self.saved_query = runtime.tk.StringVar()
        self.saved_order = runtime.tk.StringVar(value="Спочатку нові")
        self.saved_state_filter = runtime.tk.StringVar(value="Усі")
        self.saved_due_only = runtime.tk.BooleanVar(value=False)
        panes = runtime.ttk.Panedwindow(win, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = runtime.ttk.Frame(panes, style="Sidebar.TFrame", padding=(20, 22))
        panes.add(left, weight=2)
        self.library_title = runtime.tk.StringVar()
        runtime.ttk.Label(left, textvariable=self.library_title, style="Sidebar.TLabel",
                  foreground="#f1f1f5", font=(runtime.FONT, 18, "bold")).pack(anchor="w", pady=(0, 18))
        search = runtime.ttk.Entry(left, textvariable=self.saved_query, width=36)
        search.pack(fill="x", pady=(0, 5))
        runtime.ttk.Label(left, text="Пошук за словом, перекладом, грою", style="Sidebar.TLabel",
                  font=(runtime.FONT, 9)).pack(anchor="w", pady=(0, 12))
        filters = runtime.ttk.Frame(left, style="Sidebar.TFrame")
        filters.pack(fill="x", pady=(0, 4))
        for label in runtime.STATUS_LABELS:
            runtime.ttk.Radiobutton(filters, text=label, value=label, variable=self.saved_state_filter,
                            style="Filter.TRadiobutton").pack(side="left")
        runtime.ttk.Checkbutton(left, text="Час повторити", variable=self.saved_due_only).pack(anchor="w", pady=(3, 12))
        list_area = runtime.ttk.Frame(left, style="Sidebar.TFrame")
        list_area.pack(fill="both", expand=True)
        self.saved_tree = runtime.ttk.Treeview(list_area, columns=("check", "word", "translation", "state", "source"),
                                       displaycolumns=("check", "word", "translation", "source"),
                                       show="headings", selectmode="browse", height=5)
        for col, label, width in (("check", "✓", 24), ("word", "Слово", 115),
                                  ("translation", "Переклад", 120), ("state", "Стан", 60),
                                  ("source", "Гра", 90)):
            self.saved_tree.heading(col, text=label)
            self.saved_tree.column(col, width=width, minwidth=24, stretch=col != "check")
        bar = runtime.ttk.Scrollbar(list_area, orient="vertical", command=self.saved_tree.yview)
        self.saved_tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.saved_tree.pack(fill="both", expand=True)
        self.saved_tree.bind("<Button-1>", self._saved_tree_click)
        self.saved_tree.bind("<<TreeviewSelect>>", self._saved_tree_select)
        self.saved_tree.bind("<Double-1>", lambda _e: self._open_selected_popup())
        self.saved_tree.bind("<Return>", lambda _e: self._open_selected_popup())
        selection = runtime.ttk.Frame(left, style="Sidebar.TFrame")
        selection.pack(fill="x", pady=(12, 6))
        runtime.ttk.Button(selection, text="Вибрати знайдені", command=lambda: self.select_all_saved(True)).pack(side="left")
        runtime.ttk.Button(selection, text="Зняти", command=lambda: self.select_all_saved(False)).pack(side="left", padx=5)
        self.saved_selected_var = runtime.tk.StringVar()
        runtime.ttk.Label(left, textvariable=self.saved_selected_var, style="Sidebar.TLabel").pack(anchor="w", pady=4)
        runtime.ttk.Combobox(left, state="readonly", textvariable=self.saved_order,
                     values=["Спочатку нові", "Спочатку старі", "За абеткою"]).pack(fill="x", pady=4)
        right = runtime.ttk.Frame(panes, padding=(24, 22))
        panes.add(right, weight=3)
        tools = runtime.ttk.Frame(right)
        tools.pack(fill="x", pady=(0, 14))
        runtime.ttk.Button(tools, text="Повторити слова", command=self.review_saved_words).pack(side="left")
        self.anki_sync_btn = runtime.ttk.Button(tools, text="Передати в Anki", style="Accent.TButton", command=self.sync_saved_to_anki)
        self.anki_sync_btn.pack(side="right")
        detail = runtime.ScrolledPanel(right, bg="#111318")
        detail.pack(fill="both", expand=True)
        self.saved_list_frame = detail.content
        export = runtime.ttk.Frame(right)
        export.pack(fill="x", pady=(14, 0))
        runtime.ttk.Label(export, text="Колода").pack(side="left")
        runtime.ttk.Entry(export, width=18, textvariable=self._var(runtime.tk.StringVar, ("anki", "deck"))).pack(side="left", padx=6)
        runtime.ttk.Button(export, text="Експорт .txt", command=self.export_anki_selected).pack(side="right")
        runtime.ttk.Button(export, text="Експорт .apkg", command=self.export_anki_apkg_selected).pack(side="right", padx=5)
        self.anki_sync_status = runtime.tk.StringVar(value="Для передавання відкрий Anki з AnkiConnect на цьому ПК.")
        runtime.ttk.Label(right, textvariable=self.anki_sync_status, foreground="#a2a8b8", wraplength=540).pack(anchor="w", pady=(8, 0))
        for var in (self.saved_query, self.saved_order, self.saved_state_filter, self.saved_due_only):
            var.trace_add("write", lambda *_: self._render_saved_list())
        win.bind("<Control-f>", lambda _e: search.focus_set())
        win.bind("<Escape>", lambda _e: self._hide_saved_window())
        win.bind("<Control-s>", lambda _e: self.save_saved_details())
        self._saved_iids = {}
        self._render_saved_list()
        search.focus_set()

    def _hide_saved_window(self):
        if self._commit_or_discard_editor():
            self.saved_win.withdraw()

    def _commit_or_discard_editor(self):
        if not getattr(self, "_editor_dirty", False):
            return True
        answer = runtime.messagebox.askyesnocancel(runtime.APP_NAME, "Зберегти зміни до картки?", parent=self.saved_win)
        if answer is None:
            return False
        if answer:
            return self.save_saved_details()
        self._editor_dirty = False
        return True

    def _saved_tree_click(self, event):
        iid = self.saved_tree.identify_row(event.y)
        if iid and self.saved_tree.identify_column(event.x) == "#1":
            word = self._saved_iids[iid]
            var = self._saved_check_var(word)
            var.set(not var.get())
            self._update_selected_count()
            return "break"

    def _saved_tree_select(self, _event=None):
        selection = self.saved_tree.selection()
        if selection:
            word = self._saved_iids.get(selection[0])
            if word and word != self._editor_word:
                if self._commit_or_discard_editor():
                    self.show_saved_details(word)
                elif self._editor_word:
                    self.saved_tree.selection_set(self._saved_tree_id(self._editor_word))

    def _open_selected_popup(self):
        selection = self.saved_tree.selection()
        if selection:
            self.open_saved_word(self._saved_iids[selection[0]])

    @staticmethod
    def _saved_tree_id(word):
        return runtime.hashlib.sha256(word.lower().encode()).hexdigest()

    def show_saved_details(self, word):
        entry = runtime.saved_entry(word)
        if not entry:
            return
        self._loading_editor = True
        self._editor_word = word
        self._editor_dirty = False
        for child in self.saved_list_frame.winfo_children():
            child.destroy()
        frame = self.saved_list_frame
        runtime.ttk.Label(frame, text=word, font=(runtime.FONT, 27, "bold"), wraplength=500).pack(anchor="w", padx=12, pady=8)
        buttons = runtime.ttk.Frame(frame)
        buttons.pack(fill="x", padx=12)
        runtime.ttk.Button(buttons, text="Зберегти (Ctrl+S)", style="Accent.TButton", command=self.save_saved_details).pack(side="left")
        runtime.ttk.Button(buttons, text="Озвучити", command=lambda: self.speak_saved_word(word)).pack(side="left", padx=6)
        runtime.ttk.Button(buttons, text="Видалити", command=lambda: self.delete_saved_word(word)).pack(side="right")
        self.editor_message = runtime.tk.StringVar(value="Зміни зберігаються кнопкою або Ctrl+S.")
        runtime.ttk.Label(frame, textvariable=self.editor_message, wraplength=500).pack(anchor="w", padx=12, pady=6)
        row = runtime.ttk.Frame(frame)
        row.pack(fill="x", padx=12, pady=6)
        runtime.ttk.Label(row, text="Стан:").pack(side="left")
        states = {k: v for k, v in runtime.STATUS_LABELS.items() if v != "all"}
        inverse = {v: k for k, v in states.items()}
        self.editor_status = runtime.tk.StringVar(value=inverse.get(entry.get("status", "new"), "Нові"))
        runtime.ttk.Combobox(row, textvariable=self.editor_status, values=list(states), width=12,
                     state="readonly").pack(side="left", padx=6)
        self.editor_status.trace_add("write", self._mark_editor_dirty)
        self.editor_fields = {}
        labels = (("translation", "Переклад"), ("definition", "Визначення англійською"),
                  ("definition_tr", "Переклад визначення"), ("example", "Приклад"),
                  ("context", "Речення з гри / контекст"), ("context_tr", "Переклад контексту"),
                  ("source", "Джерело / гра"), ("notes", "Власні нотатки"))
        for key, label in labels:
            runtime.ttk.Label(frame, text=label.upper(), foreground="#a2a8b8", font=(runtime.FONT, 9)).pack(anchor="w", padx=12, pady=(14, 5))
            text = runtime.tk.Text(frame, height=3 if key == "notes" else 2, width=48, wrap="word",
                           font=(runtime.FONT, 14 if key == "translation" else 11), undo=True,
                           bg="#1d2029", fg="#f1f1f5", insertbackground="#ffd43b",
                           selectbackground="#ffd43b", selectforeground="#111318",
                           relief="flat", bd=0, padx=12, pady=10, highlightthickness=0)
            text.pack(fill="x", padx=12)
            text.insert("1.0", entry.get(key, ""))
            text.edit_modified(False)
            text.bind("<<Modified>>", self._mark_editor_dirty)
            self.editor_fields[key] = text
        image_buttons = runtime.ttk.Frame(frame)
        image_buttons.pack(fill="x", padx=12, pady=10)
        runtime.ttk.Button(image_buttons, text="Додати зображення…", command=self.replace_saved_image).pack(side="left")
        runtime.ttk.Button(image_buttons, text="Відкрити зображення", command=self.open_saved_image).pack(side="left", padx=6)
        self.editor_photo = runtime.context_photo(entry.get("image_file", ""), self.saved_win, size=(480, 250))
        if self.editor_photo:
            label = runtime.ttk.Label(frame, image=self.editor_photo, cursor="hand2")
            label.pack(anchor="w", padx=12, pady=8)
            label.bind("<Button-1>", lambda _e: self.open_saved_image())
        self._loading_editor = False
        self._editor_dirty = False
        iid = self._saved_tree_id(word)
        if self.saved_tree.exists(iid):
            self.saved_tree.selection_set(iid)

    def _mark_editor_dirty(self, event=None, *_):
        # Text генерує <<Modified>> і при скиданні прапорця: обробляємо лише зміни.
        if event is not None and hasattr(event, "widget"):
            if not event.widget.edit_modified():
                return
            event.widget.edit_modified(False)
        if not self._loading_editor:
            self._editor_dirty = True
            self.editor_message.set("Є незбережені зміни.")

    def save_saved_details(self):
        if not self._editor_word or not hasattr(self, "editor_fields"):
            return True
        fields = {k: text.get("1.0", "end-1c") for k, text in self.editor_fields.items()}
        fields["status"] = runtime.STATUS_LABELS[self.editor_status.get()]
        try:
            runtime.update_saved_entry(self._editor_word, fields)
        except (OSError, ValueError) as e:
            self.editor_message.set(str(e))
            return False
        self._editor_dirty = False
        self.editor_message.set("Збережено")
        self._render_saved_list()
        return True

    def replace_saved_image(self):
        if not self._commit_or_discard_editor():
            return
        path = runtime.filedialog.askopenfilename(parent=self.saved_win, title="Зображення контексту",
                                          filetypes=[("Зображення", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if not path:
            return
        name = ""
        try:
            from PIL import Image
            with Image.open(path) as image:
                name = runtime.save_context_image(image)
            if not name:
                raise OSError("Не вдалося записати зображення")
            entry = runtime.saved_entry(self._editor_word)
            old = runtime.context_image_path(entry.get("image_file", ""))
            runtime.update_saved_entry(self._editor_word, {"image_file": name})
            if old and not any(runtime.context_image_path(e.get("image_file", "")) == old for e in runtime.load_saved_words()):
                try:
                    runtime.os.remove(old)
                except OSError:
                    runtime.log_exc('word_lookup.replace_saved_image')
            self.show_saved_details(self._editor_word)
        except (OSError, ValueError) as e:
            if name and not any(e.get("image_file") == name for e in runtime.load_saved_words()):
                try:
                    runtime.os.remove(runtime.context_image_path(name))
                except OSError:
                    runtime.log_exc('word_lookup.replace_saved_image')
            self.editor_message.set(f"Зображення не додано: {e}")

    def open_saved_image(self):
        entry = runtime.saved_entry(self._editor_word) if self._editor_word else None
        path = runtime.context_image_path(entry.get("image_file", "")) if entry else None
        if path and runtime.os.path.isfile(path):
            runtime.open_file(path)
        elif hasattr(self, "editor_message"):
            self.editor_message.set("У цієї картки ще немає зображення.")

    def speak_saved_word(self, word):
        entry = runtime.saved_entry(word)
        audios = entry.get("dictionary", {}).get("audios", []) if entry else []
        if audios and runtime.AUDIO_OK:
            runtime.play_audio(audios[0][1])
        else:
            self.editor_message.set("Озвучення відсутнє або pygame не встановлений.")

    def sync_saved_to_anki(self):
        if not self._commit_or_discard_editor():
            return
        entries = [e for e in runtime.load_saved_words() if self._saved_check_var(e["word"]).get()]
        entries = runtime.copy.deepcopy(entries or self._visible_saved_words())
        if not entries:
            self.anki_sync_status.set("Немає слів для передавання")
            return
        deck = runtime.cfg["anki"]["deck"]
        self.anki_sync_btn.state(["disabled"])
        self.anki_sync_status.set(f"Передаю {len(entries)} записів у Anki…")
        def worker():
            try:
                runtime.events.put(("anki_sync_done", runtime.sync_anki_entries(entries, deck), ""))
            except Exception as e:
                runtime.events.put(("anki_sync_done", None, str(e)))
        runtime.threading.Thread(target=worker, daemon=True).start()

    def _saved_check_var(self, word: str):
        key = word.lower()
        var = self.saved_checks.get(key)
        if var is None:
            var = runtime.tk.BooleanVar(value=False)
            self.saved_checks[key] = var
        return var

    def _update_selected_count(self):
        n = sum(1 for e in runtime.load_saved_words() if self._saved_check_var(e["word"]).get())
        if hasattr(self, "saved_selected_var"):
            shown = len(self._visible_saved_words())
            self.saved_selected_var.set(f"Знайдено: {shown} · Обрано: {n}")
        if hasattr(self, "saved_tree"):
            for iid, word in self._saved_iids.items():
                self.saved_tree.set(iid, "check", "☑" if self._saved_check_var(word).get() else "☐")

    def review_saved_words(self):
        if not self._commit_or_discard_editor():
            return
        selected = [e for e in runtime.load_saved_words() if self._saved_check_var(e["word"]).get()]
        entries = selected or self._visible_saved_words()
        if not entries:
            self.toast("Спочатку збережи слова або зміни запит пошуку")
            return
        self.review_window = runtime.ReviewWindow(self, entries)

    def _visible_saved_words(self):
        order = {"Спочатку нові": "newest", "Спочатку старі": "oldest", "За абеткою": "az"}
        entries = runtime.filter_saved_words(runtime.load_saved_words(), self.saved_query.get(),
                                      order.get(self.saved_order.get(), "newest"))
        state = runtime.STATUS_LABELS[self.saved_state_filter.get()]
        return [e for e in entries if (state == "all" or e.get("status", "new") == state)
                and (not self.saved_due_only.get() or runtime.review_due(e))]

    def select_all_saved(self, value: bool):
        for e in self._visible_saved_words() if value else runtime.load_saved_words():
            self._saved_check_var(e.get("word", "")).set(value)
        self._update_selected_count()

    def export_anki_selected(self):
        self._export_saved("txt")

    def export_anki_apkg_selected(self):
        self._export_saved("apkg")

    def _export_saved(self, kind):
        if not self._commit_or_discard_editor():
            return
        selected = [e for e in runtime.load_saved_words()
                   if self._saved_check_var(e.get("word", "")).get()]
        if not selected:
            self.toast("Спочатку познач слова прапорцями")
            return
        if kind == "apkg":
            if not runtime.apkg_available():
                runtime.messagebox.showinfo(
                    runtime.APP_NAME,
                    "Для готового файлу колоди (.apkg) потрібна додаткова бібліотека, якої зараз "
                    "немає в цьому Python:\n\n"
                    f'"{runtime.sys.executable}" -m pip install genanki\n\n'
                    "Постав її й спробуй ще раз. А поки що можна скористатись кнопкою "
                    "«→ Anki (.txt)» — вона працює без додаткових бібліотек.")
                return
            path = runtime.filedialog.asksaveasfilename(
                title="Експорт в Anki (готова колода)", defaultextension=".apkg",
                filetypes=[("Колода Anki", "*.apkg"), ("Усі файли", "*.*")],
                initialfile="word_lookup_anki.apkg")
        else:
            path = runtime.filedialog.asksaveasfilename(
                title="Експорт в Anki", defaultextension=".txt",
                filetypes=[("Текстовий файл для Anki", "*.txt"), ("Усі файли", "*.*")],
                initialfile="word_lookup_anki.txt")
        if not path:
            return
        need = sum(1 for e in selected if runtime._needs_enrich(e))
        if need:  # старі слова без пояснень — дотягуємо (мережа), тож у фоновому потоці
            self.toast(f"Підтягую переклад і пояснення для {need} слів — зачекай…", timeout=20)
        runtime.threading.Thread(target=self._export_worker, args=(kind, selected, path),
                         daemon=True).start()

    def _export_worker(self, kind, selected, path):
        err = ""
        try:
            runtime.enrich_entries(selected)
            (runtime.export_anki_apkg if kind == "apkg" else runtime.export_anki_txt)(selected, path)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        runtime.events.put(("export_done", kind, len(selected), path, err))

    def _export_finished(self, kind, n, path, err):
        if hasattr(self, "saved_list_frame"):  # у списку з'являться дотягнуті переклади
            try:
                self._render_saved_list()
            except runtime.tk.TclError:
                runtime.log_exc('word_lookup._export_finished')
        if err:
            self.toast(f"Не вдалося створити файл: {err}", timeout=6)
            return
        if kind == "apkg":
            self.toast(f"Створено колоду з {n} слів — відкрий файл подвійним кліком, "
                       "Anki додасть картки сама", timeout=6)
            return
        try:
            with open(path, encoding="utf-8") as f:
                rows = [ln.rstrip("\n") for ln in f if not ln.startswith("#") and ln.strip()]
        except OSError:
            rows = []
        preview = "\n".join((r if len(r) <= 110 else r[:107] + "…") for r in rows[:3])
        runtime.messagebox.showinfo(
            runtime.APP_NAME,
            f"Збережено {n} слів:\n{path}\n\nПерші рядки файлу (слово ⇥ зворотна сторона):\n"
            f"{preview}\n\nУ вікні імпорту Anki перевір блок зіставлення полів: «Зворотна "
            "сторона» (Back) має бути прив'язана до стовпця 2. Якщо цих слів у колоді вже "
            "є — видали старі картки або вибери оновлення існуючих нотаток, інакше Anki "
            "може їх пропустити й зворотна сторона лишиться порожньою.")

    def _render_saved_list(self):
        self.library_title.set(f"Бібліотека · {len(runtime.load_saved_words())} слів")
        selected = self.saved_tree.selection()
        previous = self._saved_iids.get(selected[0]) if selected else self._editor_word
        self.saved_tree.delete(*self.saved_tree.get_children())
        self._saved_iids = {}
        inverse = {v: k for k, v in runtime.STATUS_LABELS.items()}
        for entry in self._visible_saved_words():
            word = entry["word"]
            iid = self._saved_tree_id(word)
            if iid in self._saved_iids:
                continue
            self._saved_iids[iid] = word
            self.saved_tree.insert("", "end", iid=iid, values=(
                "☑" if self._saved_check_var(word).get() else "☐", word,
                entry.get("translation", ""), inverse.get(entry.get("status", "new"), "Нові"),
                entry.get("source", "")))
        if previous and self.saved_tree.exists(self._saved_tree_id(previous)):
            self.saved_tree.selection_set(self._saved_tree_id(previous))
            if previous == self._editor_word and not self._editor_dirty and hasattr(self, "editor_status"):
                self._loading_editor = True
                entry = runtime.saved_entry(previous)
                self.editor_status.set(inverse.get(entry.get("status", "new"), "Нові"))
                self._loading_editor = False
        elif self.saved_tree.get_children() and not self._editor_dirty:
            self.saved_tree.selection_set(self.saved_tree.get_children()[0])
        if not runtime.load_saved_words():
            for child in self.saved_list_frame.winfo_children():
                child.destroy()
            self._editor_word = None
            self._editor_dirty = False
            runtime.ttk.Label(self.saved_list_frame, text="Бібліотека порожня. Натисни ⭐ у картці слова чи фрази.",
                      wraplength=450).pack(padx=12, pady=20)
        self._update_selected_count()

    def open_saved_word(self, word):
        entry = runtime.saved_entry(word)
        if entry is not None:
            self._new_token()
            self.show_lookup(runtime.saved_word_card(entry), None)

    def delete_saved_word(self, word):
        if not runtime.remove_saved_word(word):
            self.toast("Не вдалося видалити запис; перевір журнал і доступ до файлу")
            return
        self.saved_checks.pop(word.lower(), None)
        if getattr(self, "_editor_word", None) == word:
            self._editor_word = None
            self._editor_dirty = False
        self.refresh_saved_count()
