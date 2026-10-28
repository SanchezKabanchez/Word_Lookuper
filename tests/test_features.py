import base64
import copy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from PIL import Image
import pytest
import word_lookup as w


def test_phrase_saves_and_reopens_offline_with_image(app):
    phrase = w.text_card("How did you become a herbalist?", "Як ти став травником?")
    phrase["_context_image"] = Image.new("RGB", (320, 160), "navy")
    app.show_lookup(phrase, None)
    app.popup.save_current()
    saved = w.load_saved_words()[0]
    assert saved["kind"] == "phrase"
    assert not w._needs_enrich(saved)
    assert w.context_image_path(saved["image_file"])
    app.open_saved_word(phrase["word"])
    assert app.popup.card["kind"] == "text"
    assert app.popup.card["translation"] == "Як ти став травником?"
    assert app.popup.context_photo is not None


def test_library_details_edit_persist_and_do_not_get_overwritten(app, card):
    w.add_saved_word(card)
    app.open_saved_words()
    app.root.update()
    assert app._editor_word == card["word"]
    text = app.editor_fields["translation"]
    text.delete("1.0", "end")
    text.insert("1.0", "мій переклад")
    app.editor_fields["notes"].insert("1.0", "My mnemonic")
    app.editor_status.set("Знаю")
    app.root.update()
    assert app._editor_dirty
    assert app.save_saved_details()
    assert not app._editor_dirty
    w.add_saved_word(card)
    saved = w.saved_entry(card["word"])
    assert saved["translation"] == "мій переклад"
    assert saved["status"] == "known"
    assert "translation" in saved["edited_fields"]
    app.saved_state_filter.set("Нові")
    assert app._visible_saved_words() == []
    app.saved_state_filter.set("Знаю")
    app.saved_query.set("mnemonic")
    assert app._visible_saved_words() == [saved]
    w._saved_words = None
    assert w.load_saved_words()[0]["notes"] == "My mnemonic"


def test_clear_definition_is_respected_by_offline_card_and_export(card):
    w.add_saved_word(card)
    entry = w.update_saved_entry(card["word"], {"definition": "", "example": "My example"})
    w.add_saved_word(card)
    assert w.saved_word_card(entry)["meanings"][0]["defs"][0] == {"d": "", "e": "My example"}
    assert not w._needs_enrich(entry)
    assert w._anki_html_fields(entry)[2] == ""


def test_editor_switch_cancel_preserves_unsaved_text(app, card, monkeypatch):
    w.add_saved_word(card)
    w.add_saved_word(dict(card, word="apple"))
    app.open_saved_words()
    app.show_saved_details(card["word"])
    app.editor_fields["translation"].insert("end", " edited")
    app.root.update()
    monkeypatch.setattr(w.messagebox, "askyesnocancel", lambda *args, **kwargs: None)
    app.saved_tree.selection_set(app._saved_tree_id("apple"))
    app._saved_tree_select()
    assert app._editor_word == card["word"]
    assert "edited" in app.editor_fields["translation"].get("1.0", "end")
    app._editor_dirty = False


def test_replace_image_preserves_entry_and_removes_old_media(app, card, tmp_path, monkeypatch):
    card["_context_image"] = Image.new("RGB", (100, 80), "navy")
    w.add_saved_word(card)
    old = w.context_image_path(w.saved_entry(card["word"])["image_file"])
    app.open_saved_words()
    app.show_saved_details(card["word"])
    image_file = tmp_path / "replacement.png"
    Image.new("RGB", (200, 150), "green").save(image_file)
    monkeypatch.setattr(w.filedialog, "askopenfilename", lambda **kwargs: str(image_file))
    app.replace_saved_image()
    entry = w.saved_entry(card["word"])
    assert entry["translation"] == card["translation"]
    assert w.context_image_path(entry["image_file"]) != old
    assert not w.os.path.exists(old)
    assert app.editor_photo is not None


def test_quit_cancel_keeps_editor_and_app_alive(app, card, monkeypatch):
    w.add_saved_word(card)
    app.open_saved_words()
    app.show_saved_details(card["word"])
    app.editor_fields["notes"].insert("1.0", "Unsaved note")
    app.root.update()
    monkeypatch.setattr(w.messagebox, "askyesnocancel", lambda *a, **kw: None)
    app.quit()
    assert app.root.winfo_exists()
    assert app._editor_dirty
    app._editor_dirty = False


def test_schedule_persists_and_due_filter_changes(card):
    w.add_saved_word(card)
    entry = w.saved_entry(card["word"])
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    assert w.review_due(entry, now)
    w.record_review(entry, True, now)
    assert not w.review_due(entry, now)
    assert w.review_due(entry, now + timedelta(days=1))
    w.record_review(entry, True, now + timedelta(days=1))
    assert datetime.fromisoformat(entry["review"]["due"]) == now + timedelta(days=4)
    w.record_review(entry, False, now)
    assert entry["review"]["streak"] == 0
    assert w.review_due(entry, now + timedelta(minutes=10))
    w._saved_words = None
    assert w.load_saved_words()[0]["review"]["attempts"] == 3


def test_failed_disk_write_rolls_back_edit_review_and_save(card, monkeypatch):
    w.add_saved_word(card)
    entry = w.saved_entry(card["word"])
    original = copy.deepcopy(entry)
    monkeypatch.setattr(w, "_write_saved_words", lambda: False)
    with pytest.raises(OSError):
        w.update_saved_entry(card["word"], {"translation": "changed"})
    assert entry == original
    with pytest.raises(OSError):
        w.record_review(entry, True)
    assert entry == original
    assert not w.add_saved_word(dict(card, word="apple"))
    assert len(w.load_saved_words()) == 1
    assert not w.remove_saved_word(card["word"])
    assert w.load_saved_words() == [original]


def test_reverse_and_cloze_review_render_real_prompts(app, card):
    w.add_saved_word(card)
    review = w.ReviewWindow(app, w.load_saved_words())
    review.mode.set("Пропуск у реченні")
    review.next_card()
    labels = [c.cget("text") for c in review.body.winfo_children()]
    assert "That is an ______." in labels
    review.mode.set("Переклад → Англійська")
    review.next_card()
    labels = [c.cget("text") for c in review.body.winfo_children()]
    assert card["translation"] in labels
    assert card["word"] not in labels
    review.reveal()
    assert card["word"] in [c.cget("text") for c in review.body.winfo_children()]
    review.win.destroy()


def test_region_translation_screenshot_is_saved_without_polluting_cache(app, monkeypatch):
    w.cfg["learning"]["capture_context"] = True
    image = Image.new("RGB", (300, 100), "white")
    monkeypatch.setattr(w, "ocr_region", lambda *args, **kwargs: [
        w.W("hello", 10, 10, 100, 40, 1), w.W("world", 120, 10, 210, 40, 1)])
    monkeypatch.setattr(w, "translate_text", lambda text: "привіт, світе")
    token = app._new_token()
    app._region_thread(("region", 0, 0, 300, 100), (image, 0, 0), token)
    events = []
    while not w.events.empty():
        events.append(w.events.get_nowait())
    show = next(ev for ev in events if ev[0] == "show")
    assert show[1]["_context_image"].size == (300, 100)
    assert show[1]["entry_kind"] == "phrase"
    w.save_card_cache()  # PIL images must never enter the JSON dictionary cache.


@pytest.fixture
def fake_anki(monkeypatch):
    state = {"models": [], "notes": {}, "media": {}, "calls": []}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert request["version"] == 6
            action, p = request["action"], request["params"]
            state["calls"].append((action, p))
            error = None
            if action == "version": result = 6
            elif action == "modelNames": result = state["models"]
            elif action == "createModel":
                state["models"].append(p["modelName"]); result = None
            elif action == "modelFieldNames": result = ["Word", "Translation", "Meaning", "Example", "Context"]
            elif action == "createDeck": result = 1
            elif action == "findNotes":
                result = [nid for nid, n in state["notes"].items() if
                          any("tag:" + tag in p["query"] for tag in n["tags"])]
            elif action == "notesInfo":
                result = [{"noteId": nid, "modelName": n["modelName"],
                           "fields": {k: {"value": v} for k, v in n["fields"].items()}}
                          for nid in p["notes"] for n in [state["notes"][nid]]]
            elif action == "addNote":
                result = len(state["notes"]) + 1
                state["notes"][result] = p["note"]
            elif action == "updateNoteFields":
                state["notes"][p["note"]["id"]]["fields"].update(p["note"]["fields"]); result = None
            elif action == "addTags": result = None
            elif action == "storeMediaFile":
                state["media"][p["filename"]] = base64.b64decode(p["data"]); result = p["filename"]
            else: result, error = None, "Unsupported action " + action
            data = json.dumps({"result": result, "error": error}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    post = w.requests.post
    def route_to_test_server(url, **kwargs):
        assert url == "http://127.0.0.1:8765"
        return post(f"http://127.0.0.1:{server.server_port}", **kwargs)
    monkeypatch.setattr(w.requests, "post", route_to_test_server)
    yield state
    server.shutdown(); thread.join(); server.server_close()


def test_anki_add_update_no_duplicates_and_real_media_transfer(fake_anki, card):
    card["_context_image"] = Image.new("RGB", (100, 80), "navy")
    w.add_saved_word(card)
    entries = w.load_saved_words()
    result = w.sync_anki_entries(entries, "My vocabulary")
    assert result == {"added": 1, "updated": 0, "failed": 0, "errors": []}
    entries[0]["translation"] = "Updated translation"
    result = w.sync_anki_entries(entries, "My vocabulary")
    assert result["updated"] == 1 and result["added"] == 0
    assert len(fake_anki["notes"]) == 1
    assert fake_anki["notes"][1]["fields"]["Translation"] == "Updated translation"
    assert fake_anki["notes"][1]["deckName"] == "My vocabulary"
    assert fake_anki["media"][entries[0]["image_file"]].startswith(b"\xff\xd8")
    assert all(action not in ("deleteNotes", "sync") for action, _ in fake_anki["calls"])


def test_anki_unavailable_keeps_local_data(card, monkeypatch):
    w.add_saved_word(card)
    before = copy.deepcopy(w.load_saved_words())
    def unavailable(*args, **kwargs):
        raise w.requests.ConnectionError("No Anki")
    monkeypatch.setattr(w.requests, "post", unavailable)
    with pytest.raises(w.AnkiConnectError, match="AnkiConnect"):
        w.sync_anki_entries(w.load_saved_words())
    assert w.load_saved_words() == before
