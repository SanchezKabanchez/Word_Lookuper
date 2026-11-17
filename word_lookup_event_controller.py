from __future__ import annotations
import queue
import time
from tkinter import messagebox
from word_lookup_theme import (APP_NAME, SAMPLE_CARD, TRAY_OK)
from word_lookup_utils import clamp
from word_lookup_models import (ServiceStatus, Notice,
    as_event, ScopedEvent, ShowCard, UpdateCard, LookupFailure, ShowLoading, HighlightReady,
    EscapePressed, FreeCursorKey, ActionRequested, RecordingStatus, MouseInput,
    DiagnosticReady, ControlStatus, ExportFinished, AnkiSyncFinished, TrayCommand)
import word_lookup_platform as platform
try:
    if TRAY_OK:
        import pystray
except (ImportError, RuntimeError):
    pystray = None


class EventController:
    def _pump(self):
        self.diagnostics.sample()
        if self.instance is not None and self.instance.activation_requested():
            self.show_window()
        processed = 0
        deadline = time.monotonic() + 0.008
        try:
            while processed < 100 and time.monotonic() < deadline:
                ev = self.events.get_nowait()
                processed += 1
                try:
                    self._handle(ev)
                except Exception:
                    self._resume_game()
                    self.logger.exception()
        except queue.Empty:
            # Poll exhaustion is expected; schedule the next tick below.
            ev = None
        if self.free_cursor is not None:
            with self._fc_lock:
                dx, dy = self._fc_delta
                self._fc_delta[0] = self._fc_delta[1] = 0
            if dx or dy:
                self.free_cursor.nudge(dx, dy)
        if self._game_pause is not None and not self._game_pause.active:
            ok = self._resume_game()
            self.toast("Пауза гри завершена (ліміт часу або закриття процесу)" if ok
                       else "Не підтверджено відновлення гри — перевір журнал", timeout=6)
        self.root.after(self._pump_delay_ms(), self._pump)


    def _pump_delay_ms(self):
        if not self.events.empty():
            return 10
        if self.selector or self.free_cursor or self.popup or self._game_pause or self.recording:
            return 40
        return int(clamp(self.settings.data.get("performance", {}).get("idle_poll_ms", 120), 80, 500))


    def _handle(self, ev):
        ev = as_event(ev)
        if isinstance(ev, ServiceStatus):
            self.translator_settings.handle_status(ev)
            return
        if isinstance(ev, Notice):
            self.toast(ev.message)
            return
        if isinstance(ev, ScopedEvent):
            if ev.token == self.active_token:
                self._handle(ev.event)
            return
        if isinstance(ev, ShowCard):
            card, hit, anchor, token = ev.card, ev.hit, ev.anchor, ev.token
            if token is not None and token != self.active_token:
                return  # новіший пошук уже почався — цей результат застарів
            if token is not None:
                self.active_token = token
                profile = self._lookup_profiles.get(token, {})
                if profile.get("anki_tag"):
                    card = {**card, "anki_tag": profile["anki_tag"]}
            self.show_lookup(card, hit, anchor)
        elif isinstance(ev, UpdateCard):
            token, word, full_card = ev.token, ev.word, ev.card
            profile = self._lookup_profiles.get(token, {})
            if profile.get("anki_tag"):
                full_card = {**full_card, "anki_tag": profile["anki_tag"]}
            if (token == self.active_token and self.popup
                    and self.popup.card.get("kind") == "word"
                    and self.popup.card.get("word") == word):
                self.popup.card = full_card
                if self.library.saved_entry(word) and self.library.add_saved_word(full_card):
                    self.refresh_saved_count()
                self.rerender(self.popup)
                self.last_card = full_card
                self._refresh_cache_label()
        elif isinstance(ev, ShowLoading):
            self.show_loading(ev.anchor)
        elif isinstance(ev, LookupFailure):
            if self._game_pause is not None:
                self._resume_game()
            if self.highlight and not self.popup:  # пошук не вдався — прибираємо підсвічування
                self.highlight.close()
                self.highlight = None
            self.toast(ev.message)
        elif isinstance(ev, HighlightReady):
            if ev.token == self.active_token:
                self.show_highlight(ev.hit)
        elif isinstance(ev, EscapePressed):
            if self.popup and hasattr(self.popup, "correction_row") and self.popup.correction_row.winfo_ismapped():
                self.popup.cancel_word_edit()
                return
            if time.monotonic() < getattr(self, "_word_edit_escape_until", 0):
                self._word_edit_escape_until = 0
                return
            if self.selector:
                self.selector.finish(None)
            if self.free_cursor:
                self.free_cursor.finish(None)
            self.dismiss_lookup()
        elif isinstance(ev, FreeCursorKey):
            if self.free_cursor:
                fast = "shift" in self.manager.down
                self.free_cursor.key(ev.key, fast)
        elif isinstance(ev, ActionRequested):
            self.do_action(ev.action)
        elif isinstance(ev, RecordingStatus):
            self.handle_rec(ev.state, ev.text)
        elif isinstance(ev, MouseInput) and ev.phase == "press":
            x, y, btn, mods = ev.x, ev.y, ev.button, ev.modifiers
            keep_preview = (self.popup and self.popup.card is SAMPLE_CARD
                            and self.inside_main_window(x, y))
            if self.popup and not self.popup.pinned and not self.popup.contains(x, y) and not keep_preview:
                self.dismiss_lookup()
            if not self.paused and not self.selector and not self.free_cursor and self._mouse_match(btn, mods):
                self._start_lookup("word", pos=(x, y))
            if btn == "left":
                self.auto_state["press"] = (x, y)
        elif isinstance(ev, MouseInput) and ev.phase == "release":
            self._handle_auto_release(ev.x, ev.y, ev.button)
        elif isinstance(ev, DiagnosticReady):
            text, path = ev.text, ev.path
            self.logger.write(text)
            if path:
                platform.open_file(path)
                text += f"\n\nЗнімок із рамками слів: {path}"
            messagebox.showinfo(APP_NAME, text)
        elif isinstance(ev, ControlStatus):
            {"dict": self.dict_var, "ocr": self.ocr_var, "deepl": self.deepl_var}[ev.control].set(ev.text)
        elif isinstance(ev, ExportFinished):
            self._export_finished(ev.format, ev.count, ev.path, ev.error)
        elif isinstance(ev, AnkiSyncFinished):
            self.anki_sync_btn.state(["!disabled"])
            result, err = ev.result, ev.error
            if err:
                self.anki_sync_status.set(err)
            else:
                self.anki_sync_status.set(f"Додано: {result['added']} · Оновлено: {result['updated']} · "
                                          f"Помилок: {result['failed']}")
                if result["errors"]:
                    messagebox.showwarning(APP_NAME, "\n".join(result["errors"][:10]), parent=self.saved_win)
        elif isinstance(ev, TrayCommand):
            if ev.command == "show":
                self.show_window()
            elif ev.command == "pause":
                self.toggle_pause()
            elif ev.command == "quit":
                self.quit()

