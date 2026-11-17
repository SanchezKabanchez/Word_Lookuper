"""Saved words and review progress with explicit settings and logging."""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import tempfile
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from word_lookup_schemas import UnsupportedSchema, decode_words, encode_words
from word_lookup_utils import clamp

_SAVED_BACKUPS = 5
EDIT_FIELDS = ("translation", "definition", "definition_tr", "example", "context", "context_tr", "source", "notes")


def _saved_transaction(function):
    @wraps(function)
    def serialized(self, *args, **kwargs):
        with self.lock:
            return function(self, *args, **kwargs)
    return serialized


class Library:
    def __init__(self, settings, logger):
        self.settings, self.logger = settings, logger
        self.words = None
        self.state = {}
        self.lock = threading.RLock()
        self.edit_fields = EDIT_FIELDS

    def text_card(self, original, translation):
        return {"kind":"text", "word":original, "original":original,
                "translation":translation, "entry_kind":"phrase"}

    def _saved_words_path(self) -> str:
        return os.path.join(os.path.dirname(self.settings.path()), "saved_words.json")


    def _file_revision(self, path):
        try:
            info = Path(path).stat()
        except FileNotFoundError:
            return None
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


    def _read_saved_file(self, path):
        # Windows editors may add a UTF-8 BOM. Invalid data is an error, not an
        # empty library that can later be written over the user's progress.
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
            info = os.fstat(handle.fileno())
        data = decode_words(data)
        revision = info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
        return data, revision


    def read_saved_words_file(self, path):
        """Read a library or backup without changing the active library."""
        return self._read_saved_file(path)[0]


    def saved_words_error(self):
        return self.state.get("error", "")


    @_saved_transaction
    def load_saved_words(self) -> list:
        """Cached during lookups; opening the library explicitly reloads the file."""
        path = str(Path(self._saved_words_path()).absolute())
        if self.state.get("path") not in (None, path):
            self.words = None
        if self.words is not None:
            return self.words
        state = self.state
        state.clear()
        state.update(path=path, loaded=True, error="", revision=None)
        try:
            self.words, state["revision"] = self._read_saved_file(path)
        except FileNotFoundError:
            self.words = []
        except (OSError, ValueError):
            self.words = []
            state["error"] = ("Не вдалося прочитати бібліотеку. Файл не змінено. "
                              "Натисни «Оновити» або «Відновити з файлу…».")
            self.logger.write("Не вдалося прочитати бібліотеку; запис заблоковано до перечитування або відновлення.")
        return self.words


    @_saved_transaction
    def reload_saved_words(self):
        path = str(Path(self._saved_words_path()).absolute())
        previous = (self.words if self.state.get("path") in (None, path) else None)
        self.words = None
        loaded = self.load_saved_words()
        if previous and self.state.get("revision") is None and not self.saved_words_error():
            self.state["error"] = "Файл бібліотеки зник. Показано попередні записи; віднови файл перед збереженням."
        if self.saved_words_error() and previous is not None:
            self.words = previous
        else:
            self._adopt_saved_words(loaded, previous)
        return self.words


    def _adopt_saved_words(self, entries, previous):
        """Keep identities held by review windows and background enrichment."""
        if previous is None:
            self.words = entries
            return
        by_word = {e["word"].casefold(): e for e in previous}
        reconciled = []
        for entry in entries:
            old = by_word.get(entry["word"].casefold())
            if old is not None and old is not entry:
                old.clear()
                old.update(entry)
                entry = old
            reconciled.append(entry)
        previous[:] = reconciled
        self.words = previous


    def _remember_saved_revision(self, path):
        state = self.state
        state.update(path=str(Path(path).absolute()), loaded=True, error="")
        try:
            state["revision"] = self._file_revision(path)
        except OSError:
            # The commit succeeded. Keep memory committed too, but require a reload
            # before a further write when its revision could not be established.
            state["error"] = "Бібліотеку записано, але не вдалося перевірити файл. Натисни «Оновити»."


    def _preserve_before_restore(self, path):
        """Keep an exact, unrotated copy, including malformed input, before repair."""
        import shutil
        folder = path.with_suffix(".recovery")
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        snapshot = None
        try:
            with path.open("rb") as source, tempfile.NamedTemporaryFile(mode="wb", dir=folder,
                    prefix=f"saved_words-before-{time.time_ns():020d}-", suffix=".json", delete=False) as target:
                snapshot = Path(target.name)
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
        except OSError:
            if snapshot:
                snapshot.unlink(missing_ok=True)
            raise
        return snapshot


    @_saved_transaction
    def restore_saved_words(self, source):
        """Merge missing words from a selected file; existing edits and review win.

        Read the destination afresh, so an old in-memory list cannot erase words
        saved by another copy. Repair requires an exact checkpoint before commit.
        """
        incoming = self.read_saved_words_file(source)
        if not incoming:
            raise ValueError("Вибраний файл не містить збережених слів.")
        path = Path(self._saved_words_path())
        damaged = False
        try:
            current, revision = self._read_saved_file(path)
        except FileNotFoundError:
            current, revision = [], None
        except UnsupportedSchema:
            raise  # A newer library is not a corrupt legacy file to overwrite.
        except ValueError:
            current, revision, damaged = [], self._file_revision(path), True
        merged = copy.deepcopy(current)
        known = {entry["word"].casefold() for entry in current}
        added = 0
        for entry in incoming:
            key = entry["word"].casefold()
            if key not in known:
                merged.append(copy.deepcopy(entry))
                known.add(key)
                added += 1
        checkpoint = None
        if added or damaged:
            if revision is not None:
                checkpoint = self._preserve_before_restore(path)
            if self._file_revision(path) != revision:
                raise OSError("Бібліотеку змінила інша копія програми. Закрий її та повтори відновлення.")
            self.settings.writer(str(path), encode_words(merged))
        self._adopt_saved_words(merged, self.words)
        self._remember_saved_revision(path)
        return {"added": added, "total": len(merged), "checkpoint": str(checkpoint) if checkpoint else ""}


    def _saved_backups(self):
        folder = Path(self._saved_words_path()).with_suffix(".backups")
        return sorted(folder.glob("saved_words-*.json"), reverse=True)


    def _backup_image_names(self):
        names = set()
        for backup in self._saved_backups():
            try:
                with backup.open(encoding="utf-8") as handle:
                    entries = decode_words(json.load(handle))
                names.update(e["image_file"] for e in entries if isinstance(e, dict)
                             and self.context_image_path(e.get("image_file")))
            except (OSError, ValueError, TypeError):
                # Keep images if a backup cannot be inspected; a later save can retry.
                self.logger.exception("word_lookup_storage.backup_images")
                return None
        return names


    @_saved_transaction
    def context_image_in_use(self, name):
        """A retained history version also owns its screenshot."""
        if not self.context_image_path(name):
            return False
        if any(e.get("image_file") == name for e in self.load_saved_words()):
            return True
        backups = self._backup_image_names()
        return backups is None or name in backups


    def _prune_saved_backups(self):
        expired_images = set()
        for old in self._saved_backups()[_SAVED_BACKUPS:]:
            try:
                with old.open(encoding="utf-8") as handle:
                    expired_images.update(e["image_file"] for e in decode_words(json.load(handle))
                                          if isinstance(e, dict)
                                          and self.context_image_path(e.get("image_file")))
            except (OSError, ValueError, TypeError):
                self.logger.exception("word_lookup_storage.expired_backup_images")
            old.unlink()
        if not expired_images:
            return  # Most saves have no expired images; avoid rereading five libraries.
        retained_images = self._backup_image_names()
        if retained_images is None:
            return
        retained_images.update(e["image_file"] for e in self.words or []
                               if self.context_image_path(e.get("image_file")))
        for name in expired_images - retained_images:
            path = self.context_image_path(name)
            if path:
                Path(path).unlink(missing_ok=True)


    def _write_saved_words(self) -> bool:
        """Атомарно зберігає список і п'ять попередніх версій для відновлення.

        Резервну копію робимо до зміни основного файлу. Невдалий запис не
        знищує попередні версії й повертає False, щоб відкотити зміни в пам'яті.
        """
        with self.lock:
            snapshot = None
            try:
                path = Path(self._saved_words_path())
                state = self.state
                if state.get("path") == str(path.absolute()) and state.get("loaded"):
                    if state.get("error"):
                        self.logger.write("Список слів не записано: спочатку перечитай або віднови бібліотеку.")
                        return False
                    if self._file_revision(path) != state.get("revision"):
                        state["error"] = ("Файл бібліотеки змінився поза цим вікном. "
                                          "Натисни «Оновити», щоб не втратити нові слова.")
                        self.logger.write("Запис застарілої бібліотеки заблоковано; потрібне перечитування.")
                        return False
                if path.exists():
                    previous, _revision = self._read_saved_file(path)
                    folder = path.with_suffix(".backups")
                    snapshot = folder / (f"saved_words-{time.time_ns():020d}-{uuid.uuid4().hex}.json")
                    self.settings.writer(str(snapshot), encode_words(previous))
                self.settings.writer(str(path), encode_words(self.words or []))
            except (OSError, ValueError) as e:
                if snapshot is not None:
                    try:
                        snapshot.unlink(missing_ok=True)
                    except OSError:
                        self.logger.exception("word_lookup_storage.rollback_backup")
                self.logger.write(f"Не вдалося зберегти список слів: {e}")
                return False
            self._remember_saved_revision(path)
            # The current file is committed. A cleanup error must not roll back memory.
            try:
                self._prune_saved_backups()
            except OSError:
                self.logger.exception("word_lookup_storage.prune_backups")
            return True


    @_saved_transaction
    def saved_entry(self, word: str):
        if not isinstance(word, str) or not word.strip():
            return None
        for e in self.load_saved_words():
            if e.get("word", "").lower() == word.lower():
                return e
        return None


    def _entry_complete(self, e: dict) -> bool:
        return bool(e.get("translation") or e.get("definition"))


    def is_word_saved(self, word: str) -> bool:
        return self.saved_entry(word) is not None


    def _card_example(self, card: dict) -> str:
        """Перший приклад речення з уже наявних визначень картки, якщо є."""
        for m in card.get("meanings", []):
            for d in m.get("defs", []):
                if d.get("e"):
                    return d["e"]
        return ""


    def _card_definition(self, card: dict) -> str:
        """Перше визначення (англійською) з уже наявного словника картки, якщо є."""
        for m in card.get("meanings", []):
            for d in m.get("defs", []):
                if d.get("d"):
                    pos = m.get("pos", "")
                    return f"({pos}) {d['d']}" if pos else d["d"]
        return ""


    def _clean_sentence(self, text: str, limit: int = 300) -> str:
        return " ".join((text or "").split())[:limit]


    def _anki_tag(self, text: str) -> str:
        """Тег Anki не може містити пробілів: «Persona 5 Royal» -> «Persona_5_Royal»."""
        return re.sub(r"[^\w\-]+", "_", (text or "").strip(), flags=re.U).strip("_")[:40]


    def _common_prefix(self, a: str, b: str) -> int:
        n = 0
        for x, y in zip(a, b):
            if x != y:
                break
            n += 1
        return n


    def align_word_span(self, translation: str, terms):
        """Де в перекладеному реченні стоїть переклад слова — його підсвічуємо (як слово в
        Translator). Порівняння за спільним початком слова, щоб ловити відмінки/дієвідміни
        («опора» ~ «опору»). Терміни йдуть за пріоритетом: головний переклад першим.
        Повертає (початок, кінець) або None."""
        words = [(m.start(), m.end(), m.group().lower())
                 for m in re.finditer(r"\w+", translation or "", flags=re.U)]
        for term in terms:
            best = None
            for t in re.findall(r"\w+", (term or "").lower(), flags=re.U):
                if len(t) < 3:
                    continue
                for st, en, w in words:
                    cp = self._common_prefix(t, w)
                    shortest = min(len(t), len(w))
                    if (cp == len(t) == len(w)) or (cp >= 4 and cp >= 0.55 * shortest):
                        if best is None or cp > best[0]:
                            best = (cp, st, en)
            if best:
                return best[1], best[2]
        return None


    def split_around_word(self, sentence: str, word: str) -> list:
        """[(текст, чи_виділяти), ...] — речення, розрізане навколо слова."""
        m = re.search(rf"\b{re.escape(word)}\b", sentence or "", flags=re.I)
        if not m:
            return [(sentence, False)] if sentence else []
        parts = [(sentence[:m.start()], False), (m.group(), True), (sentence[m.end():], False)]
        return [(t, h) for t, h in parts if t]


    def _first_definition_text(self, card: dict) -> str:
        for m in card.get("meanings", []):
            for d in m.get("defs", []):
                if d.get("d"):
                    return d["d"]
        return ""


    def _card_primary_translation(self, card: dict) -> str:
        if card.get("translation"):
            return card["translation"]
        groups = card.get("groups")
        return groups[0][1][0] if groups else ""


    def primary_translations(self, card: dict, limit: int = 4) -> list:
        """Кілька варіантів перекладу для головного рядка картки — не один здогад Google,
        а перші кілька термінів зі словникової групи (зазвичай повніші й точніші, напр.
        містять і «сундук», і «груди» для "chest"), плюс сам переклад Google, якщо він
        ще не входить у цей список. Без дублікатів, порядок зберігається."""
        seen, out = set(), []
        groups = card.get("groups") or []
        for term in (groups[0][1] if groups else []):
            low = term.lower()
            if low not in seen:
                seen.add(low)
                out.append(term)
        translation = card.get("translation") or ""
        if translation:  # контекстний переклад — завжди першим, без дубліката у списку
            out = [t for t in out if t.lower() != translation.lower()]
            out.insert(0, translation)
        return out[:max(1, limit)]


    @_saved_transaction
    def add_saved_word(self, card: dict) -> bool:
        """Додає слово зі словникової картки у список. Повертає False, якщо слово
        вже було збережене раніше (без дублікатів)."""
        if not isinstance(card, dict) or not isinstance(card.get("word"), str) or not card["word"].strip():
            return False
        card = dict(card, word=card["word"].strip())
        words = self.load_saved_words()
        existing = self.saved_entry(card["word"])
        if existing is not None:
            previous = copy.deepcopy(existing)
            # Слово могли зберегти ще під час довантаження визначень/речення.
            updates = {"translation": self._card_primary_translation(card), "definition": self._card_definition(card),
                       "base": card.get("base"), "example": self._card_example(card),
                       "context": self._clean_sentence(card.get("context", "")), "source": card.get("source"),
                       "definition_tr": card.get("definition_tr"), "context_tr": card.get("sentence_tr"),
                       "anki_tag": card.get("anki_tag")}
            changed = False
            for key, value in updates.items():
                if value and not existing.get(key) and key not in existing.get("edited_fields", []):
                    existing[key] = value
                    changed = True
            details = existing.setdefault("dictionary", {})
            for key in ("phonetic", "audios", "meanings", "groups"):
                if card.get(key) and not details.get(key):
                    details[key] = copy.deepcopy(card[key])
                    changed = True
            if changed:
                if not self._write_saved_words():
                    existing.clear()
                    existing.update(previous)
                    return False
            return changed
        words.append({
            "word": card["word"],
            "kind": card.get("entry_kind", "word"),
            "translation": self._card_primary_translation(card),
            "translator": card.get("translator", ""),
            "base": card.get("base") or "",
            "example": self._card_example(card),
            "definition": self._card_definition(card),
            "context": self._clean_sentence(card.get("context", "")),
            "source": card.get("source", ""),
            "anki_tag": card.get("anki_tag", ""),
            "definition_tr": card.get("definition_tr", ""),
            "context_tr": card.get("sentence_tr", ""),
            "image_file": self.save_context_image(card.get("_context_image")),
            "dictionary": {k: copy.deepcopy(card[k]) for k in ("phonetic", "audios", "meanings", "groups")
                           if card.get(k)},
            "added": datetime.now().isoformat(timespec="seconds"),
        })
        if not self._write_saved_words():
            removed = words.pop()
            path = self.context_image_path(removed.get("image_file", ""))
            if path:
                try:
                    os.remove(path)
                except OSError:
                    self.logger.exception('word_lookup_storage.add_saved_word')
            return False
        return True


    @_saved_transaction
    def remove_saved_word(self, word: str) -> bool:
        if not isinstance(word, str) or not word.strip():
            return False
        words = self.load_saved_words()
        previous = list(words)
        removed = [e for e in words if e.get("word", "").lower() == word.lower()]
        before = len(words)
        words[:] = [e for e in words if e.get("word", "").lower() != word.lower()]
        if len(words) != before:
            if not self._write_saved_words():
                words[:] = previous
                return False
            for e in removed:
                name = e.get("image_file", "")
                path = self.context_image_path(name)
                if path and not self.context_image_in_use(name):
                    try:
                        os.remove(path)
                    except OSError:
                        self.logger.exception('word_lookup_storage.remove_saved_word')
            return True
        return False


    def filter_saved_words(self, entries, query="", order="newest"):
        """Пошук у слові, перекладі, прикладі, визначенні та джерелі без мережі."""
        terms = query.casefold().split()
        fields = ("word", "translation", "definition", "context", "example", "source", "notes")
        result = [e for e in entries if all(
            term in " ".join(str(e.get(k, "")) for k in fields).casefold() for term in terms)]
        if order == "az":
            return sorted(result, key=lambda e: e["word"].casefold())
        return sorted(result, key=lambda e: str(e.get("added", "")), reverse=order != "oldest")


    def saved_word_card(self, entry):
        """Збережені дані доступні відразу, навіть без інтернету чи після очищення кешу."""
        definition, example = entry.get("definition", ""), entry.get("example", "")
        if entry.get("kind") == "phrase":
            return {**self.text_card(entry["word"], entry.get("translation", "")),
                    "source": entry.get("source", ""), "anki_tag": entry.get("anki_tag", ""),
                    "image_file": entry.get("image_file", ""), "translator": entry.get("translator", "")}
        card = {"kind": "word", "word": entry["word"], "base": entry.get("base", ""),
                "translation": entry.get("translation", ""), "groups": [],
                "phonetic": "", "audios": [], "note": "", "dict_note": "",
                "meanings": [{"pos": "", "defs": [{"d": definition, "e": example}],
                              "syn": [], "ant": []}] if definition or example else [],
                "context": entry.get("context", ""), "sentence": entry.get("context", ""),
                "sentence_tr": entry.get("context_tr", ""),
                "definition_tr": entry.get("definition_tr", ""),
                "source": entry.get("source", ""), "anki_tag": entry.get("anki_tag", ""),
                "image_file": entry.get("image_file", ""), "translator": entry.get("translator", "")}
        details = entry.get("dictionary", {})
        if isinstance(details, dict):
            for key in ("phonetic", "audios", "meanings", "groups"):
                if details.get(key):
                    card[key] = copy.deepcopy(details[key])
        if "definition" in entry.get("edited_fields", []) or "example" in entry.get("edited_fields", []):
            if not card["meanings"]:
                card["meanings"] = [{"pos": "", "defs": [], "syn": [], "ant": []}]
            first = card["meanings"][0]
            if not first["defs"]:
                first["defs"] = [{"d": "", "e": ""}]
            first["defs"][0].update(d=definition, e=example)
        return card


    @_saved_transaction
    def update_saved_entry(self, word, fields):
        entry = self.saved_entry(word)
        if entry is None:
            raise ValueError("Слово вже видалене з бібліотеки")
        old = copy.deepcopy(entry)
        edited = set(entry.get("edited_fields", []))
        for key, value in fields.items():
            if key in self.edit_fields and isinstance(value, str):
                if value.strip() != entry.get(key, ""):
                    edited.add(key)
                entry[key] = value.strip()
            elif key == "status" and value in ("new", "learning", "known"):
                entry[key] = value
            elif key == "image_file" and (not value or self.context_image_path(value)):
                entry[key] = value
        entry["edited_fields"] = sorted(edited)
        if not self._write_saved_words():
            entry.clear()
            entry.update(old)
            raise OSError("Не вдалося записати зміни. Попередні дані збережено в пам'яті.")
        return entry


    @_saved_transaction
    def merge_saved_enrichments(self, results):
        """Commit completed copy-based lookups without replacing user edits or deletions."""
        words = self.load_saved_words()
        saved_ids = {id(entry) for entry in words}
        changed = []
        persist = False
        for target, before, enriched in results:
            updates = {key: enriched[key] for key in ("translation", "definition", "example", "base", "enriched")
                       if key in enriched and enriched[key] != before.get(key)
                       and target.get(key) == before.get(key)
                       and key not in (target.get("edited_fields") or [])}
            if updates:
                changed.append((target, copy.deepcopy(target)))
                target.update(updates)
                persist = persist or id(target) in saved_ids
        if persist and not self._write_saved_words():
            for target, previous in changed:
                target.clear()
                target.update(previous)
            raise OSError("Не вдалося зберегти дозаповнення слів; попередні дані відновлено")
        return len(changed)


    def review_due(self, entry, now=None):
        if entry.get("status") == "known":
            return False
        now = now or datetime.now(timezone.utc)
        review = entry.get("review")
        due = review.get("due") if isinstance(review, dict) else None
        if not due:
            return True
        try:
            dt = datetime.fromisoformat(due)
            return dt.replace(tzinfo=dt.tzinfo or timezone.utc) <= now
        except (ValueError, TypeError):
            return True


    @_saved_transaction
    def record_review(self, entry, known, now=None):
        """Простий розклад 1, 3, 7, 14, 30 днів; помилка — повторити через 10 хв."""
        now = now or datetime.now(timezone.utc)
        old = copy.deepcopy(entry)
        stats = entry.get("review")
        if not isinstance(stats, dict):
            stats = entry["review"] = {}
        streak = int(clamp(stats.get("streak", 0), 0, 1000)) + 1 if known else 0
        interval = (1, 3, 7, 14, 30)[min(streak - 1, 4)] if known else 0
        stats.update(streak=streak, attempts=int(clamp(stats.get("attempts", 0), 0, 1000000)) + 1,
                     last=now.isoformat(), due=(now + (timedelta(days=interval) if known else
                                                     timedelta(minutes=10))).isoformat())
        entry["status"] = "learning"
        if not self._write_saved_words():
            entry.clear()
            entry.update(old)
            raise OSError("Прогрес не записано на диск")
        return stats


    def context_image_path(self, name):
        if not isinstance(name, str) or not re.fullmatch(r"context-[a-f0-9]{24}\.jpg", name):
            return None
        return os.path.join(os.path.dirname(self.settings.path()), "media", name)


    def save_context_image(self, image):
        if image is None:
            return ""
        name = "context-" + hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:24] + ".jpg"
        path = self.context_image_path(name)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            image = image.copy().convert("RGB")
            image.thumbnail((960, 480))
            image.save(path, "JPEG", quality=82)
            return name
        except (OSError, ValueError) as e:
            self.logger.write(f"Не вдалося зберегти зображення контексту: {e}")
            return ""


    def context_photo(self, name, master, size=(560, 260)):
        path = self.context_image_path(name)
        if not path or not os.path.isfile(path):
            return None
        try:
            from PIL import Image, ImageTk
            with Image.open(path) as image:
                image.thumbnail(size)
                return ImageTk.PhotoImage(image.copy(), master=master)
        except (OSError, ValueError):
            return None

