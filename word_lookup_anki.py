"""Anki text/package export and local AnkiConnect protocol.

The runtime supplies configuration, shared state and overridable service adapters.
The entry point reexports this API for existing integrations.
"""
from __future__ import annotations

runtime = None


def configure(application):
    global runtime
    runtime = application


def _anki_field(text: str) -> str:
    """Anki-текстовий імпорт розбиває поля по табуляції й рядках — прибираємо їх."""
    return " ".join((text or "").split())


def _anki_back(e: dict) -> str:
    """Зворотна сторона для .txt (Basic: 2 поля): переклад — визначення — приклад.
    Якщо геть нічого немає — зрозуміла підказка замість порожньої картки."""
    parts = []
    translation = runtime._anki_field(e.get("translation", ""))
    if translation:
        parts.append(translation)
    definition = runtime._anki_field(e.get("definition", ""))
    if definition:
        parts.append(definition)
    definition_tr = runtime._anki_field(e.get("definition_tr", ""))
    if definition and definition_tr:
        parts.append(definition_tr)
    example = runtime._anki_field(e.get("example", ""))
    if example:
        parts.append(f"«{example}»")
    context = runtime._anki_field(e.get("context", ""))
    if context:
        parts.append(f"з гри: «{context}»")
    if not parts:
        return "(переклад відсутній — видали це слово зі списку й збережи ще раз)"
    return " — ".join(parts)


def export_anki_txt(entries: list, path: str) -> None:
    """Простий текстовий файл для Anki (File -> Import): рядок = слово<TAB>зворотна
    сторона. Службових рядків-директив свідомо мінімум (лише роздільник): зайві
    (#columns тощо) різні версії Anki трактують по-різному."""
    lines = ["#separator:tab", "#html:false"]
    for e in entries:
        front = runtime._anki_field(e.get("word", ""))
        if not front:
            continue
        lines.append(f"{front}\t{runtime._anki_back(e)}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _needs_enrich(e: dict) -> bool:
    if not e.get("word"):
        return False
    edited = e.get("edited_fields", [])
    return ((not e.get("translation") and "translation" not in edited)
            or (e.get("kind") != "phrase" and not e.get("definition")
                and not e.get("enriched") and "definition" not in edited))


def enrich_entry(e: dict) -> bool:
    """Дотягує те, чого бракує у збереженому слові: переклад, визначення, приклад.
    Ходить у мережу — викликати НЕ з потоку інтерфейсу. True, якщо щось змінилось."""
    changed = False
    word = e.get("word", "")
    if not e.get("translation") and "translation" not in e.get("edited_fields", []):
        main, groups, _note, _lim = runtime._translate_word(word)
        tr = main or (groups[0][1][0] if groups else "")
        if tr:
            e["translation"] = tr
            changed = True
    if (e.get("kind") != "phrase" and not e.get("definition") and not e.get("enriched")
            and "definition" not in e.get("edited_fields", [])):
        try:
            info = runtime.english_dictionary(word)
        except Exception as ex:  # мережа/словник недоступні — не позначаємо, спробуємо наступного разу
            runtime.log(f"Дозаповнення «{word}»: {ex}")
            return changed
        pseudo = {"meanings": info.get("meanings", [])}
        d, ex_sentence = runtime._card_definition(pseudo), runtime._card_example(pseudo)
        if d:
            e["definition"] = d
        if ex_sentence and not e.get("example") and "example" not in e.get("edited_fields", []):
            e["example"] = ex_sentence
        if info.get("base") and not e.get("base"):
            e["base"] = info["base"]
        e["enriched"] = True
        changed = True
    return changed


def enrich_entries(entries: list, timeout: float = 40.0) -> int:
    """Паралельно (4 потоки) дозаповнює всі неповні слова зі списку й зберігає файл.
    Загальний ліміт часу — щоб експорт не чекав вічно. Повертає, скільком слів додано дані."""
    todo = [e for e in entries if runtime._needs_enrich(e)]
    if not todo:
        return 0
    changed = 0
    pool = runtime.ThreadPoolExecutor(max_workers=4)
    futs = [pool.submit(runtime.enrich_entry, e) for e in todo]
    try:
        for f in runtime.as_completed(futs, timeout=timeout):
            try:
                if f.result():
                    changed += 1
            except Exception as ex:
                runtime.log(f"Дозаповнення слова: {type(ex).__name__}: {ex}")
    except Exception as ex:  # вичерпано загальний час
        runtime.log(f"Дозаповнення: не всі слова встигли ({type(ex).__name__})")
    finally:
        pool.shutdown(wait=False)
    if changed:
        runtime._write_saved_words()
    return changed


def apkg_available() -> bool:
    return runtime.importlib.util.find_spec("genanki") is not None


def _emphasize(text: str, words, tag: str) -> str:
    """Екранує HTML і виділяє в тексті саме це слово (та його початкову форму)."""
    out = runtime.html.escape(text or "", quote=False)
    for w in sorted({w.lower() for w in words if w and len(w) >= 3}, key=len, reverse=True):
        out = runtime.re.sub(rf"\b({runtime.re.escape(runtime.html.escape(w, quote=False))})\b",
                     rf"<{tag}>\1</{tag}>", out, flags=runtime.re.I)
    return out


def _anki_html_fields(e: dict) -> list:
    """Поля картки .apkg: [Word, Translation, Meaning, Example]. У Meaning слово
    курсивом, у Example — жирним (як у звичайних колодах для вивчення мови)."""
    word = runtime._anki_field(e.get("word", ""))
    words = [word, e.get("base", "")]
    tr = runtime._anki_field(e.get("translation", ""))
    meaning = runtime._anki_field(e.get("definition", ""))
    example = runtime._anki_field(e.get("example", ""))
    context = runtime._anki_field(e.get("context", ""))
    if not (tr or meaning or example or context):
        tr = "(переклад відсутній — видали це слово зі списку й збережи ще раз)"
    meaning_html = runtime._emphasize(meaning, words, "i")
    definition_tr = runtime._anki_field(e.get("definition_tr", ""))
    if meaning and definition_tr:
        meaning_html += "<br>" + runtime.html.escape(definition_tr, quote=False)
    context_html = runtime._emphasize(context, words, "b")
    context_tr = runtime._anki_field(e.get("context_tr", ""))
    if context_tr:
        context_html += "<br><i>" + runtime.html.escape(context_tr, quote=False) + "</i>"
    if e.get("notes"):
        context_html += "<br><i>" + runtime.html.escape(runtime._anki_field(e["notes"]), quote=False) + "</i>"
    image_path = runtime.context_image_path(e.get("image_file", ""))
    if image_path and runtime.os.path.isfile(image_path):
        context_html += '<br><img src="' + runtime.html.escape(e["image_file"], quote=True) + '">'
    return [runtime.html.escape(word, quote=False), runtime.html.escape(tr, quote=False),
            meaning_html, runtime._emphasize(example, words, "b"), context_html]


def _entry_tag(e: dict) -> str:
    """Тег гри для картки: назва з налаштувань (для всіх), інакше — з заголовка вікна гри."""
    a = runtime.cfg.get("anki", {})
    manual = runtime._anki_tag(a.get("game", ""))
    if manual:
        return manual
    return runtime._anki_tag(e.get("source", "")) if a.get("auto_game_tag", True) else ""


class AnkiConnectError(RuntimeError):
    pass


def _anki_identity(word):
    return "wordlookup::" + runtime.hashlib.sha256(word.casefold().encode("utf-8")).hexdigest()[:24]


def anki_call(action, **params):
    """Лише локальна Anki; ключі й доступ до зовнішніх серверів не потрібні."""
    try:
        response = runtime.requests.post("http://127.0.0.1:8765", json={
            "action": action, "version": 6, "params": params}, timeout=8)
        response.raise_for_status()
        data = response.json()
    except (runtime.requests.RequestException, ValueError) as e:
        raise runtime.AnkiConnectError("Anki недоступна. Запусти Anki з доповненням AnkiConnect "
                               "на цьому ж комп'ютері (порт 8765).") from e
    if not isinstance(data, dict) or "result" not in data or "error" not in data:
        raise runtime.AnkiConnectError("AnkiConnect повернув некоректну відповідь")
    if data["error"]:
        raise runtime.AnkiConnectError(str(data["error"]))
    return data["result"]


def sync_anki_entries(entries, deck_name="Word Lookup"):
    """Додає/оновлює тільки обрані записи; не видаляє нотатки й не запускає хмарний sync."""
    deck_name = deck_name.strip()
    if not deck_name:
        raise ValueError("Укажи назву колоди Anki")
    version = runtime.anki_call("version")
    if not isinstance(version, int) or version < 6:
        raise runtime.AnkiConnectError("Онови AnkiConnect до версії з підтримкою API 6")
    model = "Word Lookup Card"
    names = ["Word", "Translation", "Meaning", "Example", "Context"]
    if model not in runtime.anki_call("modelNames"):
        runtime.anki_call("createModel", modelName=model, inOrderFields=names, css=runtime._APKG_CSS,
                  cardTemplates=[{"Name": "Card 1", "Front": '<div class="word">{{Word}}</div>',
                                  "Back": '{{FrontSide}}<hr id=answer><div class="tr">{{Translation}}</div>'
                                          '{{#Meaning}}<div class="box">{{Meaning}}</div>{{/Meaning}}'
                                          '{{#Example}}<div class="box">{{Example}}</div>{{/Example}}'
                                          '{{#Context}}<div class="box">{{Context}}</div>{{/Context}}'}])
    if set(runtime.anki_call("modelFieldNames", modelName=model)) != set(names):
        raise runtime.AnkiConnectError("Модель Word Lookup Card має інші поля; її не змінено")
    runtime.anki_call("createDeck", deck=deck_name)
    result = {"added": 0, "updated": 0, "failed": 0, "errors": []}
    for e in entries:
        word = runtime._anki_field(e.get("word", ""))
        if not word:
            continue
        try:
            tag = runtime._anki_identity(word)
            quote = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
            prefix = 'deck:"' + quote(deck_name) + '" '
            ids = runtime.anki_call("findNotes", query=prefix + "tag:" + tag)
            if not ids:
                ids = runtime.anki_call("findNotes", query=prefix + '"Word:' + quote(word) + '"')
            info = runtime.anki_call("notesInfo", notes=ids) if ids else []
            matches = [n for n in info if n.get("modelName") == model and
                       runtime.html.unescape(runtime.re.sub(r"<[^>]+>", "", n.get("fields", {}).get("Word", {}).get("value", "")))
                       .casefold() == word.casefold()]
            if len(matches) > 1:
                raise runtime.AnkiConnectError("Знайдено кілька однакових нотаток; вибери їх вручну в Anki")
            fields = dict(zip(names, runtime._anki_html_fields(e)))
            image_path = runtime.context_image_path(e.get("image_file", ""))
            if image_path and runtime.os.path.isfile(image_path):
                with open(image_path, "rb") as f:
                    media = runtime.base64.b64encode(f.read()).decode("ascii")
                stored = runtime.anki_call("storeMediaFile", filename=e["image_file"], data=media)
                if stored != e["image_file"]:
                    raise runtime.AnkiConnectError("AnkiConnect не підтвердив запис зображення")
            tags = [tag] + ([runtime._entry_tag(e)] if runtime._entry_tag(e) else [])
            if matches:
                nid = matches[0]["noteId"]
                runtime.anki_call("updateNoteFields", note={"id": nid, "fields": fields})
                runtime.anki_call("addTags", notes=[nid], tags=" ".join(tags))
                result["updated"] += 1
            else:
                nid = runtime.anki_call("addNote", note={"deckName": deck_name, "modelName": model,
                                               "fields": fields, "tags": tags,
                                               "options": {"allowDuplicate": False, "duplicateScope": "deck"}})
                if nid is None:
                    raise runtime.AnkiConnectError("Anki не додала нотатку")
                result["added"] += 1
        except Exception as e:
            result["failed"] += 1
            result["errors"].append(f"{word}: {e}")
    return result


def export_anki_apkg(entries: list, path: str, deck_name: str = "Word Lookup") -> None:
    """Готова колода Anki (.apkg): подвійний клік — і картки вже в Anki. Картка:
    спереду слово; ззаду переклад, окремі блоки Meaning і Example. Потребує
    бібліотеку genanki (pip install genanki)."""
    try:
        import genanki
    except ImportError:
        raise ImportError("Для .apkg потрібна бібліотека: pip install genanki")
    model = genanki.Model(
        1746930003, "Word Lookup Card",
        fields=[{"name": "Word"}, {"name": "Translation"}, {"name": "Meaning"},
                {"name": "Example"}, {"name": "Context"}],
        templates=[{
            "name": "Card 1",
            "qfmt": '<div class="word">{{Word}}</div>',
            "afmt": ('{{FrontSide}}<hr id=answer><div class="tr">{{Translation}}</div>'
                     '{{#Meaning}}<div class="lbl">Meaning</div><div class="box">{{Meaning}}</div>{{/Meaning}}'
                     '{{#Example}}<div class="lbl">Example</div><div class="box">{{Example}}</div>{{/Example}}'
                     '{{#Context}}<div class="lbl">From the game</div><div class="box">{{Context}}</div>{{/Context}}'),
        }],
        css=runtime._APKG_CSS)
    deck_id = int(runtime.hashlib.md5(deck_name.encode("utf-8")).hexdigest()[:8], 16)
    deck = genanki.Deck(deck_id, deck_name)
    added = 0
    for e in entries:
        word = runtime._anki_field(e.get("word", ""))
        if not word:
            continue
        # стабільний guid за словом: повторний імпорт ОНОВЛЮЄ картку, а не дублює
        tag = runtime._entry_tag(e)
        extra = {"tags": [runtime._anki_identity(word)] + ([tag] if tag else [])}
        deck.add_note(genanki.Note(model=model, fields=runtime._anki_html_fields(e),
                                   guid=genanki.guid_for(word.lower()), **extra))
        added += 1
    if added == 0:
        raise ValueError("Немає жодного слова з назвою для експорту")
    media_files = {runtime.context_image_path(e.get("image_file", "")) for e in entries}
    media_files = sorted(p for p in media_files if p and runtime.os.path.isfile(p))
    genanki.Package(deck, media_files=media_files).write_to_file(path)
