"""Persistent cards, review scheduling, media and bounded card cache.

The runtime supplies configuration, shared state and overridable service adapters.
The entry point reexports this API for existing integrations.
"""
from __future__ import annotations

runtime = None


def configure(application):
    global runtime
    runtime = application


def _cache_path() -> str:
    return runtime.os.path.join(runtime.os.path.dirname(runtime.config_path()), "cache.json")


def load_card_cache() -> None:
    try:
        with open(runtime._cache_path(), encoding="utf-8") as f:
            data = runtime.json.load(f)
        if isinstance(data, dict):
            limit = int(runtime.clamp(runtime.cfg["cache"]["max_entries"], 0, 5000))
            valid = [(k, v) for k, v in data.items()
                     if isinstance(v, dict) and v.get("kind") == "word"]
            with runtime._cache_lock:
                runtime._card_cache.clear()
                if limit:
                    runtime._card_cache.update(valid[-limit:])
    except (OSError, ValueError):
        runtime.log_exc('word_lookup_storage.load_card_cache')


def cache_disk_kb() -> float:
    try:
        return runtime.os.path.getsize(runtime._cache_path()) / 1024
    except OSError:
        return 0.0


def save_card_cache() -> None:
    limit = int(runtime.clamp(runtime.cfg["cache"]["max_entries"], 0, 5000))
    with runtime._cache_lock:
        try:
            if limit == 0:
                data = {}
            else:
                data = dict(list(runtime._card_cache.items())[-limit:])
            runtime.atomic_json_write(runtime._cache_path(), data, indent=None)
        except OSError:
            runtime.log_exc('word_lookup_storage.save_card_cache')


def clear_card_cache() -> None:
    runtime.TEXT_CACHE.clear(runtime.os.path.join(runtime.os.path.dirname(runtime.config_path()), "translations.json"))
    with runtime._cache_lock:
        runtime._card_cache.clear()
        try:
            runtime.os.remove(runtime._cache_path())
        except OSError:
            runtime.log_exc('word_lookup_storage.clear_card_cache')


def cached_word_card(key):
    """Картка кожного пошуку незалежна: контекст не протікає в наступний запит."""
    if int(runtime.clamp(runtime.cfg["cache"]["max_entries"], 0, 5000)) == 0:
        return None
    with runtime._cache_lock:
        card = runtime._card_cache.pop(key, None)
        if card is not None:
            runtime._card_cache[key] = card
            return runtime.copy.deepcopy(card)
    return None


def _saved_words_path() -> str:
    return runtime.os.path.join(runtime.os.path.dirname(runtime.config_path()), "saved_words.json")


def load_saved_words() -> list:
    """Список збережених слів. Читається з диска лише один раз за сеанс."""
    if runtime._saved_words is not None:
        return runtime._saved_words
    try:
        with open(runtime._saved_words_path(), encoding="utf-8") as f:
            data = runtime.json.load(f)
        runtime._saved_words = [e for e in data if isinstance(e, dict)
                        and isinstance(e.get("word"), str) and e["word"].strip()] \
            if isinstance(data, list) else []
    except (OSError, ValueError):
        runtime._saved_words = []
    return runtime._saved_words


def _write_saved_words() -> None:
    """Безпечний запис: спершу у тимчасовий файл, потім атомарна заміна —
    щоб раптове закриття програми не пошкодило список."""
    try:
        path = runtime._saved_words_path()
        runtime.atomic_json_write(path, runtime._saved_words or [])
        return True
    except OSError as e:
        runtime.log(f"Не вдалося зберегти список слів: {e}")
        return False


def saved_entry(word: str):
    for e in runtime.load_saved_words():
        if e.get("word", "").lower() == word.lower():
            return e
    return None


def _entry_complete(e: dict) -> bool:
    return bool(e.get("translation") or e.get("definition"))


def is_word_saved(word: str) -> bool:
    return any(e.get("word", "").lower() == word.lower() for e in runtime.load_saved_words())


def _card_example(card: dict) -> str:
    """Перший приклад речення з уже наявних визначень картки, якщо є."""
    for m in card.get("meanings", []):
        for d in m.get("defs", []):
            if d.get("e"):
                return d["e"]
    return ""


def _card_definition(card: dict) -> str:
    """Перше визначення (англійською) з уже наявного словника картки, якщо є."""
    for m in card.get("meanings", []):
        for d in m.get("defs", []):
            if d.get("d"):
                pos = m.get("pos", "")
                return f"({pos}) {d['d']}" if pos else d["d"]
    return ""


def _clean_sentence(text: str, limit: int = 300) -> str:
    return " ".join((text or "").split())[:limit]


def _anki_tag(text: str) -> str:
    """Тег Anki не може містити пробілів: «Persona 5 Royal» -> «Persona_5_Royal»."""
    return runtime.re.sub(r"[^\w\-]+", "_", (text or "").strip(), flags=runtime.re.U).strip("_")[:40]


def _common_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def align_word_span(translation: str, terms):
    """Де в перекладеному реченні стоїть переклад слова — його підсвічуємо (як слово в
    Translator). Порівняння за спільним початком слова, щоб ловити відмінки/дієвідміни
    («опора» ~ «опору»). Терміни йдуть за пріоритетом: головний переклад першим.
    Повертає (початок, кінець) або None."""
    words = [(m.start(), m.end(), m.group().lower())
             for m in runtime.re.finditer(r"\w+", translation or "", flags=runtime.re.U)]
    for term in terms:
        best = None
        for t in runtime.re.findall(r"\w+", (term or "").lower(), flags=runtime.re.U):
            if len(t) < 3:
                continue
            for st, en, w in words:
                cp = runtime._common_prefix(t, w)
                shortest = min(len(t), len(w))
                if (cp == len(t) == len(w)) or (cp >= 4 and cp >= 0.55 * shortest):
                    if best is None or cp > best[0]:
                        best = (cp, st, en)
        if best:
            return best[1], best[2]
    return None


def split_around_word(sentence: str, word: str) -> list:
    """[(текст, чи_виділяти), ...] — речення, розрізане навколо слова."""
    m = runtime.re.search(rf"\b{runtime.re.escape(word)}\b", sentence or "", flags=runtime.re.I)
    if not m:
        return [(sentence, False)] if sentence else []
    parts = [(sentence[:m.start()], False), (m.group(), True), (sentence[m.end():], False)]
    return [(t, h) for t, h in parts if t]


def _first_definition_text(card: dict) -> str:
    for m in card.get("meanings", []):
        for d in m.get("defs", []):
            if d.get("d"):
                return d["d"]
    return ""


def _card_primary_translation(card: dict) -> str:
    if card.get("translation"):
        return card["translation"]
    groups = card.get("groups")
    return groups[0][1][0] if groups else ""


def primary_translations(card: dict, limit: int = 4) -> list:
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


def add_saved_word(card: dict) -> bool:
    """Додає слово зі словникової картки у список. Повертає False, якщо слово
    вже було збережене раніше (без дублікатів)."""
    words = runtime.load_saved_words()
    existing = runtime.saved_entry(card["word"])
    if existing is not None:
        previous = runtime.copy.deepcopy(existing)
        # Слово могли зберегти ще під час довантаження визначень/речення.
        updates = {"translation": runtime._card_primary_translation(card), "definition": runtime._card_definition(card),
                   "base": card.get("base"), "example": runtime._card_example(card),
                   "context": runtime._clean_sentence(card.get("context", "")), "source": card.get("source"),
                   "definition_tr": card.get("definition_tr"), "context_tr": card.get("sentence_tr")}
        changed = False
        for key, value in updates.items():
            if value and not existing.get(key) and key not in existing.get("edited_fields", []):
                existing[key] = value
                changed = True
        details = existing.setdefault("dictionary", {})
        for key in ("phonetic", "audios", "meanings", "groups"):
            if card.get(key) and not details.get(key):
                details[key] = runtime.copy.deepcopy(card[key])
                changed = True
        if changed:
            if not runtime._write_saved_words():
                existing.clear()
                existing.update(previous)
                return False
        return changed
    words.append({
        "word": card["word"],
        "kind": card.get("entry_kind", "word"),
        "translation": runtime._card_primary_translation(card),
        "base": card.get("base") or "",
        "example": runtime._card_example(card),
        "definition": runtime._card_definition(card),
        "context": runtime._clean_sentence(card.get("context", "")),
        "source": card.get("source", ""),
        "definition_tr": card.get("definition_tr", ""),
        "context_tr": card.get("sentence_tr", ""),
        "image_file": runtime.save_context_image(card.get("_context_image")),
        "dictionary": {k: runtime.copy.deepcopy(card[k]) for k in ("phonetic", "audios", "meanings", "groups")
                       if card.get(k)},
        "added": runtime.datetime.now().isoformat(timespec="seconds"),
    })
    if not runtime._write_saved_words():
        removed = words.pop()
        path = runtime.context_image_path(removed.get("image_file", ""))
        if path:
            try:
                runtime.os.remove(path)
            except OSError:
                runtime.log_exc('word_lookup_storage.add_saved_word')
        return False
    return True


def remove_saved_word(word: str) -> bool:
    words = runtime.load_saved_words()
    previous = list(words)
    removed = [e for e in words if e.get("word", "").lower() == word.lower()]
    before = len(words)
    words[:] = [e for e in words if e.get("word", "").lower() != word.lower()]
    if len(words) != before:
        if not runtime._write_saved_words():
            words[:] = previous
            return False
        for e in removed:
            name = e.get("image_file", "")
            path = runtime.context_image_path(name)
            if path and not any(w.get("image_file") == name for w in words):
                try:
                    runtime.os.remove(path)
                except OSError:
                    runtime.log_exc('word_lookup_storage.remove_saved_word')
        return True
    return False


def filter_saved_words(entries, query="", order="newest"):
    """Пошук у слові, перекладі, прикладі, визначенні та джерелі без мережі."""
    terms = query.casefold().split()
    fields = ("word", "translation", "definition", "context", "example", "source", "notes")
    result = [e for e in entries if all(
        term in " ".join(str(e.get(k, "")) for k in fields).casefold() for term in terms)]
    if order == "az":
        return sorted(result, key=lambda e: e["word"].casefold())
    return sorted(result, key=lambda e: str(e.get("added", "")), reverse=order != "oldest")


def saved_word_card(entry):
    """Збережені дані доступні відразу, навіть без інтернету чи після очищення кешу."""
    definition, example = entry.get("definition", ""), entry.get("example", "")
    if entry.get("kind") == "phrase":
        return {**runtime.text_card(entry["word"], entry.get("translation", "")),
                "source": entry.get("source", ""), "image_file": entry.get("image_file", "")}
    card = {"kind": "word", "word": entry["word"], "base": entry.get("base", ""),
            "translation": entry.get("translation", ""), "groups": [],
            "phonetic": "", "audios": [], "note": "", "dict_note": "",
            "meanings": [{"pos": "", "defs": [{"d": definition, "e": example}],
                          "syn": [], "ant": []}] if definition or example else [],
            "context": entry.get("context", ""), "sentence": entry.get("context", ""),
            "sentence_tr": entry.get("context_tr", ""),
            "definition_tr": entry.get("definition_tr", ""),
            "source": entry.get("source", ""), "image_file": entry.get("image_file", "")}
    details = entry.get("dictionary", {})
    if isinstance(details, dict):
        for key in ("phonetic", "audios", "meanings", "groups"):
            if details.get(key):
                card[key] = runtime.copy.deepcopy(details[key])
    if "definition" in entry.get("edited_fields", []) or "example" in entry.get("edited_fields", []):
        if not card["meanings"]:
            card["meanings"] = [{"pos": "", "defs": [], "syn": [], "ant": []}]
        first = card["meanings"][0]
        if not first["defs"]:
            first["defs"] = [{"d": "", "e": ""}]
        first["defs"][0].update(d=definition, e=example)
    return card


def update_saved_entry(word, fields):
    entry = runtime.saved_entry(word)
    if entry is None:
        raise ValueError("Слово вже видалене з бібліотеки")
    old = runtime.copy.deepcopy(entry)
    edited = set(entry.get("edited_fields", []))
    for key, value in fields.items():
        if key in runtime.EDIT_FIELDS and isinstance(value, str):
            if value.strip() != entry.get(key, ""):
                edited.add(key)
            entry[key] = value.strip()
        elif key == "status" and value in ("new", "learning", "known"):
            entry[key] = value
        elif key == "image_file" and (not value or runtime.context_image_path(value)):
            entry[key] = value
    entry["edited_fields"] = sorted(edited)
    if not runtime._write_saved_words():
        entry.clear()
        entry.update(old)
        raise OSError("Не вдалося записати зміни. Попередні дані збережено в пам'яті.")
    return entry


def review_due(entry, now=None):
    if entry.get("status") == "known":
        return False
    now = now or runtime.datetime.now(runtime.timezone.utc)
    due = entry.get("review", {}).get("due")
    if not due:
        return True
    try:
        dt = runtime.datetime.fromisoformat(due)
        return dt.replace(tzinfo=dt.tzinfo or runtime.timezone.utc) <= now
    except (ValueError, TypeError):
        return True


def record_review(entry, known, now=None):
    """Простий розклад 1, 3, 7, 14, 30 днів; помилка — повторити через 10 хв."""
    now = now or runtime.datetime.now(runtime.timezone.utc)
    old = runtime.copy.deepcopy(entry)
    stats = entry.setdefault("review", {})
    streak = int(runtime.clamp(stats.get("streak", 0), 0, 1000)) + 1 if known else 0
    interval = (1, 3, 7, 14, 30)[min(streak - 1, 4)] if known else 0
    stats.update(streak=streak, attempts=int(runtime.clamp(stats.get("attempts", 0), 0, 1000000)) + 1,
                 last=now.isoformat(), due=(now + (runtime.timedelta(days=interval) if known else
                                                 runtime.timedelta(minutes=10))).isoformat())
    entry["status"] = "learning"
    if not runtime._write_saved_words():
        entry.clear()
        entry.update(old)
        raise OSError("Прогрес не записано на диск")
    return stats


def context_image_path(name):
    if not isinstance(name, str) or not runtime.re.fullmatch(r"context-[a-f0-9]{24}\.jpg", name):
        return None
    return runtime.os.path.join(runtime.os.path.dirname(runtime.config_path()), "media", name)


def save_context_image(image):
    if image is None:
        return ""
    name = "context-" + runtime.hashlib.sha256(str(runtime.time.time_ns()).encode()).hexdigest()[:24] + ".jpg"
    path = runtime.context_image_path(name)
    try:
        runtime.os.makedirs(runtime.os.path.dirname(path), exist_ok=True)
        image = image.copy().convert("RGB")
        image.thumbnail((960, 480))
        image.save(path, "JPEG", quality=82)
        return name
    except (OSError, ValueError) as e:
        runtime.log(f"Не вдалося зберегти зображення контексту: {e}")
        return ""


def context_photo(name, master, size=(560, 260)):
    path = runtime.context_image_path(name)
    if not path or not runtime.os.path.isfile(path):
        return None
    try:
        from PIL import Image, ImageTk
        with Image.open(path) as image:
            image.thumbnail(size)
            return ImageTk.PhotoImage(image.copy(), master=master)
    except (OSError, ValueError):
        return None
