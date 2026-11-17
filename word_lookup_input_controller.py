from __future__ import annotations
import copy
import time
from tkinter import messagebox
import pyperclip
from word_lookup_theme import (APP_NAME, ACTIONS, MOUSE_BTN_ALIASES, TRAY_OK)
from word_lookup_config import DEFAULT_CONFIG
from word_lookup_utils import clamp
from word_lookup_input import pretty, combo_problem, MOD_KEYS, COPY_MOD
from word_lookup_models import (as_event)
try:
    if TRAY_OK:
        import pystray
except (ImportError, RuntimeError):
    pystray = None


class InputController:
    def start_record(self, action):
        if self.recording:
            self.finish_record()
        self.recording = action
        self.key_btns[action].config(text="Натисни…")
        self.key_vars[action].set("натисни комбінацію")
        self.manager.start_recording(lambda kind, text: self.events.put(as_event(("rec", kind, text))))


    def finish_record(self):
        action, self.recording = self.recording, None
        self.manager.recorder = None
        if action:
            self.key_btns[action].config(text="Записати")
            self.key_vars[action].set(pretty(self.settings.data["hotkeys"][action]))


    def set_hotkey(self, action, combo) -> bool:
        if combo:
            for a, c in self.settings.data["hotkeys"].items():
                if c == combo and a != action:
                    label = dict(ACTIONS)[a]
                    messagebox.showwarning(APP_NAME, f"«{pretty(combo)}» вже призначено для дії:\n{label}")
                    return False
        self.settings.data["hotkeys"][action] = combo
        self.settings.save_config(self.settings.data)
        self.manager.set_bindings(self.settings.data["hotkeys"])
        self.key_vars[action].set(pretty(combo))
        self._refresh_hint()
        return True


    def reset_hotkeys(self):
        self.finish_record()
        self.settings.data["hotkeys"] = copy.deepcopy(DEFAULT_CONFIG["hotkeys"])
        self.settings.save_config(self.settings.data)
        self.manager.set_bindings(self.settings.data["hotkeys"])
        for a, v in self.key_vars.items():
            v.set(pretty(self.settings.data["hotkeys"][a]))
        self._refresh_hint()


    def handle_rec(self, kind, text):
        if not self.recording:
            return
        action = self.recording
        if kind == "progress":
            self.key_vars[action].set((pretty(text) + " + …") if text else "натисни комбінацію")
        elif kind == "cancel":
            self.finish_record()
        elif kind == "done":
            problem = combo_problem(text)
            self.finish_record()
            if problem:
                messagebox.showwarning(APP_NAME, problem)
            else:
                self.set_hotkey(action, text)


    def _copy_selection(self) -> str:
        try:
            old = pyperclip.paste()
        except Exception:
            old = ""
        t0 = time.time()
        while self.manager.keys_down() and time.time() - t0 < 0.7:  # чекаємо відпускання клавіш
            time.sleep(0.02)
        for name in list(self.manager.down):
            if name in MOD_KEYS:
                self.kb.release(MOD_KEYS[name])
        marker = f"__word_lookup_{time.time()}__"
        pyperclip.copy(marker)
        self.manager.ignore_until = time.time() + 0.8
        with self.kb.pressed(COPY_MOD):
            self.kb.press("c")
            self.kb.release("c")
        text = ""
        for _ in range(15):
            time.sleep(0.03)
            try:
                cur = pyperclip.paste()
            except Exception:
                continue
            if cur != marker:
                text = cur
                break
        try:
            pyperclip.copy(old)
        except Exception:
            self.logger.exception('word_lookup._copy_selection')
        return text.strip()


    def _handle_auto_release(self, x, y, btn):
        """Ліва кнопка: перетягування (виділення) або подвійний клік -> показати словник/переклад,
        не чекаючи гарячої клавіші. Працює лише там, де текст можна виділити й скопіювати
        (браузер, PDF, Word) — на відміну від OCR-пошуку, який працює і в іграх."""
        if btn != "left" or not self.settings.data["auto"]["enabled"] or self.paused or self.selector or self.free_cursor:
            return
        if self.popup and self.popup.contains(x, y):
            return  # клік по самій картці (кнопки 🔊, «more») — не запускати повторно
        if self.inside_main_window(x, y):
            return  # клік у вікні налаштувань — не текстовий виділений фрагмент
        px, py = self.auto_state["press"]
        moved = abs(x - px) + abs(y - py) > int(clamp(self.settings.data["auto"]["drag_px"], 2, 100))
        now = time.time()
        double_click = (not moved
                        and now - self.auto_state["last_release"] < clamp(
                            self.settings.data["auto"]["double_click_s"], 0.1, 1.0))
        self.auto_state["last_release"] = now
        if moved or double_click:
            # невелика затримка: додаток під курсором ще фіксує виділення
            token = self._new_token()
            self.root.after(80, lambda: self._start_lookup("selection", pos=(x, y))
                            if token == self.active_token and not self.paused else None)


    def _mouse_match(self, btn_name, mods) -> bool:
        want = self.settings.data["mouse"]["button"]
        if want == "none" or btn_name not in MOUSE_BTN_ALIASES.get(want, ()):
            return False
        mod = self.settings.data["mouse"]["modifier"]
        return mod == "none" or mod in mods


    def _on_click(self, x, y, button, pressed):
        kind = "press" if pressed else "release"
        self.events.put(as_event((kind, x, y, button.name, set(self.manager.down))))


    def toggle_pause(self):
        self.paused = not self.paused
        if self.paused:
            if self.selector:
                self.selector.finish(None)
            if self.free_cursor:
                self.free_cursor.finish(None)
            self.dismiss_lookup()
        self._refresh_status()
        self.toast("Пауза" if self.paused else "Працює")

