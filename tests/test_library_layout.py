"""Regression coverage for the dark library controls and narrow list pane."""
from types import SimpleNamespace

import pytest

import word_lookup as w


def test_dark_filter_buttons_keep_radio_and_checkbox_behavior(app, card):
    w.add_saved_word(card)
    w.add_saved_word(dict(card, word="apple", source="Garden"))
    w.update_saved_entry("apple", {"status": "known"})
    app.open_saved_words()
    app.root.update()

    for button in [*app.saved_filter_buttons.values(), app.saved_due_button]:
        assert not bool(int(button.cget("indicatoron")))
        assert button.cget("background") == "#1d2029"
        assert button.cget("selectcolor") == "#303644"
        assert bool(int(button.cget("takefocus")))
    assert app.saved_filter_buttons["Знаю"].winfo_class() == "Radiobutton"
    assert app.saved_due_button.winfo_class() == "Checkbutton"
    app.saved_filter_buttons["Знаю"].invoke()
    assert app.saved_state_filter.get() == "Знаю"
    assert [entry["word"] for entry in app._visible_saved_words()] == ["apple"]
    assert app.saved_filter_buttons["Знаю"].cget("foreground") == "#ffd43b"
    assert app.saved_filter_buttons["Усі"].cget("foreground") == "#a2a8b8"
    app.saved_due_button.invoke()
    assert app.saved_due_only.get()
    assert not app._visible_saved_words()  # Known words are excluded from due reviews.
    app.saved_due_button.invoke()
    assert not app.saved_due_only.get()
    assert [entry["word"] for entry in app._visible_saved_words()] == ["apple"]


def test_library_scrollbars_have_explicit_dark_styles(app, card):
    w.add_saved_word(card)
    app.open_saved_words()
    app.root.update()
    style = w.ttk.Style(app.root)
    for bar, name in ((app.saved_ybar, "Library.Vertical.TScrollbar"),
                      (app.saved_xbar, "Library.Horizontal.TScrollbar")):
        assert bar.cget("style") == name
        assert style.lookup(name, "background") == "#272b36"
        assert style.lookup(name, "troughcolor") == "#1d2029"
        assert style.lookup(name, "arrowcolor") == "#a2a8b8"
        assert style.lookup(name, "background", ("active",)) == "#3b4354"
        assert bar.winfo_ismapped()
    assert app.saved_tree.cget("style") == "Library.Treeview"


def test_game_name_fits_normal_pane_and_stays_available_when_narrow(app, card):
    w.add_saved_word(card)
    app.open_saved_words()
    app.root.update()
    tree, iid = app.saved_tree, app._saved_tree_id(card["word"])
    assert tree.heading("check", "text") == "Вибір"
    source_width = int(tree.column("source", "width"))
    font = w.tkfont.Font(root=app.root, font=w.ttk.Style(app.root).lookup("Library.Treeview", "font"))
    assert source_width >= font.measure("Example game") + 16
    assert tree.xview() == (0.0, 1.0)

    long_name = "Indiana Jones and the Great Circle — Long custom game profile"
    w.update_saved_entry(card["word"], {"source": long_name})
    app.refresh_saved_count()
    app.show_saved_details(card["word"])
    app.library_panes.sash_place(0, 350, 0)
    app.root.update()
    assert tree.xview()[1] < 1.0
    assert int(tree.column("source", "width")) >= 160
    tree.xview_moveto(1.0)
    app.root.update()
    x, y, width, height = tree.bbox(iid, "source")
    assert x >= 0 and x + width <= tree.winfo_width()
    assert long_name in app.saved_source_preview.get()
    assert tree.item(iid, "values")[-1] == long_name


def test_check_column_mouse_and_space_toggle_same_word_after_horizontal_scroll(app, card):
    w.add_saved_word(card)
    app.open_saved_words()
    app.root.update()
    tree, iid = app.saved_tree, app._saved_tree_id(card["word"])
    app.library_panes.sash_place(0, 350, 0)
    tree.xview_moveto(1.0)
    app.root.update()
    tree.xview_moveto(0.0)
    app.root.update()
    x, y, width, height = tree.bbox(iid, "check")
    assert app._saved_tree_click(SimpleNamespace(x=x + width // 2, y=y + height // 2)) == "break"
    assert app._saved_check_var(card["word"]).get()
    tree.selection_set(iid)
    tree.focus_set()
    app.saved_win.focus_force()
    tree.focus_force()
    app.root.update()
    tree.event_generate("<space>")
    app.root.update()
    assert not app._saved_check_var(card["word"]).get()
    assert "Обрано: 0" in app.saved_selected_var.get()


def test_library_list_keeps_vertical_scroll_and_selected_word(app, monkeypatch):
    entries = [dict(word=f"word{number}", translation="переклад", source="Example game")
               for number in range(25)]
    monkeypatch.setattr(w, "_saved_words", entries)
    app.open_saved_words()
    app.root.update()
    tree = app.saved_tree
    assert tree.yview()[1] < 1.0
    tree.yview_moveto(1.0)
    app.root.update()
    assert tree.yview()[0] > 0.0
    last = tree.get_children()[-1]
    assert tree.bbox(last)
    tree.selection_set(last)
    app._toggle_selected_saved_word()
    assert app._saved_check_var(app._saved_iids[last]).get()


@pytest.mark.parametrize("scaling", [5 / 3, 2.0])
def test_library_headers_and_filters_fit_scaled_fonts(app, scaling):
    # Tk's scaling is pixels per point: 96dpi at 150% uses 2 pixels per point.
    previous = app.root.tk.call("tk", "scaling")
    try:
        app.root.tk.call("tk", "scaling", scaling)
        app.open_saved_words()
        # Finish the initial Map/after_idle placement before simulating a user
        # resize and sash drag; otherwise initialization can replace the drag.
        app.root.update()
        app.saved_win.geometry("980x600")
        app.root.update()
        app.library_panes.sash_place(0, 300, 0)
        app.root.update()
        header = w.tkfont.Font(root=app.root, font=(w.FONT, 9))
        for column in ("check", "word", "translation", "source"):
            assert int(app.saved_tree.column(column, "minwidth")) >= header.measure(
                app.saved_tree.heading(column, "text")) + 16
        assert app._library_filter_columns in (1, 2, 4)
        for button in app.saved_filter_buttons.values():
            assert button.winfo_x() + button.winfo_width() <= button.master.winfo_width()
            assert button.winfo_ismapped()
        assert app.saved_tree.xview()[1] < 1.0
    finally:
        app.root.tk.call("tk", "scaling", previous)


@pytest.mark.parametrize("scaling", [4 / 3, 5 / 3, 2.0])
@pytest.mark.parametrize("geometry", ["980x540", "1280x820"])
def test_library_panes_cannot_hide_word_list_or_editor(app, scaling, geometry):
    previous = app.root.tk.call("tk", "scaling")
    try:
        app.root.tk.call("tk", "scaling", scaling)
        w.atomic_json_write(w._saved_words_path(), [
            {"word": f"sample{index}", "translation": "переклад"} for index in range(35)])
        app.open_saved_words()
        app.saved_win.geometry(geometry)
        app.root.update()
        for position in (0, 100000):
            app.library_panes.sash_place(0, position, 0)
            app.root.update()
            assert app.library_sidebar.winfo_width() >= app._library_sidebar_min
            assert app.library_editor.winfo_width() >= app._library_editor_min
            assert app.saved_tree.winfo_width() >= 150
            assert app.saved_tree.winfo_height() >= 80
        tree = app.saved_tree
        last = tree.get_children()[-1]
        tree.see(last)
        tree.selection_set(last)
        app.root.update()
        assert app._editor_word == app._saved_iids[last]
        app.saved_win.withdraw()
        app.open_saved_words()
        app.root.update()
        assert app.library_sidebar.winfo_viewable()
        assert len(tree.get_children()) == 35
    finally:
        app.root.tk.call("tk", "scaling", previous)
