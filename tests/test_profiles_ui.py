"""Profile editor acceptance tests, using actual Tk widgets without global hooks."""
import copy
import tkinter as tk

import pytest
import word_lookup as w
import word_lookup_profiles_ui as ui


@pytest.fixture
def profile_editor(monkeypatch):
    monkeypatch.setattr(w, "cfg", copy.deepcopy(w.DEFAULT_CONFIG))
    w.cfg.setdefault("game_profiles", {})

    class Editor(ui.GameProfilesViewMixin):
        def __init__(self):
            self.settings = w._settings
            self.root = tk.Tk()
            self.root.withdraw()
            self.saves = 0
            frame = w.ttk.Frame(self.root)
            frame.pack()
            self._build_profiles_settings(frame)

        def _schedule_config_save(self):
            self.saves += 1

    editor = Editor()
    yield editor
    editor.root.destroy()


def test_create_edit_delete_profile_keeps_general_settings(profile_editor):
    e = profile_editor
    defaults = copy.deepcopy(w.cfg["freeze"])
    e.profile_exe_var.set("Cyberpunk2077.EXE")
    e.profile_mode_var.set("Клавіатура")
    e.profile_width_var.set("900")
    e.profile_height_var.set("200")
    e.profile_tag_var.set("Cyberpunk_2077")
    assert not e.profile_pause_var.get()
    assert e._profile_save()
    expected = {"selection_mode": "keyboard", "region_w": 900, "region_h": 200,
                "pause_game": False, "anki_tag": "Cyberpunk_2077"}
    assert w.cfg["game_profiles"] == {"cyberpunk2077.exe": expected}
    assert e.saves == 1
    assert w.cfg["freeze"] == defaults
    e._profile_selected()
    e.profile_mode_var.set("Наведення мишею")
    e.profile_width_var.set("500")
    assert e._profile_save()
    assert w.cfg["game_profiles"]["cyberpunk2077.exe"]["selection_mode"] == "hover"
    assert w.cfg["game_profiles"]["cyberpunk2077.exe"]["region_w"] == 500
    assert e._profile_delete()
    assert w.cfg["game_profiles"] == {}
    assert e.saves == 3


@pytest.mark.parametrize("name,width,height", [
    (r"C:\\Games\\Game.exe", "480", "100"),
    ("game", "480", "100"),
    ("game.exe", "abc", "100"),
    ("game.exe", "199", "100"),
    ("game.exe", "480", "601"),
])
def test_bad_profile_never_schedules_persistence(profile_editor, name, width, height):
    e = profile_editor
    e.profile_exe_var.set(name)
    e.profile_width_var.set(width)
    e.profile_height_var.set(height)
    assert not e._profile_save()
    assert e.saves == 0
    assert w.cfg["game_profiles"] == {}
    assert "Не збережено" in e.profile_status_var.get()


def test_new_profile_always_starts_without_pause(profile_editor):
    e = profile_editor
    w.cfg["freeze"]["pause_game"] = True
    e.profile_pause_var.set(True)
    e._profile_new()
    assert not e.profile_pause_var.get()
    assert not e._profile_delete()
    assert e.saves == 0
