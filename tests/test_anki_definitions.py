"""Clearing an English definition must also hide its stale translated text."""
import sqlite3
import zipfile

import pytest
import word_lookup as w


@pytest.mark.parametrize("definition", ["", " \t\n", "An English explanation."])
def test_definition_translation_follows_definition_in_txt_and_apkg(tmp_path, definition):
    entry = {"word": "footing", "translation": "опора", "definition": definition,
             "definition_tr": "ЗАСТАРІЛЕ ВИЗНАЧЕННЯ", "edited_fields": ["definition"]}
    txt = tmp_path / "deck.txt"
    w.export_anki_txt([entry], str(txt))
    apkg = tmp_path / "deck.apkg"
    w.export_anki_apkg([entry], str(apkg))
    with zipfile.ZipFile(apkg) as archive:
        archive.extract("collection.anki2", tmp_path)
    with sqlite3.connect(tmp_path / "collection.anki2") as db:
        fields = db.execute("select flds from notes").fetchone()[0].split("\x1f")
    assert fields[1] == "опора"
    assert (entry["definition_tr"] in txt.read_text()) is bool(definition.strip())
    assert (entry["definition_tr"] in fields[2]) is bool(definition.strip())
    if not definition.strip():
        assert fields[2] == ""


def test_ankiconnect_clears_translated_definition_on_update(monkeypatch):
    captured = {}
    def anki(action, **params):
        if action == "version": return 6
        if action == "modelNames": return ["Word Lookup Card"]
        if action == "modelFieldNames": return ["Word", "Translation", "Meaning", "Example", "Context"]
        if action == "findNotes": return [42]
        if action == "notesInfo":
            return [{"noteId": 42, "modelName": "Word Lookup Card",
                     "fields": {"Word": {"value": "footing"}}}]
        if action == "updateNoteFields": captured.update(params["note"])
    monkeypatch.setattr(w, "anki_call", anki)
    result = w.sync_anki_entries([{"word": "footing", "translation": "опора",
                                   "definition": "", "definition_tr": "stale"}])
    assert result["updated"] == 1
    assert captured["id"] == 42
    assert captured["fields"]["Meaning"] == ""


def test_ankiconnect_upgrades_only_unchanged_front_for_builtin_tts(monkeypatch):
    captured = {}
    templates = {"Card 1": {"Front": '<div class="word">{{Word}}</div>', "Back": "custom back"},
                 "Custom": {"Front": "my custom {{Word}}", "Back": "keep me"}}
    def anki(action, **params):
        if action == "version": return 6
        if action == "modelNames": return ["Word Lookup Card"]
        if action == "modelFieldNames": return ["Word", "Translation", "Meaning", "Example", "Context"]
        if action == "modelTemplates": return templates
        if action == "updateModelTemplates": captured.update(params["model"])
    monkeypatch.setattr(w, "anki_call", anki)
    w.sync_anki_entries([])
    assert captured["templates"] == {"Card 1": {
        "Front": '<div class="word">{{Word}}</div>{{tts en_US:Word}}', "Back": "custom back"}}


def test_ankiconnect_rejects_redirect_without_contacting_remote_host(monkeypatch):
    calls = []
    def redirect(url, **kwargs):
        calls.append((url, kwargs))
        response = w.requests.Response()
        response.status_code = 302
        response.headers["Location"] = "https://example.com/collect"
        return response
    monkeypatch.setattr(w.requests, "post", redirect)
    with pytest.raises(w.AnkiConnectError, match="перенаправлення"):
        w.anki_call("version")
    assert len(calls) == 1
    assert calls[0][0] == "http://127.0.0.1:8765"
    assert calls[0][1]["allow_redirects"] is False
