"""Anki exports and local AnkiConnect with explicit library dependencies."""
from __future__ import annotations
from word_lookup_package import write_package
import base64
import copy
import hashlib
import html
import importlib.util
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
_WORD_FRONT = '<div class="word">{{Word}}</div>{{tts en_US:Word}}'
_APKG_CSS = (".card{font-family:arial;font-size:20px;text-align:left;color:#222;background:#fff}"
             ".nightMode .card{color:#eee;background:#2c2c2c}"
             ".word{font-size:30px;text-align:center}"
             ".tr{font-size:26px;font-weight:bold;text-align:center;margin:8px 0}"
             ".lbl{font-size:14px;opacity:.7;margin-top:14px}"
             ".box{background:rgba(127,127,127,.15);border-radius:8px;padding:10px 12px;margin-top:4px}")

class AnkiConnectError(RuntimeError):
    pass



class Anki:
    def __init__(self, settings, library, providers, logger):
        self.settings, self.library, self.providers, self.logger = settings, library, providers, logger

    def _snapshot_entries(self, entries):
        with self.library.lock:
            return copy.deepcopy(list(entries))


    def _anki_field(self, text: str) -> str:
        """Anki-текстовий імпорт розбиває поля по табуляції й рядках — прибираємо їх."""
        return " ".join((text or "").split())


    def _anki_back(self, e: dict) -> str:
        """Зворотна сторона для .txt (Basic: 2 поля): переклад — визначення — приклад.
        Якщо геть нічого немає — зрозуміла підказка замість порожньої картки."""
        parts = []
        translation = self._anki_field(e.get("translation", ""))
        if translation:
            parts.append(translation)
        definition = self._anki_field(e.get("definition", ""))
        if definition:
            parts.append(definition)
        definition_tr = self._anki_field(e.get("definition_tr", ""))
        if definition and definition_tr:
            parts.append(definition_tr)
        example = self._anki_field(e.get("example", ""))
        if example:
            parts.append(f"«{example}»")
        context = self._anki_field(e.get("context", ""))
        if context:
            parts.append(f"з гри: «{context}»")
        if not parts:
            return "(переклад відсутній — видали це слово зі списку й збережи ще раз)"
        return " — ".join(parts)


    def export_anki_txt(self, entries: list, path: str) -> None:
        """Простий текстовий файл для Anki (File -> Import): рядок = слово<TAB>зворотна
        сторона. Службових рядків-директив свідомо мінімум (лише роздільник): зайві
        (#columns тощо) різні версії Anki трактують по-різному."""
        entries = self._snapshot_entries(entries)
        lines = ["#separator:tab", "#html:false"]
        for e in entries:
            front = self._anki_field(e.get("word", ""))
            if not front:
                continue
            lines.append(f"{front}\t{self._anki_back(e)}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


    def _needs_enrich(self, e: dict) -> bool:
        if not e.get("word"):
            return False
        edited = e.get("edited_fields", [])
        return ((not e.get("translation") and "translation" not in edited)
                or (e.get("kind") != "phrase" and not e.get("definition")
                    and "definition" not in edited))


    def enrich_entry(self, e: dict) -> bool:
        """Дотягує те, чого бракує у збереженому слові: переклад, визначення, приклад.
        Ходить у мережу — викликати НЕ з потоку інтерфейсу. True, якщо щось змінилось."""
        changed = False
        word = e.get("word", "")
        if not e.get("translation") and "translation" not in e.get("edited_fields", []):
            main, groups, _note, _lim = self.providers._translate_word(word)
            tr = main or (groups[0][1][0] if groups else "")
            if tr:
                e["translation"] = tr
                changed = True
        if (e.get("kind") != "phrase" and not e.get("definition")
                and "definition" not in e.get("edited_fields", [])):
            try:
                info = self.providers.english_dictionary(word)
            except Exception as ex:  # мережа/словник недоступні — не позначаємо, спробуємо наступного разу
                self.logger.write(f"Дозаповнення «{word}»: {ex}")
                return changed
            pseudo = {"meanings": info.get("meanings", [])}
            d, ex_sentence = self.library._card_definition(pseudo), self.library._card_example(pseudo)
            if d:
                e["definition"] = d
                e["enriched"] = True
                changed = True
            if ex_sentence and not e.get("example") and "example" not in e.get("edited_fields", []):
                e["example"] = ex_sentence
                changed = True
            if info.get("base") and not e.get("base"):
                e["base"] = info["base"]
                changed = True
        return changed


    def enrich_entries(self, entries: list, timeout: float = 40.0) -> int:
        """Паралельно (4 потоки) дозаповнює всі неповні слова зі списку й зберігає файл.
        Загальний ліміт часу — щоб експорт не чекав вічно. Повертає, скільком слів додано дані."""
        with self.library.lock:
            seen = set()
            todo = []
            for entry in entries:
                if id(entry) not in seen and isinstance(entry, dict) and self._needs_enrich(entry):
                    seen.add(id(entry))
                    before = copy.deepcopy(entry)
                    todo.append((entry, before, copy.deepcopy(before)))
        if not todo:
            return 0
        completed = []
        pool = ThreadPoolExecutor(max_workers=4)
        futs = {pool.submit(self.enrich_entry, candidate): (entry, before, candidate)
                for entry, before, candidate in todo}
        try:
            for f in as_completed(futs, timeout=timeout):
                try:
                    if f.result():
                        completed.append(futs[f])
                except Exception as ex:
                    self.logger.write(f"Дозаповнення слова: {type(ex).__name__}: {ex}")
        except TimeoutError as ex:  # вичерпано загальний час
            self.logger.write(f"Дозаповнення: не всі слова встигли ({type(ex).__name__})")
        finally:
            # Running requests may finish later, but they own only detached copies.
            pool.shutdown(wait=False, cancel_futures=True)
        return self.library.merge_saved_enrichments(completed)


    def apkg_available(self) -> bool:
        return importlib.util.find_spec("genanki") is not None


    def _emphasize(self, text: str, words, tag: str) -> str:
        """Екранує HTML і виділяє в тексті саме це слово (та його початкову форму)."""
        terms = sorted({w for w in words if isinstance(w, str) and len(w) >= 3}, key=len, reverse=True)
        text = text or ""
        if not terms:
            return html.escape(text, quote=False)
        pattern = r"\b(?:" + "|".join(re.escape(word) for word in terms) + r")\b"
        parts, end = [], 0
        for match in re.finditer(pattern, text, flags=re.I):
            parts.append(html.escape(text[end:match.start()], quote=False))
            parts.append(f"<{tag}>" + html.escape(match.group(), quote=False) + f"</{tag}>")
            end = match.end()
        parts.append(html.escape(text[end:], quote=False))
        return "".join(parts)


    def _anki_html_fields(self, e: dict) -> list:
        """Поля картки .apkg: [Word, Translation, Meaning, Example]. У Meaning слово
        курсивом, у Example — жирним (як у звичайних колодах для вивчення мови)."""
        word = self._anki_field(e.get("word", ""))
        words = [word, e.get("base", "")]
        tr = self._anki_field(e.get("translation", ""))
        meaning = self._anki_field(e.get("definition", ""))
        example = self._anki_field(e.get("example", ""))
        context = self._anki_field(e.get("context", ""))
        if not (tr or meaning or example or context):
            tr = "(переклад відсутній — видали це слово зі списку й збережи ще раз)"
        meaning_html = self._emphasize(meaning, words, "i")
        definition_tr = self._anki_field(e.get("definition_tr", ""))
        if meaning and definition_tr:
            meaning_html += "<br>" + html.escape(definition_tr, quote=False)
        context_html = self._emphasize(context, words, "b")
        context_tr = self._anki_field(e.get("context_tr", ""))
        if context_tr:
            context_html += "<br><i>" + html.escape(context_tr, quote=False) + "</i>"
        if e.get("notes"):
            context_html += "<br><i>" + html.escape(self._anki_field(e["notes"]), quote=False) + "</i>"
        image_path = self.library.context_image_path(e.get("image_file", ""))
        if image_path and os.path.isfile(image_path):
            context_html += '<br><img src="' + html.escape(e["image_file"], quote=True) + '">'
        return [html.escape(word, quote=False), html.escape(tr, quote=False),
                meaning_html, self._emphasize(example, words, "b"), context_html]


    def _entry_tag(self, e: dict) -> str:
        """Тег профілю гри, загальний тег або автоматична назва джерела."""
        profile_tag = self.library._anki_tag(e.get("anki_tag", ""))
        if profile_tag:
            return profile_tag
        a = self.settings.data.get("anki", {})
        manual = self.library._anki_tag(a.get("game", ""))
        if manual:
            return manual
        return self.library._anki_tag(e.get("source", "")) if a.get("auto_game_tag", True) else ""


    def _anki_identity(self, word):
        return "wordlookup::" + hashlib.sha256(word.casefold().encode("utf-8")).hexdigest()[:24]


    def anki_call(self, action, **params):
        """Лише локальна Anki; ключі й доступ до зовнішніх серверів не потрібні."""
        import requests
        try:
            response = requests.post("http://127.0.0.1:8765", json={
                "action": action, "version": 6, "params": params}, timeout=8,
                allow_redirects=False)
            if 300 <= response.status_code < 400:
                raise AnkiConnectError("AnkiConnect повернув перенаправлення; його відхилено")
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as e:
            raise AnkiConnectError("Anki недоступна. Запусти Anki з доповненням AnkiConnect "
                                   "на цьому ж комп'ютері (порт 8765).") from e
        if not isinstance(data, dict) or "result" not in data or "error" not in data:
            raise AnkiConnectError("AnkiConnect повернув некоректну відповідь")
        if data["error"]:
            raise AnkiConnectError(str(data["error"]))
        return data["result"]


    def _upgrade_anki_tts(self, model):
        """Add speech to the original front while preserving customized templates."""
        templates = self.anki_call("modelTemplates", modelName=model)
        if not isinstance(templates, dict):
            return
        updates = {}
        for name, template in templates.items():
            if template.get("Front") == '<div class="word">{{Word}}</div>':
                updates[name] = dict(template, Front=_WORD_FRONT)
        if updates:
            self.anki_call("updateModelTemplates", model={"name": model, "templates": updates})


    def sync_anki_entries(self, entries, deck_name="Word Lookup"):
        """Додає/оновлює тільки обрані записи; не видаляє нотатки й не запускає хмарний sync."""
        entries = self._snapshot_entries(entries)
        deck_name = deck_name.strip()
        if not deck_name:
            raise ValueError("Укажи назву колоди Anki")
        version = self.anki_call("version")
        if not isinstance(version, int) or version < 6:
            raise AnkiConnectError("Онови AnkiConnect до версії з підтримкою API 6")
        model = "Word Lookup Card"
        names = ["Word", "Translation", "Meaning", "Example", "Context"]
        existing_model = model in self.anki_call("modelNames")
        if not existing_model:
            self.anki_call("createModel", modelName=model, inOrderFields=names, css=_APKG_CSS,
                      cardTemplates=[{"Name": "Card 1", "Front": _WORD_FRONT,
                                      "Back": '{{FrontSide}}<hr id=answer><div class="tr">{{Translation}}</div>'
                                              '{{#Meaning}}<div class="box">{{Meaning}}</div>{{/Meaning}}'
                                              '{{#Example}}<div class="box">{{Example}}</div>{{/Example}}'
                                              '{{#Context}}<div class="box">{{Context}}</div>{{/Context}}'}])
        if set(self.anki_call("modelFieldNames", modelName=model)) != set(names):
            raise AnkiConnectError("Модель Word Lookup Card має інші поля; її не змінено")
        if existing_model:
            self._upgrade_anki_tts(model)
        self.anki_call("createDeck", deck=deck_name)
        result = {"added": 0, "updated": 0, "failed": 0, "errors": []}
        for e in entries:
            word = self._anki_field(e.get("word", ""))
            if not word:
                continue
            try:
                tag = self._anki_identity(word)
                def quote(value):
                    return value.replace("\\", "\\\\").replace('"', '\\"')
                prefix = 'deck:"' + quote(deck_name) + '" '
                ids = self.anki_call("findNotes", query=prefix + "tag:" + tag)
                if not ids:
                    ids = self.anki_call("findNotes", query=prefix + '"Word:' + quote(word) + '"')
                info = self.anki_call("notesInfo", notes=ids) if ids else []
                matches = [n for n in info if n.get("modelName") == model and
                           html.unescape(re.sub(r"<[^>]+>", "", n.get("fields", {}).get("Word", {}).get("value", "")))
                           .casefold() == word.casefold()]
                if len(matches) > 1:
                    raise AnkiConnectError("Знайдено кілька однакових нотаток; вибери їх вручну в Anki")
                fields = dict(zip(names, self._anki_html_fields(e)))
                image_path = self.library.context_image_path(e.get("image_file", ""))
                if image_path and os.path.isfile(image_path):
                    with open(image_path, "rb") as f:
                        media = base64.b64encode(f.read()).decode("ascii")
                    stored = self.anki_call("storeMediaFile", filename=e["image_file"], data=media)
                    if stored != e["image_file"]:
                        raise AnkiConnectError("AnkiConnect не підтвердив запис зображення")
                tags = [tag] + ([self._entry_tag(e)] if self._entry_tag(e) else [])
                if matches:
                    nid = matches[0]["noteId"]
                    self.anki_call("updateNoteFields", note={"id": nid, "fields": fields})
                    self.anki_call("addTags", notes=[nid], tags=" ".join(tags))
                    result["updated"] += 1
                else:
                    nid = self.anki_call("addNote", note={"deckName": deck_name, "modelName": model,
                                                   "fields": fields, "tags": tags,
                                                   "options": {"allowDuplicate": False, "duplicateScope": "deck"}})
                    if nid is None:
                        raise AnkiConnectError("Anki не додала нотатку")
                    result["added"] += 1
            except Exception as e:
                result["failed"] += 1
                result["errors"].append(f"{word}: {e}")
        return result


    def export_anki_apkg(self, entries: list, path: str, deck_name: str = "Word Lookup") -> None:
        """Готова колода Anki (.apkg): подвійний клік — і картки вже в Anki. Картка:
        спереду слово; ззаду переклад, окремі блоки Meaning і Example. Потребує
        бібліотеку genanki (pip install genanki)."""
        entries = self._snapshot_entries(entries)
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
                "qfmt": _WORD_FRONT,
                "afmt": ('{{FrontSide}}<hr id=answer><div class="tr">{{Translation}}</div>'
                         '{{#Meaning}}<div class="lbl">Meaning</div><div class="box">{{Meaning}}</div>{{/Meaning}}'
                         '{{#Example}}<div class="lbl">Example</div><div class="box">{{Example}}</div>{{/Example}}'
                         '{{#Context}}<div class="lbl">From the game</div><div class="box">{{Context}}</div>{{/Context}}'),
            }],
            css=_APKG_CSS)
        deck_id = int(hashlib.md5(deck_name.encode("utf-8")).hexdigest()[:8], 16)
        deck = genanki.Deck(deck_id, deck_name)
        added = 0
        for e in entries:
            word = self._anki_field(e.get("word", ""))
            if not word:
                continue
            # стабільний guid за словом: повторний імпорт ОНОВЛЮЄ картку, а не дублює
            tag = self._entry_tag(e)
            extra = {"tags": [self._anki_identity(word)] + ([tag] if tag else [])}
            deck.add_note(genanki.Note(model=model, fields=self._anki_html_fields(e),
                                       guid=genanki.guid_for(word.lower()), **extra))
            added += 1
        if added == 0:
            raise ValueError("Немає жодного слова з назвою для експорту")
        media_files = {self.library.context_image_path(e.get("image_file", "")) for e in entries}
        media_files = sorted(p for p in media_files if p and os.path.isfile(p))
        write_package(genanki.Package(deck, media_files=media_files), path)

