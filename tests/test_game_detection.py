import os

import psutil
import pytest
import word_lookup as w


@pytest.mark.parametrize("name,path,reason", [
    ("Mystery.exe", r"D:\SteamLibrary\steamapps\common\Mystery\Mystery.exe", "Steam"),
    ("Game.exe", r"C:\Program Files\Epic Games\Title\Game.exe", "Epic Games"),
    ("Game.exe", r"D:\GOG Games\Title\Game.exe", "GOG"),
    ("Game.exe", r"D:\GOG Galaxy\Games\Title\Game.exe", "GOG"),
    ("Game.exe", r"D:\XboxGames\Title\Content\Game.exe", "Xbox"),
    ("Game.exe", r"D:\EA Games\Title\Game.exe", "EA"),
    ("Game.exe", r"D:\Ubisoft Game Launcher\games\Title\Game.exe", "Ubisoft"),
    ("Title-Win64-Shipping.exe", r"D:\Custom\Title-Win64-Shipping.exe", "Unreal Engine Shipping"),
    ("Cyberpunk2077.exe", r"D:\Games\Cyberpunk\bin\x64\Cyberpunk2077.exe", "відома гра"),
])
def test_installed_game_detection(name, path, reason):
    assert w.game_detection_reason(name, path) == reason


@pytest.mark.parametrize("name,path", [
    ("chrome.exe", r"D:\Steam\steamapps\common\Browser\chrome.exe"),
    ("explorer.exe", r"C:\Windows\explorer.exe"),
    ("Code.exe", r"D:\Steam\steamapps\common\Editor\Code.exe"),
    ("notepad.exe", r"C:\Windows\notepad.exe"),
    ("steamwebhelper.exe", r"D:\Steam\steamapps\common\helper\steamwebhelper.exe"),
    ("CrashReportClient.exe", r"D:\Steam\steamapps\common\Title\CrashReportClient.exe"),
    ("UnityCrashHandler64.exe", r"D:\Steam\steamapps\common\Title\UnityCrashHandler64.exe"),
    ("UnrealEditor.exe", r"D:\Epic Games\UE\UnrealEditor.exe"),
    ("Launcher.exe", r"D:\Rockstar Games\Launcher\Launcher.exe"),
    ("HogwartsLegacy.exe", r"C:\Windows\HogwartsLegacy.exe"),
    ("Random.exe", r"D:\Unknown\Random.exe"),
    ("Cyberpunk2077.exe", "Cyberpunk2077.exe"),
    ("Title.exe", r"D:\Steam\steamapps\common\Title\Different.exe"),
])
def test_desktop_apps_and_unknown_processes_are_not_automatically_paused(name, path):
    assert w.game_detection_reason(name, path) == ""


def test_windows_game_mode_supports_custom_install_location():
    path = r"E:\MyFolder\IndieGame.exe"
    registered = {w.windows_exe_path(path)}
    assert w.game_detection_reason("IndieGame.exe", path, registered) == "Windows Game Mode"
    browser = r"D:\Browser\chrome.exe"
    assert w.game_detection_reason("chrome.exe", browser, {w.windows_exe_path(browser)}) == ""


@pytest.fixture
def active_window(monkeypatch):
    info = {"pid": 987654, "created": 1234.5, "hwnd": 77, "name": "Cyberpunk2077.exe",
            "exe": r"D:\Games\Cyberpunk\bin\x64\Cyberpunk2077.exe", "status": psutil.STATUS_RUNNING,
            "title": "Cyberpunk 2077"}
    monkeypatch.setattr(w, "foreground_process_info", lambda: dict(info))
    monkeypatch.setattr(w, "_foreground_window", lambda: info["hwnd"])
    monkeypatch.setattr(w, "registered_game_paths", lambda: set())
    return info


def test_auto_targets_the_foreground_pid_and_explains_detection(active_window):
    assert w.foreground_game() == (987654, 1234.5, 77)
    assert w._last_game_detection["name"] == "Cyberpunk2077.exe"
    assert w._last_game_detection["reason"] == "відома гра"


def test_manual_fallback_allows_unknown_game_but_checks_exact_name(active_window):
    active_window.update(name="IndieGame.exe", exe=r"D:\Custom\IndieGame.exe")
    with pytest.raises(RuntimeError, match="розпізнати"):
        w.foreground_game()
    assert w.foreground_game("INDIEGAME.EXE") == (987654, 1234.5, 77)
    with pytest.raises(RuntimeError, match="не належить"):
        w.foreground_game("SomeOtherGame.exe")


def test_foreground_switch_during_detection_prevents_pause(active_window, monkeypatch):
    monkeypatch.setattr(w, "_foreground_window", lambda: 88)
    with pytest.raises(RuntimeError, match="вікно змінилось"):
        w.foreground_game()
    assert w._last_game_detection == {}


def test_self_and_already_stopped_target_rejected(active_window):
    active_window["pid"] = os.getpid()
    with pytest.raises(RuntimeError):
        w.foreground_game()
    active_window.update(pid=987654, status=psutil.STATUS_STOPPED)
    with pytest.raises(RuntimeError, match="уже призупинений"):
        w.foreground_game()


def test_manual_mode_does_not_allow_browser(active_window):
    active_window.update(name="chrome.exe", exe=r"D:\Apps\chrome.exe")
    with pytest.raises(RuntimeError, match="звичайна програма"):
        w.foreground_game("chrome.exe")


def test_ui_auto_mode_disables_exe_field_and_readonly_check_never_pauses(app, active_window, monkeypatch):
    assert app.game_exe_entry.instate(["disabled"])
    def must_not_pause(*args):
        raise AssertionError("The detection check must not suspend a process")
    monkeypatch.setattr(w, "GamePauseGuard", must_not_pause)
    assert app._check_active_game()
    assert "Cyberpunk2077.exe" in app.game_detection_var.get()
    assert app._game_pause is None
    w.cfg["freeze"]["auto_game"] = False
    app._game_mode_changed()
    assert not app.game_exe_entry.instate(["disabled"])
    assert not app._check_active_game()  # empty manual name must not accidentally select auto mode
    w.cfg["freeze"]["game_exe"] = "Cyberpunk2077.exe"
    assert app._check_active_game()


def test_detection_failure_never_starts_guard_or_overlay(app, active_window, monkeypatch):
    w.cfg["freeze"]["pause_game"] = True
    active_window.update(name="Random.exe", exe=r"D:\Unknown\Random.exe")
    called = []
    monkeypatch.setattr(w, "GamePauseGuard", lambda *a: called.append("guard"))
    monkeypatch.setattr(w, "RegionSelector", lambda *a: called.append("overlay"))
    app.start_region_select()
    assert called == []
    assert app._game_pause is None
