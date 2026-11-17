"""Game profile settings, kept separate from the main settings/library view."""
from word_lookup_i18n import _ as tr_ui
import tkinter as tk
from tkinter import ttk


_MODE_LABELS = {"Клавіатура": "keyboard", "Наведення мишею": "hover"}


class GameProfilesViewMixin:
    def _build_profiles_settings(self, parent):
        box = ttk.LabelFrame(parent, text=tr_ui("Профілі ігор"), padding=10)
        box.pack(fill="x", pady=(10, 0))
        ttk.Label(box, text=tr_ui("Профіль діє для активного .exe при наступному пошуку. "
                          "Для ігор без профілю діють загальні налаштування."),
                          wraplength=520, foreground="#a2a8b8").pack(anchor="w")
        self.profile_tree = ttk.Treeview(box, columns=("mode", "pause"),
                                               height=4, selectmode="browse")
        self.profile_tree.heading("#0", text=tr_ui("Процес гри"))
        self.profile_tree.heading("mode", text=tr_ui("Вибір слова"))
        self.profile_tree.heading("pause", text=tr_ui("Пауза"))
        self.profile_tree.column("#0", width=210, minwidth=120)
        self.profile_tree.column("mode", width=140, minwidth=100)
        self.profile_tree.column("pause", width=65, minwidth=50)
        self.profile_tree.pack(fill="x", pady=8)
        self.profile_tree.bind("<<TreeviewSelect>>", self._profile_selected)

        editor = ttk.Frame(box)
        editor.pack(fill="x")
        editor.columnconfigure(1, weight=1)
        self.profile_exe_var = tk.StringVar(master=self.root)
        self.profile_mode_var = tk.StringVar(master=self.root)
        self.profile_width_var = tk.StringVar(master=self.root)
        self.profile_height_var = tk.StringVar(master=self.root)
        self.profile_pause_var = tk.BooleanVar(master=self.root, value=False)
        self.profile_tag_var = tk.StringVar(master=self.root)
        ttk.Label(editor, text=tr_ui("Процес (.exe):")).grid(row=0, column=0, sticky="w", pady=3)
        self.profile_exe_entry = ttk.Entry(editor, textvariable=self.profile_exe_var)
        self.profile_exe_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=3)
        ttk.Label(editor, text=tr_ui("Вибір слова:")).grid(row=1, column=0, sticky="w", pady=3)
        ttk.Combobox(editor, textvariable=self.profile_mode_var,
                             values=tuple(_MODE_LABELS), state="readonly", width=23
                             ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=3)
        ttk.Label(editor, text=tr_ui("Область OCR, px:")).grid(row=2, column=0, sticky="w", pady=3)
        sizes = ttk.Frame(editor)
        sizes.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=3)
        ttk.Spinbox(sizes, from_=200, to=2000, increment=50, width=7,
                            textvariable=self.profile_width_var).pack(side="left")
        ttk.Label(sizes, text=" × ").pack(side="left")
        ttk.Spinbox(sizes, from_=40, to=600, increment=10, width=7,
                            textvariable=self.profile_height_var).pack(side="left")
        ttk.Label(editor, text=tr_ui("Тег Anki:")).grid(row=3, column=0, sticky="w", pady=3)
        ttk.Entry(editor, textvariable=self.profile_tag_var).grid(
            row=3, column=1, sticky="ew", padx=(8, 0), pady=3)
        ttk.Checkbutton(editor, text=tr_ui("Призупиняти гру (тривалість із загальних налаштувань)"),
                               variable=self.profile_pause_var).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(editor, text=tr_ui("Спершу перевір на несуттєвій одиночній грі. "
                          "Не вмикай паузу в онлайн-іграх."), wraplength=520,
                          foreground="#a2a8b8").grid(row=5, column=0, columnspan=2,
                                                     sticky="w", pady=(3, 0))
        actions = ttk.Frame(box)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text=tr_ui("Новий"), command=self._profile_new).pack(side="left")
        ttk.Button(actions, text=tr_ui("Зберегти профіль"), command=self._profile_save).pack(
            side="left", padx=6)
        ttk.Button(actions, text=tr_ui("Видалити профіль"), command=self._profile_delete).pack(side="left")
        self.profile_status_var = tk.StringVar(master=self.root)
        ttk.Label(box, textvariable=self.profile_status_var, wraplength=520).pack(
            anchor="w", pady=(5, 0))
        self._profile_refresh_list()
        self._profile_new()

    def _profile_refresh_list(self, selected=""):
        self.profile_tree.delete(*self.profile_tree.get_children())
        for name, profile in sorted(self.settings.data.get("game_profiles", {}).items()):
            label = next((label for label, mode in _MODE_LABELS.items()
                          if mode == profile.get("selection_mode")), "Загальне")
            self.profile_tree.insert("", "end", iid=name, text=name,
                                     values=(label, "Так" if profile.get("pause_game") else "Ні"))
        if selected and self.profile_tree.exists(selected):
            self.profile_tree.selection_set(selected)
            self.profile_tree.focus(selected)

    def _profile_new(self):
        self.profile_tree.selection_remove(*self.profile_tree.selection())
        self.profile_exe_var.set("")
        mode = self.settings.data["freeze"].get("selection_mode", "keyboard")
        self.profile_mode_var.set(next((label for label, value in _MODE_LABELS.items()
                                        if value == mode), "Клавіатура"))
        self.profile_width_var.set(str(self.settings.data["ocr"]["region_w"]))
        self.profile_height_var.set(str(self.settings.data["ocr"]["region_h"]))
        self.profile_pause_var.set(False)
        self.profile_tag_var.set("")
        self.profile_status_var.set("Новий профіль: уведи точну назву процесу гри.")

    def _profile_selected(self, _event=None):
        selection = self.profile_tree.selection()
        if not selection:
            return
        name = selection[0]
        profile = self.settings.data.get("game_profiles", {}).get(name)
        if profile is None:
            return
        self.profile_exe_var.set(name)
        self.profile_mode_var.set(next((label for label, mode in _MODE_LABELS.items()
                                        if mode == profile.get("selection_mode")), "Клавіатура"))
        self.profile_width_var.set(str(profile.get("region_w", self.settings.data["ocr"]["region_w"])))
        self.profile_height_var.set(str(profile.get("region_h", self.settings.data["ocr"]["region_h"])))
        self.profile_pause_var.set(bool(profile.get("pause_game", False)))
        self.profile_tag_var.set(profile.get("anki_tag", ""))
        self.profile_status_var.set(f"Редагується {name}")

    def _profile_save(self):
        from word_lookup_profiles import normalize_profile
        try:
            key, profile = normalize_profile(self.profile_exe_var.get(), {
                "selection_mode": _MODE_LABELS.get(self.profile_mode_var.get(), ""),
                "region_w": int(self.profile_width_var.get()),
                "region_h": int(self.profile_height_var.get()),
                "pause_game": self.profile_pause_var.get(),
                "anki_tag": self.profile_tag_var.get(),
            })
        except (ValueError, TypeError) as error:
            self.profile_status_var.set(f"Не збережено: {error}")
            return False
        self.settings.data.setdefault("game_profiles", {})[key] = profile
        self._schedule_config_save()
        self._profile_refresh_list(key)
        self.profile_exe_var.set(key)
        self.profile_status_var.set(f"Збережено {key}. Застосується при наступному пошуку.")
        return True

    def _profile_delete(self):
        selection = self.profile_tree.selection()
        if not selection:
            self.profile_status_var.set("Вибери профіль у списку, щоб видалити.")
            return False
        key = selection[0]
        self.settings.data.get("game_profiles", {}).pop(key, None)
        self._schedule_config_save()
        self._profile_refresh_list()
        self._profile_new()
        self.profile_status_var.set(f"Профіль {key} видалено. Знову діють загальні налаштування.")
        return True
