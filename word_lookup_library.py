"""Saved-word library editor, review and export views."""
from word_lookup_i18n import _ as tr_ui
import copy
import hashlib
import os
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox
from word_lookup_theme import (APP_NAME, AUDIO_OK, FONT, STATUS_LABELS)
import word_lookup_platform as platform


from word_lookup_models import as_event


class LibraryViewMixin:
    def _apply_library_style(self):
        """Explicit clam colours keep list controls dark on Windows and Linux."""
        style = ttk.Style(self.root)
        panel, field, fg, dim = "#1d2029", "#272b36", "#f1f1f5", "#a2a8b8"
        style.configure("Library.Treeview", rowheight=48, fieldbackground=panel,
                        background=panel, foreground=fg, borderwidth=0,
                        font=(FONT, 11))
        style.map("Library.Treeview", background=[("selected", "#303644")],
                  foreground=[("selected", fg)])
        style.configure("Library.Treeview.Heading", background=panel, foreground=dim,
                        font=(FONT, 9), padding=8, relief="flat", borderwidth=0,
                        lightcolor=panel, darkcolor=panel, bordercolor=panel)
        style.map("Library.Treeview.Heading", background=[("active", field)])
        for orientation in ("Vertical", "Horizontal"):
            name = f"Library.{orientation}.TScrollbar"
            style.configure(name, background=field, troughcolor=panel, arrowcolor=dim,
                            bordercolor=panel, lightcolor=field, darkcolor=field,
                            borderwidth=0, relief="flat", arrowsize=13)
            style.map(name, background=[("pressed", "#515a70"), ("active", "#3b4354")],
                      arrowcolor=[("disabled", "#73798a")])

    def _library_filter_control(self, parent, text, variable, value=None):
        """Use real button semantics without native light radio/check indicators."""
        options = dict(text=text, variable=variable, indicatoron=False,
                       background="#1d2029", foreground="#a2a8b8",
                       activebackground="#353a48", activeforeground="#ffd43b",
                       selectcolor="#303644", relief="flat", offrelief="flat",
                       borderwidth=0, highlightthickness=1,
                       highlightbackground="#1d2029", highlightcolor="#ffd43b",
                       font=(FONT, 10), padx=9, pady=6, takefocus=True,
                       cursor="hand2")
        if value is None:
            button = tk.Checkbutton(parent, **options)
            def selected():
                return variable.get()
        else:
            button = tk.Radiobutton(parent, value=value, **options)
            def selected():
                return variable.get() == value

        def sync(*_):
            active = selected()
            button.configure(foreground="#ffd43b" if active else "#a2a8b8",
                             highlightbackground="#ffd43b" if active else "#1d2029")

        variable.trace_add("write", sync)
        sync()
        return button

    def _layout_library_filters(self, event=None):
        buttons = list(self.saved_filter_buttons.values())
        available = event.width if event is not None else sum(b.winfo_reqwidth() + 3 for b in buttons)
        widths = [button.winfo_reqwidth() + 3 for button in buttons]
        columns = next((count for count in (4, 2, 1)
                        if max(sum(widths[start:start + count])
                               for start in range(0, len(widths), count)) <= available), 1)
        if getattr(self, "_library_filter_columns", None) == columns:
            return
        self._library_filter_columns = columns
        for index, button in enumerate(buttons):
            button.grid(row=index // columns, column=index % columns, sticky="ew", padx=(0, 3), pady=(0, 2))

    def _library_column_specs(self):
        header_font = tkfont.Font(root=self.root, font=(FONT, 9))
        body_font = tkfont.Font(root=self.root, font=(FONT, 11))
        specs = []
        for column, label, minimum, sample in (
            ("check", "Вибір", 54, ""), ("word", "Слово", 145, "understatement"),
            ("translation", "Переклад", 145, "применшення"),
            ("state", "Стан", 60, ""), ("source", "Гра", 160, "Example game"),
        ):
            width = max(minimum, header_font.measure(label) + 20, body_font.measure(sample) + 20)
            specs.append((column, label, width))
        return specs

    def refresh_saved_count(self):
        n = len(self.library.load_saved_words())
        if hasattr(self, "saved_btn"):
            self.saved_btn.config(text=f"⭐ Збережені слова ({n})")
        if getattr(self, "saved_win", None) is not None:
            self._render_saved_list()

    def open_saved_words(self):
        if getattr(self, "saved_win", None) is not None:
            if not self.reload_library(reset_filters=False):
                return
            self.saved_win.deiconify()
            self.saved_win.lift()
            return
        self.library.reload_saved_words()
        win = self.saved_win = tk.Toplevel(self.root)
        win.title(tr_ui("Бібліотека — Word Lookup"))
        win.configure(bg="#111318")
        self._apply_library_style()
        win.geometry("1200x740")
        win.minsize(980, 540)
        win.protocol("WM_DELETE_WINDOW", self._hide_saved_window)
        self._editor_word = None
        self._editor_dirty = False
        self._loading_editor = False
        self.saved_query = tk.StringVar()
        self.saved_order = tk.StringVar(value="Спочатку нові")
        self.saved_state_filter = tk.StringVar(value="Усі")
        self.saved_due_only = tk.BooleanVar(value=False)
        # Classic Tk enforces per-pane minima even while dragging the sash.
        # ttk.Panedwindow permits a zero-width sidebar on Windows.
        panes = self.library_panes = tk.PanedWindow(win, orient="horizontal",
            bg="#303644", bd=0, sashwidth=7, sashrelief="flat", sashcursor="sb_h_double_arrow")
        panes.pack(fill="both", expand=True)
        left = self.library_sidebar = ttk.Frame(panes, style="Sidebar.TFrame", padding=(20, 22))
        panes.add(left, minsize=320, stretch="always", sticky="nsew")
        self.library_title = tk.StringVar()
        ttk.Label(left, textvariable=self.library_title, style="Sidebar.TLabel",
                  foreground="#f1f1f5", font=(FONT, 18, "bold")).pack(anchor="w", pady=(0, 18))
        recovery = self._library_recovery_controls = ttk.Frame(left, style="Sidebar.TFrame")
        recovery.pack(fill="x", pady=(0, 10))
        ttk.Button(recovery, text=tr_ui("Оновити"), command=self.reload_library).pack(side="left")
        ttk.Button(recovery, text=tr_ui("Відновити з файлу…"), command=self.restore_library_file
                           ).pack(side="left", padx=6)
        search = self.library_search = ttk.Entry(left, textvariable=self.saved_query, width=36)
        search.pack(fill="x", pady=(0, 5))
        ttk.Label(left, text=tr_ui("Пошук за словом, перекладом, грою"), style="Sidebar.TLabel",
                  font=(FONT, 9)).pack(anchor="w", pady=(0, 12))
        filters = ttk.Frame(left, style="Sidebar.TFrame")
        filters.pack(fill="x", pady=(0, 4))
        self.saved_filter_buttons = {}
        for label in STATUS_LABELS:
            button = self._library_filter_control(filters, label, self.saved_state_filter, label)
            self.saved_filter_buttons[label] = button
        self._library_filter_columns = None
        self._layout_library_filters()
        filters.bind("<Configure>", self._layout_library_filters)
        self.saved_due_button = self._library_filter_control(left, "Час повторити", self.saved_due_only)
        self.saved_due_button.pack(anchor="w", pady=(3, 12))
        list_area = ttk.Frame(left, style="Sidebar.TFrame")
        list_area.pack(fill="both", expand=True)
        list_area.rowconfigure(0, weight=1)
        list_area.columnconfigure(0, weight=1)
        self.saved_tree = ttk.Treeview(list_area, columns=("check", "word", "translation", "state", "source"),
                                       displaycolumns=("check", "word", "translation", "source"),
                                       show="headings", selectmode="browse", height=5,
                                       style="Library.Treeview")
        column_specs = self._library_column_specs()
        for col, label, width in column_specs:
            self.saved_tree.heading(col, text=label)
            self.saved_tree.column(col, width=width, minwidth=width, stretch=col == "source",
                                   anchor="center" if col == "check" else "w")
        self.saved_ybar = ttk.Scrollbar(list_area, orient="vertical",
            command=self.saved_tree.yview, style="Library.Vertical.TScrollbar")
        self.saved_xbar = ttk.Scrollbar(list_area, orient="horizontal",
            command=self.saved_tree.xview, style="Library.Horizontal.TScrollbar")
        self.saved_tree.configure(yscrollcommand=self.saved_ybar.set, xscrollcommand=self.saved_xbar.set)
        self.saved_tree.grid(row=0, column=0, sticky="nsew")
        self.saved_ybar.grid(row=0, column=1, sticky="ns")
        self.saved_xbar.grid(row=1, column=0, sticky="ew")
        self.saved_tree.bind("<Button-1>", self._saved_tree_click)
        self.saved_tree.bind("<space>", self._toggle_selected_saved_word)
        self.saved_tree.bind("<<TreeviewSelect>>", self._saved_tree_select)
        self.saved_tree.bind("<Double-1>", lambda _e: self._open_selected_popup())
        self.saved_tree.bind("<Return>", lambda _e: self._open_selected_popup())
        self.saved_source_preview = tk.StringVar()
        source_preview = ttk.Label(left, textvariable=self.saved_source_preview,
                                          style="Sidebar.TLabel", wraplength=480)
        source_preview.pack(fill="x", pady=(5, 0))
        left.bind("<Configure>", lambda event: source_preview.configure(
            wraplength=max(120, event.width - 40)))
        selection = ttk.Frame(left, style="Sidebar.TFrame")
        selection.pack(fill="x", pady=(12, 6))
        ttk.Button(selection, text=tr_ui("Вибрати знайдені"), command=lambda: self.select_all_saved(True)).pack(side="left")
        ttk.Button(selection, text=tr_ui("Зняти"), command=lambda: self.select_all_saved(False)).pack(side="left", padx=5)
        self.saved_selected_var = tk.StringVar()
        ttk.Label(left, textvariable=self.saved_selected_var, style="Sidebar.TLabel").pack(anchor="w", pady=4)
        ttk.Combobox(left, state="readonly", textvariable=self.saved_order,
                     values=["Спочатку нові", "Спочатку старі", "За абеткою"]).pack(fill="x", pady=4)
        self.library_storage_info = tk.StringVar()
        storage_label = ttk.Label(left, textvariable=self.library_storage_info,
                                          style="Sidebar.TLabel", wraplength=480, justify="left")
        storage_label.pack(fill="x", pady=(8, 0))
        left.bind("<Configure>", lambda event: storage_label.configure(
            wraplength=max(120, event.width - 40)), add="+")
        right = self.library_editor = ttk.Frame(panes, padding=(24, 22))
        panes.add(right, minsize=360, stretch="always", sticky="nsew")
        tools = self._library_editor_controls = ttk.Frame(right)
        tools.pack(fill="x", pady=(0, 14))
        ttk.Button(tools, text=tr_ui("Повторити слова"), command=self.review_saved_words).pack(side="left")
        self.anki_sync_btn = ttk.Button(tools, text=tr_ui("Передати в Anki"), style="Accent.TButton", command=self.sync_saved_to_anki)
        self.anki_sync_btn.pack(side="right")
        detail = self.library_detail = self.scrolled_panel(right, bg="#111318")
        detail.bar.configure(style="Library.Vertical.TScrollbar")
        detail.pack(fill="both", expand=True)
        self.saved_list_frame = detail.content
        export = ttk.Frame(right)
        export.pack(fill="x", pady=(14, 0))
        ttk.Label(export, text=tr_ui("Колода")).pack(side="left")
        ttk.Entry(export, width=18, textvariable=self._var(tk.StringVar, ("anki", "deck"))).pack(side="left", padx=6)
        ttk.Button(export, text=tr_ui("Експорт .txt"), command=self.export_anki_selected).pack(side="right")
        ttk.Button(export, text=tr_ui("Експорт .apkg"), command=self.export_anki_apkg_selected).pack(side="right", padx=5)
        self.anki_sync_status = tk.StringVar(value="Для передавання відкрий Anki з AnkiConnect на цьому ПК.")
        ttk.Label(right, textvariable=self.anki_sync_status, foreground="#a2a8b8", wraplength=540).pack(anchor="w", pady=(8, 0))
        for var in (self.saved_query, self.saved_order, self.saved_state_filter, self.saved_due_only):
            var.trace_add("write", lambda *_: self._render_saved_list())
        win.bind("<Control-f>", lambda _e: search.focus_set())
        win.bind("<Escape>", lambda _e: self._hide_saved_window())
        win.bind("<Control-s>", lambda _e: self.save_saved_details())
        self._saved_iids = {}
        self._render_saved_list()
        self._configure_library_split(column_specs)
        search.focus_set()

    def _configure_library_split(self, column_specs):
        """Establish a visible sidebar after the actual window is mapped."""
        win, panes = self.saved_win, self.library_panes
        win.update_idletasks()
        self._library_sidebar_min = max(320, self._library_recovery_controls.winfo_reqwidth() + 40)
        self._library_editor_min = max(360, self._library_editor_controls.winfo_reqwidth() + 48)
        panes.paneconfigure(self.library_sidebar, minsize=self._library_sidebar_min)
        panes.paneconfigure(self.library_editor, minsize=self._library_editor_min)
        win.minsize(max(980, self._library_sidebar_min + self._library_editor_min + 7), 540)
        self._library_preferred_width = (sum(width for column, _label, width in column_specs if column != "state")
                                         + 44 + self.saved_ybar.winfo_reqwidth())
        self._library_split_initialized = False
        self._library_split_timer = None
        panes.bind("<Map>", self._queue_library_split)
        panes.bind("<Configure>", self._queue_library_split)
        self._queue_library_split()

    def _queue_library_split(self, _event=None):
        if self._library_split_initialized or self._library_split_timer is not None:
            return
        self._library_split_timer = self.library_panes.after_idle(self._place_library_split)

    def _place_library_split(self):
        self._library_split_timer = None
        panes = self.library_panes
        if not panes.winfo_ismapped():
            return  # Map/Configure will schedule initialization when ready.
        width = panes.winfo_width()
        maximum = width - self._library_editor_min - 7
        if maximum < self._library_sidebar_min:
            return
        preferred = min(self._library_preferred_width, width - max(500, self._library_editor_min))
        panes.sash_place(0, max(self._library_sidebar_min, min(preferred, maximum)), 0)
        self._library_split_initialized = True

    def _reset_library_view(self, *, reset_filters):
        self._editor_word = None
        self._editor_dirty = False
        if reset_filters:
            self._library_reloading = True
            try:
                self.saved_query.set("")
                self.saved_state_filter.set("Усі")
                self.saved_due_only.set(False)
            finally:
                self._library_reloading = False
        self.refresh_saved_count()

    def reload_library(self, *, reset_filters=True):
        if not self._commit_or_discard_editor():
            return False
        self.library.reload_saved_words()
        self._reset_library_view(reset_filters=reset_filters)
        return True

    def restore_library_file(self):
        if not self._commit_or_discard_editor():
            return
        path = filedialog.askopenfilename(parent=self.saved_win,
            title=tr_ui("Відновити слова зі saved_words.json або резервної копії"),
            initialdir=os.path.dirname(self.library._saved_words_path()),
            filetypes=[("Бібліотека / резервна копія JSON", "*.json"), ("Усі файли", "*.*")])
        if not path:
            return
        try:
            result = self.library.restore_saved_words(path)
        except (OSError, ValueError):
            messagebox.showerror(APP_NAME,
                tr_ui("Не вдалося відновити бібліотеку. Вибери справний saved_words.json "
                "або його резервну копію. Перевір доступ до папки й закрий інші копії програми. "
                "Попередні дані не замінено."), parent=self.saved_win)
            return
        self._reset_library_view(reset_filters=True)
        self.toast(f"У бібліотеці {result['total']} слів; додано {result['added']}.")

    def _hide_saved_window(self):
        if self._commit_or_discard_editor():
            self.saved_win.withdraw()

    def _commit_or_discard_editor(self):
        if not self._editor_has_unsaved_changes():
            return True
        answer = messagebox.askyesnocancel(APP_NAME, tr_ui("Зберегти зміни до картки?"), parent=self.saved_win)
        if answer is None:
            return False
        if answer:
            return self.save_saved_details()
        # Hiding the window preserves its widgets. Discard must restore their
        # saved values too, otherwise reopening silently resurrects the draft.
        if self.library.saved_entry(self._editor_word) is not None:
            self.show_saved_details(self._editor_word)
        else:
            self._editor_dirty = False
            self._render_saved_list()
        return True

    def _editor_has_unsaved_changes(self):
        if not getattr(self, "_editor_word", None):
            return False
        # <<Modified>> is queued by Tk. A fast save/close can run before its
        # binding, so consult the Text flags as well as the processed dirty flag.
        if not self._loading_editor and any(field.edit_modified()
                                           for field in self.editor_fields.values()):
            self._editor_dirty = True
            self.editor_message.set("Є незбережені зміни.")
        return self._editor_dirty

    def _saved_tree_click(self, event):
        iid = self.saved_tree.identify_row(event.y)
        if iid and self.saved_tree.identify_column(event.x) == "#1":
            word = self._saved_iids[iid]
            self._toggle_saved_word_check(word)
            return "break"

    def _toggle_saved_word_check(self, word):
        var = self._saved_check_var(word)
        var.set(not var.get())
        self._update_selected_count()

    def _toggle_selected_saved_word(self, _event=None):
        selection = self.saved_tree.selection()
        if selection:
            self._toggle_saved_word_check(self._saved_iids[selection[0]])
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
        return hashlib.sha256(word.lower().encode()).hexdigest()

    def show_saved_details(self, word):
        entry = self.library.saved_entry(word)
        if not entry:
            return
        self._loading_editor = True
        self._editor_word = word
        self.saved_source_preview.set(f"Гра: {entry.get('source', '')}" if entry.get("source") else "")
        self._editor_dirty = False
        for child in self.saved_list_frame.winfo_children():
            child.destroy()
        frame = self.saved_list_frame
        ttk.Label(frame, text=word, font=(FONT, 27, "bold"), wraplength=500).pack(anchor="w", padx=12, pady=8)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", padx=12)
        ttk.Button(buttons, text=tr_ui("Зберегти (Ctrl+S)"), style="Accent.TButton", command=self.save_saved_details).pack(side="left")
        ttk.Button(buttons, text=tr_ui("Озвучити"), command=lambda: self.speak_saved_word(word)).pack(side="left", padx=6)
        ttk.Button(buttons, text=tr_ui("Видалити"), command=lambda: self.delete_saved_word(word)).pack(side="right")
        self.editor_message = tk.StringVar(value="Зміни зберігаються кнопкою або Ctrl+S.")
        ttk.Label(frame, textvariable=self.editor_message, wraplength=500).pack(anchor="w", padx=12, pady=6)
        row = ttk.Frame(frame)
        row.pack(fill="x", padx=12, pady=6)
        ttk.Label(row, text=tr_ui("Стан:")).pack(side="left")
        states = {k: v for k, v in STATUS_LABELS.items() if v != "all"}
        inverse = {v: k for k, v in states.items()}
        self.editor_status = tk.StringVar(value=inverse.get(entry.get("status", "new"), "Нові"))
        ttk.Combobox(row, textvariable=self.editor_status, values=list(states), width=12,
                     state="readonly").pack(side="left", padx=6)
        self.editor_status.trace_add("write", self._mark_editor_dirty)
        self.editor_fields = {}
        labels = (("translation", "Переклад"), ("definition", "Визначення англійською"),
                  ("definition_tr", "Переклад визначення"), ("example", "Приклад"),
                  ("context", "Речення з гри / контекст"), ("context_tr", "Переклад контексту"),
                  ("source", "Джерело / гра"), ("notes", "Власні нотатки"))
        for key, label in labels:
            ttk.Label(frame, text=label.upper(), foreground="#a2a8b8", font=(FONT, 9)).pack(anchor="w", padx=12, pady=(14, 5))
            text = tk.Text(frame, height=3 if key == "notes" else 2, width=48, wrap="word",
                           font=(FONT, 14 if key == "translation" else 11), undo=True,
                           bg="#1d2029", fg="#f1f1f5", insertbackground="#ffd43b",
                           selectbackground="#ffd43b", selectforeground="#111318",
                           relief="flat", bd=0, padx=12, pady=10, highlightthickness=0)
            text.pack(fill="x", padx=12)
            text.insert("1.0", entry.get(key, ""))
            text.edit_modified(False)
            text.bind("<<Modified>>", self._mark_editor_dirty)
            self.editor_fields[key] = text
        image_buttons = ttk.Frame(frame)
        image_buttons.pack(fill="x", padx=12, pady=10)
        ttk.Button(image_buttons, text=tr_ui("Додати зображення…"), command=self.replace_saved_image).pack(side="left")
        ttk.Button(image_buttons, text=tr_ui("Відкрити зображення"), command=self.open_saved_image).pack(side="left", padx=6)
        self.editor_photo = self.library.context_photo(entry.get("image_file", ""), self.saved_win, size=(480, 250))
        if self.editor_photo:
            label = ttk.Label(frame, image=self.editor_photo, cursor="hand2")
            label.pack(anchor="w", padx=12, pady=8)
            label.bind("<Button-1>", lambda _e: self.open_saved_image())
        self._loading_editor = False
        self._editor_dirty = False
        self._editor_baseline = {key: field.get("1.0", "end-1c")
                                 for key, field in self.editor_fields.items()}
        self._editor_status_baseline = self.editor_status.get()
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
        self._editor_has_unsaved_changes()
        values = {key: field.get("1.0", "end-1c") for key, field in self.editor_fields.items()}
        # A lookup/review can update the same saved entry while this draft is
        # open. Persist only user changes, rather than replacing those updates
        # with untouched values captured when the editor was first shown.
        fields = {key: value for key, value in values.items()
                  if value != self._editor_baseline.get(key, "")}
        if self.editor_status.get() != self._editor_status_baseline:
            fields["status"] = STATUS_LABELS[self.editor_status.get()]
        try:
            entry = (self.library.update_saved_entry(self._editor_word, fields) if fields
                     else self.library.saved_entry(self._editor_word))
            if entry is None:
                raise ValueError("Слово вже видалене з бібліотеки")
        except (OSError, ValueError) as e:
            self.editor_message.set(str(e))
            return False
        self._loading_editor = True
        for key, field in self.editor_fields.items():
            latest = entry.get(key, "")
            if values[key] != latest:
                field.delete("1.0", "end")
                field.insert("1.0", latest)
            field.edit_modified(False)
        inverse = {value: label for label, value in STATUS_LABELS.items()}
        self.editor_status.set(inverse.get(entry.get("status", "new"), "Нові"))
        self._editor_baseline = {key: entry.get(key, "") for key in self.editor_fields}
        self._editor_status_baseline = self.editor_status.get()
        self.saved_source_preview.set(f"Гра: {entry.get('source', '')}" if entry.get("source") else "")
        self._loading_editor = False
        self._editor_dirty = False
        self.editor_message.set("Збережено")
        self._render_saved_list()
        return True

    def replace_saved_image(self):
        if not self._commit_or_discard_editor():
            return
        path = filedialog.askopenfilename(parent=self.saved_win, title=tr_ui("Зображення контексту"),
                                          filetypes=[("Зображення", "*.png *.jpg *.jpeg *.webp *.bmp")])
        if not path:
            return
        name = ""
        try:
            from PIL import Image
            with Image.open(path) as image:
                name = self.library.save_context_image(image)
            if not name:
                raise OSError("Не вдалося записати зображення")
            entry = self.library.saved_entry(self._editor_word)
            old = self.library.context_image_path(entry.get("image_file", ""))
            self.library.update_saved_entry(self._editor_word, {"image_file": name})
            if old and not self.library.context_image_in_use(os.path.basename(old)):
                try:
                    os.remove(old)
                except OSError:
                    self.logger.exception('word_lookup.replace_saved_image')
            self.show_saved_details(self._editor_word)
        except (OSError, ValueError) as e:
            if name and not any(e.get("image_file") == name for e in self.library.load_saved_words()):
                try:
                    os.remove(self.library.context_image_path(name))
                except OSError:
                    self.logger.exception('word_lookup.replace_saved_image')
            self.editor_message.set(f"Зображення не додано: {e}")

    def open_saved_image(self):
        entry = self.library.saved_entry(self._editor_word) if self._editor_word else None
        path = self.library.context_image_path(entry.get("image_file", "")) if entry else None
        if path and os.path.isfile(path):
            platform.open_file(path)
        elif hasattr(self, "editor_message"):
            self.editor_message.set("У цієї картки ще немає зображення.")

    def speak_saved_word(self, word):
        entry = self.library.saved_entry(word)
        audios = entry.get("dictionary", {}).get("audios", []) if entry else []
        if audios and AUDIO_OK:
            self.providers.play_audio(audios[0][1])
        else:
            self.editor_message.set("Озвучення відсутнє або pygame не встановлений.")

    def sync_saved_to_anki(self):
        if not self._commit_or_discard_editor():
            return
        entries = [e for e in self.library.load_saved_words() if self._saved_check_var(e["word"]).get()]
        entries = copy.deepcopy(entries or self._visible_saved_words())
        if not entries:
            self.anki_sync_status.set("Немає слів для передавання")
            return
        deck = self.settings.data["anki"]["deck"]
        self.anki_sync_btn.state(["disabled"])
        self.anki_sync_status.set(f"Передаю {len(entries)} записів у Anki…")
        def worker():
            try:
                self.events.put(as_event(("anki_sync_done", self.anki.sync_anki_entries(entries, deck), "")))
            except Exception as e:
                self.events.put(as_event(("anki_sync_done", None, str(e))))
        threading.Thread(target=worker, daemon=True).start()

    def _saved_check_var(self, word: str):
        key = word.lower()
        var = self.saved_checks.get(key)
        if var is None:
            var = tk.BooleanVar(value=False)
            self.saved_checks[key] = var
        return var

    def _update_selected_count(self):
        n = sum(1 for e in self.library.load_saved_words() if self._saved_check_var(e["word"]).get())
        if hasattr(self, "saved_selected_var"):
            shown = len(self._visible_saved_words())
            self.saved_selected_var.set(f"Знайдено: {shown} · Обрано: {n}")
        if hasattr(self, "saved_tree"):
            for iid, word in self._saved_iids.items():
                self.saved_tree.set(iid, "check", "☑" if self._saved_check_var(word).get() else "☐")

    def review_saved_words(self):
        if not self._commit_or_discard_editor():
            return
        selected = [e for e in self.library.load_saved_words() if self._saved_check_var(e["word"]).get()]
        entries = selected or self._visible_saved_words()
        if not entries:
            self.toast("Спочатку збережи слова або зміни запит пошуку")
            return
        self.review_window = self.review_window(self, entries)

    def _visible_saved_words(self):
        order = {"Спочатку нові": "newest", "Спочатку старі": "oldest", "За абеткою": "az"}
        entries = self.library.filter_saved_words(self.library.load_saved_words(), self.saved_query.get(),
                                      order.get(self.saved_order.get(), "newest"))
        state = STATUS_LABELS[self.saved_state_filter.get()]
        return [e for e in entries if (state == "all" or e.get("status", "new") == state)
                and (not self.saved_due_only.get() or self.library.review_due(e))]

    def select_all_saved(self, value: bool):
        for e in self._visible_saved_words() if value else self.library.load_saved_words():
            self._saved_check_var(e.get("word", "")).set(value)
        self._update_selected_count()

    def export_anki_selected(self):
        self._export_saved("txt")

    def export_anki_apkg_selected(self):
        self._export_saved("apkg")

    def _export_saved(self, kind):
        if not self._commit_or_discard_editor():
            return
        selected = [e for e in self.library.load_saved_words()
                   if self._saved_check_var(e.get("word", "")).get()]
        if not selected:
            self.toast("Спочатку познач слова прапорцями")
            return
        if kind == "apkg":
            if not self.anki.apkg_available():
                messagebox.showinfo(
                    APP_NAME,
                    "Для готового файлу колоди (.apkg) потрібна додаткова бібліотека, якої зараз "
                    "немає в цьому Python:\n\n"
                    f'"{sys.executable}" -m pip install genanki\n\n'
                    "Постав її й спробуй ще раз. А поки що можна скористатись кнопкою "
                    "«→ Anki (.txt)» — вона працює без додаткових бібліотек.")
                return
            path = filedialog.asksaveasfilename(
                title=tr_ui("Експорт в Anki (готова колода)"), defaultextension=".apkg",
                filetypes=[("Колода Anki", "*.apkg"), ("Усі файли", "*.*")],
                initialfile="word_lookup_anki.apkg")
        else:
            path = filedialog.asksaveasfilename(
                title=tr_ui("Експорт в Anki"), defaultextension=".txt",
                filetypes=[("Текстовий файл для Anki", "*.txt"), ("Усі файли", "*.*")],
                initialfile="word_lookup_anki.txt")
        if not path:
            return
        need = sum(1 for e in selected if self.anki._needs_enrich(e))
        if need:  # старі слова без пояснень — дотягуємо (мережа), тож у фоновому потоці
            self.toast(f"Підтягую переклад і пояснення для {need} слів — зачекай…", timeout=20)
        threading.Thread(target=self._export_worker, args=(kind, selected, path),
                         daemon=True).start()

    def _export_worker(self, kind, selected, path):
        err = ""
        try:
            self.anki.enrich_entries(selected)
            (self.anki.export_anki_apkg if kind == "apkg" else self.anki.export_anki_txt)(selected, path)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        self.events.put(as_event(("export_done", kind, len(selected), path, err)))

    def _export_finished(self, kind, n, path, err):
        if hasattr(self, "saved_list_frame"):  # у списку з'являться дотягнуті переклади
            try:
                self._render_saved_list()
            except tk.TclError:
                self.logger.exception('word_lookup._export_finished')
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
        messagebox.showinfo(
            APP_NAME,
            f"Збережено {n} слів:\n{path}\n\nПерші рядки файлу (слово ⇥ зворотна сторона):\n"
            f"{preview}\n\nУ вікні імпорту Anki перевір блок зіставлення полів: «Зворотна "
            "сторона» (Back) має бути прив'язана до стовпця 2. Якщо цих слів у колоді вже "
            "є — видали старі картки або вибери оновлення існуючих нотаток, інакше Anki "
            "може їх пропустити й зворотна сторона лишиться порожньою.")

    def _render_saved_list(self):
        if getattr(self, "_library_reloading", False):
            return
        total = len(self.library.load_saved_words())
        self.library_title.set(f"Бібліотека · {total} слів")
        selected = self.saved_tree.selection()
        previous = self._saved_iids.get(selected[0]) if selected else self._editor_word
        self.saved_tree.delete(*self.saved_tree.get_children())
        self._saved_iids = {}
        inverse = {v: k for k, v in STATUS_LABELS.items()}
        visible = self._visible_saved_words()
        info = self.library.saved_words_error() or f"Показано {len(visible)} з {total} слів."
        if total and not visible:
            info += " Натисни «Оновити», щоб скинути фільтри."
        self.library_storage_info.set(info + "\nФайл: " + self.library._saved_words_path())
        for entry in visible:
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
                entry = self.library.saved_entry(previous)
                self.editor_status.set(inverse.get(entry.get("status", "new"), "Нові"))
                self._editor_status_baseline = self.editor_status.get()
                self._loading_editor = False
        elif self.saved_tree.get_children() and not self._editor_dirty:
            self.saved_tree.selection_set(self.saved_tree.get_children()[0])
        if not self.library.load_saved_words():
            self.saved_source_preview.set("")
            for child in self.saved_list_frame.winfo_children():
                child.destroy()
            self._editor_word = None
            self._editor_dirty = False
            message = self.library.saved_words_error() or ("Бібліотека порожня. Натисни ⭐ у картці слова чи фрази "
                "або «Відновити з файлу…», якщо слова були збережені раніше.")
            ttk.Label(self.saved_list_frame, text=message,
                      wraplength=450).pack(padx=12, pady=20)
        self._update_selected_count()

    def open_saved_word(self, word):
        entry = self.library.saved_entry(word)
        if entry is not None:
            card = self.library.saved_word_card(entry)
            if self.selector:
                self.selector.finish(None)
            if self.free_cursor:
                self.free_cursor.finish(None)
            self._lookup_profile = {}
            self.dismiss_lookup()
            self.show_lookup(card, None)

    def delete_saved_word(self, word):
        if not self.library.remove_saved_word(word):
            self.toast("Не вдалося видалити запис; перевір журнал і доступ до файлу")
            return
        self.saved_checks.pop(word.lower(), None)
        if getattr(self, "_editor_word", None) == word:
            self._editor_word = None
            self._editor_dirty = False
        self.refresh_saved_count()
