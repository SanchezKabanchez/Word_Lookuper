"""Foreground windows and game process identification; no UI/global app dependency."""
import logging
import os
import ntpath
import re
import subprocess
import sys
import time
from word_lookup_profiles import current_profile
from word_lookup_theme import APP_NAME
log = logging.getLogger(__name__).info
log_exc = logging.getLogger(__name__).exception
_IGNORED_TITLES = {"", "program manager"}
_NON_GAME_EXES = frozenset({
    "python.exe", "pythonw.exe", "python3.exe", "explorer.exe", "dwm.exe", "steam.exe",
    "steamwebhelper.exe", "epicgameslauncher.exe", "epicwebhelper.exe", "galaxyclient.exe",
    "ubisoftconnect.exe", "upc.exe", "eadesktop.exe", "ealauncher.exe", "origin.exe",
    "battle.net.exe", "riotclientservices.exe", "rockstargameslauncher.exe",
    "svchost.exe", "csrss.exe", "winlogon.exe", "services.exe", "lsass.exe", "sihost.exe",
    "runtimebroker.exe", "applicationframehost.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe", "textinputhost.exe",
    "systemsettings.exe", "taskmgr.exe", "taskhostw.exe", "securityhealthsystray.exe",
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "iexplore.exe",
    "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "conhost.exe", "openconsole.exe",
    "code.exe", "cursor.exe", "devenv.exe", "idea64.exe", "rider64.exe", "notepad.exe", "notepad++.exe",
    "discord.exe", "obs64.exe", "obs32.exe", "vlc.exe", "mpv.exe", "wmplayer.exe", "spotify.exe",
    "blender.exe", "aseprite.exe", "unity.exe", "unrealeditor.exe", "ue4editor.exe",
    "wallpaper32.exe", "wallpaper64.exe", "calculatorapp.exe",
})
_KNOWN_GAME_EXES = frozenset({
    "cyberpunk2077.exe", "hogwartslegacy.exe", "rdr2.exe", "eldenring.exe", "witcher3.exe",
    "bg3.exe", "bg3_dx11.exe", "skyrimse.exe", "skyrim.exe", "fallout4.exe", "starfield.exe",
    "gta5.exe", "gta5_enhanced.exe", "kingdomcome.exe", "kingdomcome2.exe", "terraria.exe",
    "stardew valley.exe", "hollow_knight.exe", "hades.exe", "hades2.exe", "factorio.exe",
})
_game_registry_cache = {"until": 0.0, "paths": set()}
_last_game_detection = {}


def _foreground_window():
    """Яке вікно зараз активне (тільки Windows) — щоб повернути йому фокус після
    закриття нашої накладки. Без цього гра іноді лишається без фокусу, курсор миші
    виглядає «залоченим», і допомагає лише випадковий клік по Пуску/панелі задач,
    що змушує Windows переоцінити активне вікно."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes
        fn = ctypes.windll.user32.GetForegroundWindow
        fn.restype = wintypes.HWND
        return fn()
    except Exception:
        return None

def _restore_foreground(hwnd) -> bool:
    if not hwnd or not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        from ctypes import wintypes
        fn = ctypes.windll.user32.SetForegroundWindow
        fn.argtypes = [wintypes.HWND]
        fn.restype = wintypes.BOOL
        restored = bool(fn(hwnd))
        if not restored and _foreground_window() == hwnd:
            restored = True
        if not restored:
            log("Windows не повернула фокус грі; клацни її вікно або повернись через Alt+Tab.")
        return restored
    except Exception:
        log_exc('word_lookup._restore_foreground')
        return False

def release_cursor_clip() -> bool:
    """Знімає обмеження руху курсора (ClipCursor), яким гра тримає мишу в центрі екрана —
    це те саме, що відбувається, коли натиснути «Пуск». Лише Windows; безпечно викликати
    будь-коли й скільки завгодно разів (нічого не ламає, лише відпускає курсор)."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.ClipCursor(None))
    except Exception:
        return False

def _foreground_title() -> str:
    """Заголовок активного вікна (тільки Windows) — з нього беремо назву гри для тегу
    в Anki. На інших системах повертає порожній рядок."""
    if not sys.platform.startswith("win"):
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        # HWND is pointer-sized. ctypes' implicit int conversion can truncate
        # 64-bit window handles before reading the title used as card source.
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        hwnd = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception:
        return ""

def _game_source() -> str:
    profile = current_profile()
    if profile.get("source"):
        return profile["source"]
    t = " ".join(_foreground_title().split())
    return "" if (t.lower() in _IGNORED_TITLES or t == APP_NAME) else t[:80]

def windows_exe_path(path):
    if not isinstance(path, str) or not path.strip():
        return ""
    value = path.strip().replace("/", "\\")
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normpath(value).replace("\\", "/").casefold()

def non_game_process(name, exe_path=""):
    name = name.strip().casefold()
    if (name in _NON_GAME_EXES or re.fullmatch(r"python(?:w|\d+(?:\.\d+)*w?)?\.exe", name)
            or any(part in name for part in ("launcher", "crashreport", "crashhandler", "anticheat"))):
        return True
    path = windows_exe_path(exe_path)
    system = windows_exe_path(os.environ.get("SystemRoot", r"C:\Windows"))
    return bool(path and system and (path == system or path.startswith(system + "/")))

def registered_game_paths():
    """Локальні позначки Windows Game Mode; не скануємо весь список процесів."""
    if not sys.platform.startswith("win"):
        return set()
    now = time.monotonic()
    if now < _game_registry_cache["until"]:
        return _game_registry_cache["paths"]
    paths = set()
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"System\GameConfigStore\Children") as parent:
            for i in range(winreg.QueryInfoKey(parent)[0]):
                try:
                    with winreg.OpenKey(parent, winreg.EnumKey(parent, i)) as child:
                        value = winreg.QueryValueEx(child, "MatchedExeFullPath")[0]
                        if isinstance(value, str) and value:
                            paths.add(windows_exe_path(value))
                except OSError:
                    continue
    except OSError:
        log_exc('word_lookup.registered_game_paths')
    _game_registry_cache.update(until=now + 60, paths=paths)
    return paths

def game_detection_reason(name, exe_path, registered=()):
    """Консервативні ознаки гри; звичайне повноекранне вікно не є такою ознакою."""
    name, path = name.strip().casefold(), windows_exe_path(exe_path)
    if (non_game_process(name, exe_path) or not path or not ntpath.isabs(path)
            or ntpath.basename(path) != name):
        return ""
    if path in registered:
        return "Windows Game Mode"
    if re.search(r"-(?:win64|win32)-shipping\.exe$", name):
        return "Unreal Engine Shipping"
    if name in _KNOWN_GAME_EXES:
        return "відома гра"
    parts = path.split("/")
    for marker, label in ((["steamapps", "common"], "Steam"), (["epic games"], "Epic Games"),
                          (["gog galaxy", "games"], "GOG"), (["gog games"], "GOG"),
                          (["xboxgames"], "Xbox"), (["ea games"], "EA"),
                          (["ubisoft game launcher", "games"], "Ubisoft"),
                          (["rockstar games"], "Rockstar")):
        for i in range(len(parts) - len(marker) - 1):
            if parts[i:i + len(marker)] == marker:
                folder = parts[i + len(marker)]
                if folder not in {"launcher", "launchers", "redist", "redistributables", "support", "tools"}:
                    return label
    return ""

def foreground_process_info():
    """Один знімок активного вікна та процесу, без зміни його стану."""
    if not sys.platform.startswith("win"):
        raise RuntimeError("Пауза процесу гри доступна лише у Windows")
    import ctypes
    from ctypes import wintypes
    import psutil
    hwnd = _foreground_window()
    pid = wintypes.DWORD()
    user = ctypes.windll.user32
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value or pid.value in (os.getpid(), os.getppid()):
        raise RuntimeError("Повернись у гру та натисни Ctrl+Alt+R або Ctrl+Alt+G")
    target = psutil.Process(pid.value)
    with target.oneshot():
        info = {"pid": target.pid, "created": target.create_time(), "name": target.name(),
                "status": target.status(), "hwnd": hwnd, "title": _foreground_title()}
        try:
            info["exe"] = target.exe()
        except psutil.AccessDenied:
            info["exe"] = ""
    if not target.is_running():
        raise RuntimeError("Процес завершився під час визначення гри")
    return info

def foreground_game(expected_exe=""):
    """Порожня назва — автовизначення; непорожня — точний ручний вибір .exe."""
    global _last_game_detection
    _last_game_detection = {}
    info = foreground_process_info()
    if info["pid"] in (0, os.getpid(), os.getppid()) or non_game_process(info["name"], info["exe"]):
        raise RuntimeError("Це службова або звичайна програма; повернись у вікно гри")
    name = expected_exe.strip().casefold()
    if name:
        if (not name.endswith(".exe") or len(name) <= 4 or ntpath.basename(name) != name
                or ntpath.splitdrive(name)[0] or non_game_process(name)):
            raise RuntimeError("Укажи точну назву процесу гри, наприклад HogwartsLegacy.exe")
        if info["name"].casefold() != name:
            raise RuntimeError("Активне вікно не належить грі, указаній у налаштуваннях")
        reason = "ручний вибір"
    else:
        reason = game_detection_reason(info["name"], info["exe"], registered_game_paths())
        if not reason:
            raise RuntimeError("Не вдалося впевнено розпізнати гру в активному вікні. "
                               "Вимкни автовизначення й укажи її .exe вручну.")
    import psutil
    if info["status"] == psutil.STATUS_STOPPED:
        raise RuntimeError("Цей процес уже призупинений іншим інструментом")
    if _foreground_window() != info["hwnd"]:
        raise RuntimeError("Активне вікно змінилось. Повернись у гру й повтори гарячу клавішу.")
    _last_game_detection = {**info, "reason": reason}
    return info["pid"], info["created"], info["hwnd"]

def open_file(path: str) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        log_exc('word_lookup.open_file')
