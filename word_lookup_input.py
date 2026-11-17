"""Hotkey parsing and owned operating-system input listeners."""
import logging
import sys
import time
import threading
from pynput import keyboard
log = logging.getLogger(__name__).info
log_exc = logging.getLogger(__name__).exception
MOD_ORDER = ["ctrl", "alt", "alt_gr", "shift", "cmd"]
MODS = set(MOD_ORDER)
MOD_KEYS = {"ctrl": keyboard.Key.ctrl, "alt": keyboard.Key.alt,
            "alt_gr": keyboard.Key.alt_gr, "shift": keyboard.Key.shift,
            "cmd": keyboard.Key.cmd}
COPY_MOD = keyboard.Key.cmd if sys.platform == "darwin" else keyboard.Key.ctrl
PRETTY = {"ctrl": "Ctrl", "alt": "Alt", "alt_gr": "AltGr", "shift": "Shift",
          "cmd": "Cmd" if sys.platform == "darwin" else "Win", "plus": "+"}
RESERVED = {"ctrl+c", "ctrl+v", "ctrl+x", "ctrl+a", "ctrl+z", "cmd+c", "cmd+v", "cmd+x"}


def pretty(combo: str) -> str:
    if not combo:
        return "— не задано —"
    out = []
    for p in combo.split("+"):
        if p in PRETTY:
            out.append(PRETTY[p])
        elif len(p) == 1 or (p[0] == "f" and p[1:].isdigit()):
            out.append(p.upper())
        else:
            out.append(p.replace("_", " ").capitalize())
    return " + ".join(out)

def combo_problem(combo: str):
    parts = combo.split("+")
    key, mods = parts[-1], parts[:-1]
    if not mods and (len(key) == 1 or key.startswith("vk")):
        return "Для літер і цифр потрібна хоча б одна клавіша-модифікатор (Ctrl, Alt, Shift)."
    if combo in RESERVED:
        return "Ця комбінація зарезервована системою (копіювання/вставлення)."
    return None

def norm_key(key):
    """Перетворює подію pynput на ім'я клавіші (незалежно від розкладки на Windows)."""
    if isinstance(key, keyboard.Key):
        n = key.name
        if n == "alt_gr":
            return n
        for base in ("ctrl", "alt", "shift", "cmd"):
            if n == base or n.startswith(base + "_"):
                return base
        return n
    vk = getattr(key, "vk", None)
    if sys.platform.startswith("win") and vk is not None:
        if 0x41 <= vk <= 0x5A:
            return chr(vk).lower()
        if 0x30 <= vk <= 0x39:
            return chr(vk)
    ch = getattr(key, "char", None)
    if ch:
        if ord(ch) < 32:
            ch = chr(ord(ch) + 96)
        return "plus" if ch == "+" else ch.lower()
    return f"vk{vk}" if vk is not None else None

def stop_input_listener(listener, timeout=1.0):
    """Stop an owned pynput listener and release its OS resources within a bound.

    pynput 1.8.2's XRecord backend sends disable on its recording connection;
    that connection is blocked by RecordEnableContext. Send the same request on
    its existing control connection and flush it so the backend's own finally
    block closes both displays. Never close a running listener's displays here.
    """
    if listener is None or not listener.is_alive():
        return True
    deadline = time.monotonic() + timeout
    # pynput.wait has no timeout and can hang if backend initialization failed.
    while listener.is_alive() and not getattr(listener, "_ready", False):
        if time.monotonic() >= deadline:
            log("Системний слухач не завершив ініціалізацію вчасно")
            return False
        time.sleep(0.005)
    if not listener.is_alive():
        return True
    try:
        listener.stop()
        if type(listener).__module__.endswith("._xorg") and listener.is_alive():
            control = getattr(listener, "_display_stop", None)
            context = getattr(listener, "_context", None)
            if control is not None and context is not None:
                try:
                    control.record_disable_context(context)
                    control.flush()
                except Exception:
                    # The backend may finish and close this control connection
                    # before the listener thread itself finishes. Give its
                    # teardown the remaining deadline before judging the race.
                    if listener is not threading.current_thread():
                        listener.join(timeout=max(0.0, deadline - time.monotonic()))
                    if listener.is_alive():
                        raise
                    return True
        if listener is not threading.current_thread():
            listener.join(timeout=max(0.0, deadline - time.monotonic()))
    except Exception:
        log_exc("Завершення системного слухача")
        return False
    stopped = not listener.is_alive()
    if not stopped:
        log("Системний слухач не завершився вчасно")
    return stopped

class HotkeyManager:
    """Один глобальний слухач клавіатури: розпізнає комбінації й записує нові."""
    STALE_AFTER = 30.0

    def __init__(self, on_action):
        self.on_action = on_action
        self.bindings = {}
        self.down = {}
        self.recorder = None
        self.ignore_until = 0.0
        self.on_esc = None
        self.raw_mode_cb = None  # поки задано — усі натискання йдуть напряму сюди
        self.listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self.listener.daemon = True

    def start(self):
        self.listener.start()

    def set_bindings(self, hotkeys: dict) -> None:
        self.bindings = {frozenset(c.split("+")): a for a, c in hotkeys.items() if c}

    def start_recording(self, cb) -> None:
        self.down.clear()
        self.recorder = cb

    def start_raw_mode(self, cb) -> None:
        """Усі подальші натискання клавіш ідуть напряму в cb(ім'я_клавіші), а не в
        звичайне зіставлення гарячих клавіш — працює незалежно від того, яке вікно
        зараз у фокусі (той самий глобальний хук, що й звичайні гарячі клавіші),
        тому спрацьовує навіть коли фокус лишається у грі."""
        self.down.clear()
        self.raw_mode_cb = cb

    def stop_raw_mode(self) -> None:
        self.raw_mode_cb = None

    def keys_down(self) -> bool:
        self._prune(time.time())
        return bool(self.down)

    def _prune(self, now):
        for k in [k for k, t in self.down.items() if now - t > self.STALE_AFTER]:
            del self.down[k]

    def _on_press(self, key):
        n = norm_key(key)
        if n is None:
            return
        now = time.time()
        first = n not in self.down
        self.down[n] = now
        self._prune(now)
        rec = self.recorder
        if rec is not None:
            self._record(rec, n, first)
            return
        if self.raw_mode_cb is not None:
            if first:
                self.raw_mode_cb(n)
            return
        if n == "esc" and first and self.on_esc:
            self.on_esc()
        if not first or now < self.ignore_until:
            return
        action = self.bindings.get(frozenset(self.down))
        if action:
            self.on_action(action)

    def _on_release(self, key):
        n = norm_key(key)
        if n is not None:
            self.down.pop(n, None)

    def _record(self, cb, n, first):
        mods = [m for m in MOD_ORDER if m in self.down]
        if n in MODS:
            cb("progress", "+".join(mods))
            return
        if not first:
            return
        self.recorder = None
        if n == "esc" and not mods:
            cb("cancel", "")
        else:
            cb("done", "+".join(mods + [n]))
