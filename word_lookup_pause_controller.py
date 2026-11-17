from word_lookup_selection import RegionSelector
import copy
import threading
import tkinter as tk
from pynput import mouse
from word_lookup_theme import (TRAY_OK)
from word_lookup_utils import clamp
from word_lookup_input import stop_input_listener
from word_lookup_models import (as_event)
from word_lookup_pause import GamePauseGuard
from word_lookup_ui import FreeCursorOverlay
import word_lookup_platform as platform
try:
    if TRAY_OK:
        import pystray
except (ImportError, RuntimeError):
    pystray = None


class PauseController:
    def _game_mode_changed(self):
        automatic = self.settings.data["freeze"]["auto_game"]
        self.game_exe_entry.configure(state="disabled" if automatic else "normal")


    def _game_target_name(self):
        if self.settings.data["freeze"]["auto_game"]:
            return ""
        name = self.settings.data["freeze"]["game_exe"].strip()
        if not name:
            raise RuntimeError("Укажи .exe вручну або увімкни автоматичне визначення гри")
        return name


    def _display_game_detection(self):
        info = platform._last_game_detection
        name = info.get("name", "")
        title = info.get("title", "") or name
        reason = info.get("reason", "")
        self.game_detection_var.set(f"Визначено: {title[:80]} · {name} · {reason}" if name
                                    else "Активну гру визначено")


    def schedule_game_detection(self):
        timer = getattr(self, "_game_detect_timer", None)
        if timer is not None:
            self.root.after_cancel(timer)
        self.game_detection_var.set("Перемкнися у вікно гри — перевірка через 3 секунди. Пауза не вмикається.")
        self._game_detect_timer = self.root.after(3000, self._check_active_game)


    def _check_active_game(self):
        self._game_detect_timer = None
        try:
            platform.foreground_game(self._game_target_name())
            self._display_game_detection()
            return True
        except Exception as e:
            self.game_detection_var.set(str(e))
            return False


    def _pause_option_changed(self):
        if not self.settings.data["freeze"]["pause_game"]:
            self._resume_game()


    def _prepare_game_profile(self):
        """Capture the foreground game once, before an overlay takes focus."""
        self._lookup_profile = {}
        if not self.settings.data.get("game_profiles"):
            return
        try:
            active = platform.foreground_process_info()
            profile = self.settings.data["game_profiles"].get(active.get("name", "").casefold())
            if not profile:
                return
            if self._owns_foreground_pause(active):
                # The normal detector rejects stopped processes. Only this exact
                # PID/create-time/window owned by our live guard may bypass it.
                info = active
            else:
                platform.foreground_game(active["name"])
                info = dict(platform._last_game_detection)
            if profile:
                self._lookup_profile = {**copy.deepcopy(profile), "source": info.get("title", ""),
                                        "exe": info["name"]}
        except Exception as error:
            self.logger.write(f"Профіль гри не застосовано: {error}")


    def _owns_foreground_pause(self, info):
        guard = self._game_pause
        return bool(guard is not None and guard.active
                    and getattr(guard, "pid", None) is not None
                    and info.get("pid") == guard.pid
                    and info.get("created") == guard.created
                    and info.get("hwnd") == self._game_pause_hwnd
                    and platform._foreground_window() == self._game_pause_hwnd)


    def _begin_game_pause(self):
        if not self._lookup_profile.get("pause_game", self.settings.data["freeze"]["pause_game"]):
            return self._resume_game()
        if self._game_pause is not None:
            if self._game_pause.active:
                try:
                    if self._owns_foreground_pause(platform.foreground_process_info()):
                        return True  # reuse the original deadline, never restart it
                except Exception:
                    self.logger.exception("Перевірка власника активної паузи")
                resumed = self._resume_game()
                self.toast("Активне вікно змінилося — паузу попередньої гри завершено. Повтори пошук у грі."
                           if resumed else "Не підтверджено відновлення попередньої гри — перевір журнал",
                           timeout=8)
                return False
            if not self._resume_game():
                self.toast("Попереднє відновлення гри не підтверджено — перевір журнал", timeout=8)
                return False
        guard = None
        try:
            pid, created, hwnd = platform.foreground_game(self._lookup_profile.get("exe") or self._game_target_name())
            self._display_game_detection()
            limit = clamp(self.settings.data["freeze"]["pause_seconds"], 5, 300)
            guard = GamePauseGuard(pid, created, limit)
            self._game_pause, self._game_pause_hwnd = guard, hwnd
            self._game_pause_mode = self._lookup_profile.get(
                "pause_mode", self.settings.data["freeze"].get("pause_mode", "snapshot"))
            guard.start()
            self.diagnostics.record("game.pause", pid=pid, active=guard.active, mode=self._game_pause_mode)
            if not guard.active:
                raise RuntimeError("Сторож не підтвердив активну паузу")
            self.logger.write(f"Гру призупинено: PID {pid}; режим {self._game_pause_mode}; ліміт {limit:.0f} с")
            return True
        except Exception as e:
            self._resume_game()
            self.toast(f"Не вдалося підтвердити паузу гри: {e}", timeout=8)
            return False


    def _after_capture_pause(self):
        """The original guard owns the whole hold; results never start a new one."""
        if self._game_pause_mode != "lookup":
            return self._resume_game()
        return True


    def _resume_game(self):
        guard = self._game_pause
        if guard is None:
            return True
        try:
            ok, status = guard.release()
            self.diagnostics.record("game.resume", ok=ok, active=guard.active)
        except Exception:
            self.logger.exception("Відновлення гри")
            return False
        if not guard.active:
            self._game_pause = None
            if ok:
                platform._restore_foreground(self._game_pause_hwnd)
            self._game_pause_hwnd = None
            self._game_pause_mode = "snapshot"
        if not ok:
            self.logger.write(f"Відновлення гри: {status}")
        return ok


    def start_region_select(self):
        if self.selector or self.free_cursor or self.paused:
            return
        self._prepare_game_profile()
        if not self._begin_game_pause():
            return
        self._new_token()  # results from a lookup predating this frame are obsolete
        self.close_all()
        previous_windows = set(self.root.winfo_children())
        try:
            self.selector = RegionSelector(self, self._region_done)
        except Exception as e:
            for child in set(self.root.winfo_children()) - previous_windows:
                try:
                    child.destroy()
                except tk.TclError:
                    self.logger.exception('word_lookup.start_region_select')
            self._resume_game()
            self.toast(f"Не вдалося відкрити вибір слова: {e}")


    def _region_done(self, result, frozen=None):
        profile = getattr(self.selector, "profile", self._lookup_profile)
        self.selector = None
        if result is None:
            self._resume_game()
        else:
            self._after_capture_pause()
        if result is not None:
            token = self._new_token()
            self._lookup_profiles[token] = copy.deepcopy(profile)
            threading.Thread(target=self._region_thread, args=(result, frozen, token), daemon=True).start()


    def start_free_cursor(self):
        if self.selector or self.free_cursor or self.paused:
            return
        self._prepare_game_profile()
        if not self._begin_game_pause():
            return
        self._new_token()
        self.close_all()
        start = self.free_cursor_pos or (self.root.winfo_screenwidth() // 2,
                                         self.root.winfo_screenheight() // 2)
        previous_windows = set(self.root.winfo_children())
        try:
            self.free_cursor = FreeCursorOverlay(self, start, self._free_cursor_done)
        except Exception as e:
            for child in set(self.root.winfo_children()) - previous_windows:
                try:
                    child.destroy()
                except tk.TclError:
                    self.logger.exception('word_lookup.start_free_cursor')
            self._resume_game()
            self.toast(f"Не вдалося відкрити керований курсор: {e}")
            return
        self._fc_mouse_pos = None
        self._fc_delta = [0, 0]
        try:
            self._fc_mouse_listener = mouse.Listener(on_move=self._on_fc_mouse_move)
            self._fc_mouse_listener.daemon = True
            self._fc_mouse_listener.start()
            self.manager.start_raw_mode(lambda name: self.events.put(as_event(("fc_key", name))))
        except Exception as e:
            self.free_cursor.finish(None)
            self.toast(f"Не вдалося підключити керований курсор: {e}")


    def _on_fc_mouse_move(self, x, y):
        """Виконується у власному потоці pynput — лише накопичуємо зміщення від
        попереднього положення. Низькорівневий хук зазвичай бачить фізичний рух
        миші, навіть коли гра «захопила» курсор для керування камерою."""
        if self._fc_mouse_pos is not None:
            dx, dy = x - self._fc_mouse_pos[0], y - self._fc_mouse_pos[1]
            with self._fc_lock:
                self._fc_delta[0] += dx
                self._fc_delta[1] += dy
        self._fc_mouse_pos = (x, y)


    def _stop_free_cursor_input(self):
        if self._fc_mouse_listener is not None:
            stop_input_listener(self._fc_mouse_listener)
            self._fc_mouse_listener = None
        self.manager.stop_raw_mode()


    def _free_cursor_done(self, pos, frozen=None):
        profile = getattr(self.free_cursor, "profile", self._lookup_profile)
        self._stop_free_cursor_input()
        self.free_cursor = None
        if pos is None:
            self._resume_game()
        else:
            self._after_capture_pause()
        if pos is not None:
            self.free_cursor_pos = pos
            # невелика затримка, щоб екран встиг перемалюватись після зникнення накладки;
            # root.after — щоб не блокувати головний потік Tk (на відміну від _region_thread,
            # який уже виконується у фоновому потоці)
            token = self._new_token()
            self._lookup_profiles[token] = copy.deepcopy(profile)
            self.root.after(0, lambda: threading.Thread(
                target=self._lookup_thread, args=("word", pos[0], pos[1], token, frozen),
                daemon=True).start() if token == self.active_token else None)

