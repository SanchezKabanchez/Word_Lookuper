"""Overlay hierarchy stays concise, with optional details and safe DPI wrapping."""
import copy

import pytest
import word_lookup as w


def texts(widget):
    result = []
    if isinstance(widget, w.tk.Text):
        result.append(widget.get("1.0", "end-1c"))
    else:
        try:
            result.append(str(widget.cget("text")))
        except w.tk.TclError:
            pass
    for child in widget.winfo_children():
        result.extend(texts(child))
    return result


def test_default_card_keeps_one_primary_sense_and_hides_secondary_blocks(app, card):
    card["groups"] = [("noun", [card["translation"], "SECONDARYTRANSLATION", "THIRDTRANSLATION"])]
    card["meanings"].append({"pos": "verb", "defs": [{"d": "SECONDMEANING", "e": ""}], "syn": [], "ant": []})
    original = copy.deepcopy(card)
    app.show_lookup(card, None)
    app.root.update()
    popup = app.popup
    visible = "\n".join(texts(popup.content))
    assert popup.primary_translation.cget("text") == card["translation"]
    assert "SECONDARYTRANSLATION" not in visible
    assert "THIRDTRANSLATION" not in visible
    assert "SECONDMEANING" not in visible
    assert "minimization" not in visible
    assert card["meanings"][0]["defs"][0]["e"] not in visible
    assert card["sentence"] in visible
    assert card["sentence_tr"] in visible
    assert card["definition_tr"] in visible
    assert popup.win.winfo_height() < 600
    assert card == original


def test_alternative_translation_switch_reveals_separate_section_without_expanding_headline(app, card):
    card["translation"] = "скриня"
    card["groups"] = [("іменник", ["груди", "скриня", "грудна клітка"])]
    w.cfg["popup"]["alternate_translations"] = True
    app.show_lookup(card, None)
    popup = app.popup
    visible = texts(popup.content)
    assert popup.primary_translation.cget("text") == "скриня"
    assert "Інші варіанти перекладу" in visible
    assert "іменник: груди, грудна клітка" in visible
    assert all("скриня" not in text for text in visible if text.startswith("іменник:"))


@pytest.mark.parametrize("theme", ["dark", "light", "sepia"])
def test_definition_languages_have_distinct_labels_and_emphasis(app, card, theme):
    w.cfg["popup"]["theme"] = theme
    app.show_lookup(card, None)
    english, translated = app.popup.definition_blocks[:2]
    assert english[0] == english[1].cget("text") == "EN"
    assert translated[0] == translated[1].cget("text") == "UK"
    assert english[2].cget("text") == card["meanings"][0]["defs"][0]["d"]
    assert translated[2].cget("text") == card["definition_tr"]
    assert english[2].cget("foreground") != translated[2].cget("foreground")
    assert english[1].cget("background") != translated[1].cget("background")
    style = w.ttk.Style(app.root)
    scrollbar = app.popup.viewport.bar.cget("style")
    assert style.lookup(scrollbar, "troughcolor") == app.popup.th["bg"]
    assert style.lookup(scrollbar, "background", ("active",)) == app.popup.th["border"]


@pytest.mark.parametrize("font_size,scaling", [(10, 1.0), (24, 2.0)])
def test_synonym_chips_and_definition_paragraphs_wrap_inside_narrow_cards(app, card, font_size, scaling):
    previous_scaling = app.root.tk.call("tk", "scaling")
    try:
        app.root.tk.call("tk", "scaling", scaling)
        w.cfg["popup"].update(width=300, font_size=font_size)
        w.cfg["sections"]["thesaurus"] = True
        w.cfg["dictionary"]["max_synonyms"] = 20
        card["meanings"][0]["syn"] = ["minimization", "trivialization", "understatedness",
                                      "an extraordinarily lengthy synonym phrase that needs internal wrapping"]
        app.show_lookup(card, None)
        app.root.update()
        popup = app.popup
        viewport = popup.viewport.canvas
        left, right = viewport.winfo_rootx(), viewport.winfo_rootx() + viewport.winfo_width()
        chips = popup.synonym_chips
        assert len(chips) == 4
        assert len({chip.winfo_rooty() for chip in chips}) > 1
        for widget in chips + [paragraph for _language, _tag, paragraph in popup.definition_blocks]:
            assert left <= widget.winfo_rootx()
            assert widget.winfo_rootx() + widget.winfo_width() <= right
            assert widget.winfo_height() >= widget.winfo_reqheight()
        assert all(chip.cget("background") == popup.th["line"] for chip in chips)
    finally:
        app.close_all()
        app.root.tk.call("tk", "scaling", previous_scaling)


def test_new_defaults_do_not_override_any_saved_card_preferences():
    saved = {"version": 6, "popup": {"font_size": 11, "width": 480, "examples": True,
             "alternate_translations": True, "context_image": True, "sentence_translation": False},
             "sections": {"definitions": False, "thesaurus": True},
             "dictionary": {"max_definitions": 2, "max_synonyms": 5}}
    w.atomic_json_write(w.config_path(), saved)
    restored = w.load_config()
    for section in ("popup", "sections", "dictionary"):
        for key, value in saved[section].items():
            assert restored[section][key] == value
    assert restored["version"] == w.DEFAULT_CONFIG["version"]


def test_balanced_and_detailed_presets_expose_different_amounts_of_content(app, card):
    original = copy.deepcopy(card)
    app.apply_card_preset("detailed")
    app.show_lookup(card, None)
    assert app.popup.synonym_chips
    assert w.cfg["popup"]["examples"]
    assert w.cfg["popup"]["alternate_translations"]
    app.apply_card_preset("balanced")
    app.show_lookup(card, None)
    assert not hasattr(app.popup, "synonym_chips")
    assert app.popup.definition_blocks
    assert not w.cfg["popup"]["alternate_translations"]
    assert w.cfg["dictionary"]["max_definitions"] == 1
    app.apply_card_preset("compact")
    app.show_lookup(card, None)
    assert not hasattr(app.popup, "definition_blocks")
    assert card == original
