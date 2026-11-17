"""Translator controls with explicit services and no import-time I/O."""
from word_lookup_i18n import _ as tr_ui
import threading
import tkinter as tk
from tkinter import filedialog, ttk
import webbrowser

from word_lookup_models import ServiceStatus


class TranslatorSettingsView:
    def __init__(self, parent, *, root, settings, variable, credentials, save_key,
                 official, offline, target_language, events, log_error, reset_cooldown):
        self.root, self.settings, self.variable = root, settings, variable
        self.credentials, self.save_key = credentials, save_key
        self.official, self.offline, self.target_language = official, offline, target_language
        self.events, self.log_error, self.reset_cooldown = events, log_error, reset_cooldown
        self.status = {}
        self._api_box(parent, "googlecloud", "Google Cloud Translation — офіційний API")
        self._api_box(parent, "libretranslate", "LibreTranslate — власний сервер або API")
        self._offline_box(parent)

    def _api_box(self, parent, provider, title):
        box = ttk.LabelFrame(parent, text=title, padding=10)
        box.pack(fill="x", pady=(12, 0))
        ttk.Checkbutton(box, text=tr_ui("Дозволити цьому джерелу отримувати слово й контекст"),
            variable=self.variable(tk.BooleanVar, ("network", "providers", provider))).pack(anchor="w")
        if provider == "libretranslate":
            ttk.Label(box, text=tr_ui("HTTPS або локальний HTTP, наприклад http://127.0.0.1:5000")
                      ).pack(anchor="w", pady=(7, 3))
            ttk.Entry(box, textvariable=self.variable(tk.StringVar, (provider, "url"))).pack(fill="x")
        ttk.Label(box, text="API-ключ" + (" (локальний сервер може працювати без ключа)"
                  if provider == "libretranslate" else "")).pack(anchor="w", pady=(6, 0))
        entry = ttk.Entry(box, show="•", textvariable=self.variable(tk.StringVar, (provider, "key"),
            after=lambda: self._changed(provider)))
        entry.pack(fill="x", pady=6)
        buttons = ttk.Frame(box)
        buttons.pack(fill="x")
        ttk.Button(buttons, text=tr_ui("Зберегти ключ"), command=lambda: self._save(provider)).pack(side="left")
        ttk.Button(buttons, text=tr_ui("Перевірити (Hello)"), command=lambda: self._test(provider)
                   ).pack(side="left", padx=6)
        self.status[provider] = tk.StringVar(value=self.credentials.status(provider))
        ttk.Label(box, textvariable=self.status[provider], wraplength=510, justify="left"
                  ).pack(anchor="w", pady=(6, 0))
        if provider == "googlecloud":
            ttk.Label(box, text=tr_ui("Потрібен увімкнений Cloud Translation Basic API та білінг Google Cloud. "
                "Ключ зберігається у сховищі ОС. Перевірка надсилає лише слово Hello."),
                wraplength=510, justify="left").pack(anchor="w", pady=(5, 0))

    def _offline_box(self, parent):
        box = ttk.LabelFrame(parent, text=tr_ui("Локальні моделі Argos / CTranslate2 (необов'язково)"), padding=10)
        box.pack(fill="x", pady=(12, 0))
        ttk.Checkbutton(box, text=tr_ui("Використовувати встановлені локальні моделі"),
            variable=self.variable(tk.BooleanVar, ("offline_translation", "enabled"),
                after=lambda: self.reset_cooldown("Argos (локально)"))).pack(anchor="w")
        row = ttk.Frame(box)
        row.pack(fill="x", pady=6)
        ttk.Label(row, text=tr_ui("Потоків CPU:")).pack(side="left")
        ttk.Spinbox(row, from_=1, to=4, width=5,
            textvariable=self.variable(tk.IntVar, ("offline_translation", "cpu_threads"))).pack(side="left", padx=6)
        ttk.Label(box, text=tr_ui("У Python-версії спершу запусти install_offline.bat (в EXE рушій уже є), завантаж модель з каталогу "
            "й імпортуй .argosmodel із sentencepiece.model. Потрібна пара English → обрана мова. Моделі займають додаткову "
            "пам'ять і місце. Зміна потоків діє після перезапуску. Текст лишається на ПК; "
            "автоматичного завантаження моделей немає."), wraplength=510, justify="left").pack(anchor="w")
        buttons = ttk.Frame(box)
        buttons.pack(fill="x", pady=7)
        ttk.Button(buttons, text=tr_ui("Каталог моделей (браузер)"), command=lambda: webbrowser.open(
            "https://www.argosopentech.com/argospm/index/")).pack(side="left")
        ttk.Button(buttons, text=tr_ui("Імпортувати модель…"), command=self._import_model).pack(side="left", padx=6)
        self.status["argos"] = tk.StringVar(value="Додаток доступний." if self.offline.available() else
                                            "Додаток не встановлено: запусти install_offline.bat.")
        ttk.Label(box, textvariable=self.status["argos"], wraplength=510).pack(anchor="w")

    def _changed(self, provider):
        self.reset_cooldown("Google Cloud" if provider == "googlecloud" else "LibreTranslate")
        if provider in self.status:
            self.status[provider].set("")

    def _save(self, provider):
        self.status[provider].set(self.save_key(provider))

    def _test(self, provider):
        self.status[provider].set("Перевіряю…")
        def work():
            try:
                method = self.official.google_cloud if provider == "googlecloud" else self.official.libre_translate
                translated, _groups = method("Hello", "en")
                message = "API відповідає: " + translated if translated else "Вкажи ключ і дозволь джерело вище."
            except Exception:
                self.log_error("Перевірка " + provider)
                message = "Не вдалося перевірити. Перевір дозвіл, адресу, ключ і журнал."
            self.events.put(ServiceStatus(provider, message))
        threading.Thread(target=work, name="translator-check", daemon=True).start()

    def _import_model(self):
        path = filedialog.askopenfilename(parent=self.root, title=tr_ui("Вибери модель Argos"),
                                         filetypes=[("Argos model", "*.argosmodel")])
        if not path:
            return
        self.status["argos"].set("Встановлюю локальну модель…")
        def work():
            try:
                self.offline.install_model(path)
                self.reset_cooldown("Argos (локально)")
                message = "Модель встановлено. Увімкни Argos і вибери відповідну мову."
            except Exception:
                self.log_error("Імпорт моделі Argos")
                message = "Не вдалося встановити модель. Перевір офлайн-додаток, файл та журнал."
            self.events.put(ServiceStatus("argos", message))
        threading.Thread(target=work, name="model-install", daemon=True).start()

    def handle_status(self, event):
        if event.service in self.status:
            self.status[event.service].set(event.message)
