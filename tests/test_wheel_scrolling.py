"""Real wheel events must reach the visible pane before widget class bindings."""
import pytest

import word_lookup as w


def pointer_point(widget):
    return (widget.winfo_rootx() + max(1, widget.winfo_width() // 2),
            widget.winfo_rooty() + max(1, widget.winfo_height() // 2))


def emit_wheel(widget, *, delta=-120, sequence="<MouseWheel>", point=None):
    rootx, rooty = point or pointer_point(widget)
    options = dict(x=rootx - widget.winfo_rootx(), y=rooty - widget.winfo_rooty(),
                   rootx=rootx, rooty=rooty, state=0)
    if sequence == "<MouseWheel>":
        options["delta"] = delta
    widget.event_generate(sequence, **options)
    widget.update()


def top_control(app, panel, widget_type, **options):
    widget = widget_type(panel.content, **options)
    widget.pack(before=panel.content.winfo_children()[0], anchor="w")
    w.tk.Frame(panel.content, height=1000).pack()
    app.notebook.select(panel)
    app.root.lift()
    app.root.update()
    panel.canvas.yview_moveto(0)
    app.root.update()
    return widget


@pytest.mark.parametrize("index", range(4))
def test_wheel_over_buttons_scrolls_each_active_tab_only(app, index):
    panels = [app.root.nametowidget(path) for path in app.notebook.tabs()]
    panel = panels[index]
    button = top_control(app, panel, w.ttk.Button, text="Wheel target")
    hidden_views = {other: other.canvas.yview()[0] for other in panels if other is not panel}
    before = panel.canvas.yview()[0]
    emit_wheel(button)
    assert panel.canvas.yview()[0] > before
    assert all(other.canvas.yview()[0] == pytest.approx(view) for other, view in hidden_views.items())


@pytest.mark.parametrize("kind", ["combo", "spin"])
def test_wheel_scrolls_page_without_changing_preference(app, kind):
    panel = app.root.nametowidget(app.notebook.tabs()[0])
    variable = w.tk.StringVar(value="second" if kind == "combo" else "5")
    control = top_control(app, panel, w.ttk.Combobox if kind == "combo" else w.ttk.Spinbox,
        **(dict(values=("first", "second", "third"), state="readonly") if kind == "combo" else
           dict(from_=0, to=10)), textvariable=variable)
    expected = variable.get()
    emit_wheel(control)
    assert panel.canvas.yview()[0] > 0
    assert variable.get() == expected
    panel.canvas.yview_moveto(0)
    app.root.update()
    emit_wheel(control, delta=120)
    assert panel.canvas.yview()[0] == 0
    assert variable.get() == expected


def test_high_resolution_wheel_accumulates_without_native_spinbox_changes(app):
    panel = app.root.nametowidget(app.notebook.tabs()[0])
    variable = w.tk.StringVar(value="5")
    control = top_control(app, panel, w.ttk.Spinbox, from_=0, to=10, textvariable=variable)
    point = pointer_point(control)
    for _ in range(4):
        emit_wheel(control, delta=-20, point=point)
    assert panel.canvas.yview()[0] > 0
    assert variable.get() == "5"


def test_text_scrolls_itself_then_bubbles_at_edge_without_double_scroll(app):
    panel = app.root.nametowidget(app.notebook.tabs()[0])
    text = top_control(app, panel, w.tk.Text, height=3, width=30)
    text.insert("1.0", "\n".join(f"synthetic line {index}" for index in range(40)))
    app.root.update()
    emit_wheel(text)
    assert text.yview()[0] > 0
    assert panel.canvas.yview()[0] == 0
    text.yview_moveto(1)
    app.root.update()
    emit_wheel(text)
    assert panel.canvas.yview()[0] > 0


def test_linux_button_wheel_scrolls_both_directions(app):
    if app.root.tk.call("tk", "windowingsystem") != "x11":
        pytest.skip("X11 wheel uses Button-4/5; Windows and macOS use MouseWheel")
    panel = app.root.nametowidget(app.notebook.tabs()[0])
    label = top_control(app, panel, w.ttk.Label, text="Wheel target")
    point = pointer_point(label)
    emit_wheel(label, sequence="<Button-5>", point=point)
    assert panel.canvas.yview()[0] > 0
    emit_wheel(label, sequence="<Button-4>", point=point)
    assert panel.canvas.yview()[0] == 0


def test_library_list_and_detail_scroll_independently_under_pointer(app):
    w.atomic_json_write(w._saved_words_path(), [
        {"word": f"sample{index}", "translation": "переклад"} for index in range(35)])
    app.open_saved_words()
    app.saved_win.lift()
    app.root.update()
    tree, detail = app.saved_tree, app.library_detail
    detail.canvas.yview_moveto(0)
    before = detail.canvas.yview()
    emit_wheel(tree)
    assert tree.yview()[0] > 0
    assert detail.canvas.yview() == before
    tree_before = tree.yview()
    emit_wheel(app.editor_fields["translation"])
    assert detail.canvas.yview()[0] > 0
    assert tree.yview() == tree_before


def test_one_router_per_window_cleans_tcl_commands_without_breaking_other_tabs(app):
    router = app.root._word_lookup_wheel_router
    panels = [app.root.nametowidget(path) for path in app.notebook.tabs()]
    assert all(panel._wheel_router is router for panel in panels)
    top = w.tk.Toplevel(app.root)
    panel = w.ScrolledPanel(top)
    panel.pack()
    commands = list(panel._wheel_router._commands.values())
    app.root.update()
    top.destroy()
    assert app.root._word_lookup_wheel_router is router
    for command in commands:
        assert command not in app.root._tclCommands
        assert not app.root.tk.call("info", "commands", command)
    button = top_control(app, panels[-1], w.ttk.Button, text="Still scrollable")
    emit_wheel(button)
    assert panels[-1].canvas.yview()[0] > 0
