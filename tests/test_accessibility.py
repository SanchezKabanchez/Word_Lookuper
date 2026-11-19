"""Keyboard focus and readable text colors in the actual Tk popup."""
import pytest
import word_lookup as w


def luminance(color):
    values = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in values]
    return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2]


@pytest.mark.parametrize('theme', list(w.THEMES))
@pytest.mark.parametrize('role', ['fg', 'dim', 'head', 'link', 'example'])
def test_theme_text_meets_wcag_normal_text_contrast(theme, role):
    palette = w.THEMES[theme]
    values = sorted([luminance(palette[role]), luminance(palette['bg'])])
    assert (values[1] + .05) / (values[0] + .05) >= 4.5


def test_headword_is_reachable_by_tab_and_edits_from_keyboard(app, card):
    app.show_lookup(card, None, (400, 350, 470, 370))
    app.root.update()
    title = app.popup.word_title
    assert str(title.cget('takefocus')) == '1'
    assert title.bind('<Return>')
    assert title.bind('<space>')
    # The traversal helper is a lazy Tcl script; do not depend on an earlier
    # native focus/Tab event having loaded it in this fresh interpreter.
    title.tk.call('auto_load', 'tk_focusNext')
    assert str(title.tk.call('tk_focusNext', str(app.popup.toolbar_buttons[-1]))) == str(title)
    title.focus_force()
    title.event_generate('<Return>')
    app.root.update()
    assert app.popup.correction_row.winfo_ismapped()
