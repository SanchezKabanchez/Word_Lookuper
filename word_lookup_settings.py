"""Network permissions and diagnostic-log controls for the settings window."""
from word_lookup_i18n import _ as tr_ui
import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import pyperclip
from word_lookup_version import __version__
from word_lookup_diagnostics import environment_info
from word_lookup_theme import (APP_NAME)
import word_lookup_platform as platform

from word_lookup_models import Notice


class PrivacySettingsMixin:
    def save_diagnostic_package(self):
        filename = filedialog.asksaveasfilename(parent=self.root, title=tr_ui("Зберегти діагностичний пакет"),
            defaultextension=".zip", initialfile=f"word-lookup-diagnostics-{__version__}.zip",
            filetypes=[("Діагностичний пакет ZIP", "*.zip")])
        if not filename:
            return
        metadata = environment_info(self.root)  # Tk calls stay on the UI thread.
        report = self.support.diagnostic_report()
        library = {"path": self.library._saved_words_path(), "count": len(self.library.load_saved_words()),
                   "read_error": self.library.saved_words_error()}
        def work():
            try:
                self.diagnostics.export(filename, report, metadata, library)
                message = "Діагностичний пакет збережено. Переглянь перед надсиланням."
            except (OSError, ValueError):
                self.logger.exception("Збереження діагностичного пакета")
                message = "Не вдалося записати пакет. Перевір папку й журнал."
            self.events.put(Notice(message))
        threading.Thread(target=work, name="diagnostic-export", daemon=True).start()

    def _build_network_settings(self, parent):
        box = ttk.LabelFrame(parent, text=tr_ui("Мережа й джерела даних"), padding=10)
        box.pack(fill="x", pady=(12, 0))
        ttk.Label(box, text=tr_ui("Онлайн-пошук надсилає слово, а для перекладу контексту — речення. "
                          "Знімки екрана залишаються локально. Вимкнене джерело не отримує нових запитів; "
                          "уже надісланий запит може завершитися."), wraplength=520).pack(anchor="w")
        ttk.Checkbutton(box, text=tr_ui("Офлайн — лише локальний словник, бібліотека й кеш"),
                               variable=self._var(tk.BooleanVar, ("network", "offline"))
                               ).pack(anchor="w", pady=(8, 4))
        for key, label in (("google", "Google (неофіційні адреси, доступність може змінюватися)"),
                           ("mymemory", "MyMemory"), ("datamuse", "Datamuse (слова й синоніми)"),
                           ("dictionaryapi", "dictionaryapi.dev (визначення й аудіо)"),
                           ("deepl", "DeepL (потрібен увімкнений ключ нижче)")):
            ttk.Checkbutton(box, text=label, variable=self._var(
                tk.BooleanVar, ("network", "providers", key))).pack(anchor="w", pady=2)
        buttons = ttk.Frame(box)
        buttons.pack(anchor="w", pady=(8, 0))
        for label, mode in (("Усі джерела", "all"), ("Лише DeepL", "deepl"), ("Лише локально", "offline")):
            ttk.Button(buttons, text=label, command=lambda m=mode: self._network_preset(m)
                               ).pack(side="left", padx=(0, 6))

    def _network_preset(self, mode):
        offline = mode == "offline"
        path = ("network", "offline")
        self._setting_vars[path].set(offline)
        for provider in self.settings.data["network"]["providers"]:
            self._setting_vars[("network", "providers", provider)].set(mode == "all" or provider == "deepl")
        self._schedule_config_save()

    def copy_log(self):
        if not messagebox.askyesno(APP_NAME,
                tr_ui("Звіт містить шукані слова, частини речень і налаштування. Ключ DeepL приховано. "
                "Переглянь текст перед надсиланням іншій людині. Скопіювати звіт?"), parent=self.root):
            return
        report = self.support.diagnostic_report()
        try:
            pyperclip.copy(report)
        except Exception as error:
            messagebox.showinfo(APP_NAME, f"Не вдалося скопіювати: {error}")
            return
        messagebox.showinfo(APP_NAME, tr_ui("Звіт скопійовано. Переглянь його перед надсиланням."))

    def open_log(self):
        if not os.path.exists(self.support.log_path()):
            messagebox.showinfo(APP_NAME, tr_ui("Журнал ще порожній."))
            return
        platform.open_file(self.support.log_path())

    def clear_log(self):
        try:
            self.support.clear_log()
        except OSError as error:
            messagebox.showerror(APP_NAME, f"Не вдалося очистити журнал: {error}")
            return
        messagebox.showinfo(APP_NAME, tr_ui("Журнал і його попередню копію очищено."))

    def _build_ocr_budget_settings(self, parent):
        box = ttk.LabelFrame(parent, text=tr_ui("Навантаження OCR"), padding=10)
        box.pack(fill="x", pady=(12, 0))
        options = ((('ocr', 'cpu_threads'), 'Потоків CPU для OCR', 1, 4, 1),
                   (('ocr', 'max_side'), 'Максимальна сторона OCR-знімка, px', 640, 3200, 160),
                   (('performance', 'hover_delay_ms'), 'Затримка наведення перед OCR, мс', 100, 1000, 50))
        for row, (path, label, low, high, step) in enumerate(options):
            ttk.Label(box, text=label).grid(row=row, column=0, sticky='w', pady=3)
            ttk.Spinbox(box, from_=low, to=high, increment=step, width=7,
                textvariable=self._var(tk.IntVar, path)).grid(row=row, column=1, sticky='w', padx=8)
        ttk.Checkbutton(box, text=tr_ui('Для вибору слова захоплювати лише активний монітор'),
            variable=self._var(tk.BooleanVar, ('performance', 'capture_active_monitor'))
        ).grid(row=3, column=0, columnspan=2, sticky='w', pady=(6, 0))
        ttk.Label(box, text=tr_ui('Типово 2 потоки й один OCR-прохід за раз. Для слабшого ПК можна вибрати 1. '
            'Менший знімок економить CPU, але може погіршити розпізнавання дрібного тексту. '
            'Параметри застосовуються до наступного OCR; вже запущений прохід завершується.'),
            foreground='#a2a8b8', wraplength=520, justify='left'
        ).grid(row=4, column=0, columnspan=2, sticky='w', pady=(5, 0))
