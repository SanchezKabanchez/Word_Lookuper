"""Bounded card cache, invalidation and versioned persistence."""
import copy
import hashlib
import json
import os
import threading
from word_lookup_schemas import decode_cards, encode_cards
from wordlookup.network.policy import network_allowed
from word_lookup_utils import clamp
from wordlookup.network.budget import current_translation_language


class CardCache:
    def __init__(self, settings, logger, text_cache):
        self.settings, self.logger, self.text_cache = settings, logger, text_cache
        self.cards = {}
        self.file_state = {}
        self.lock = threading.RLock()
        self.generation = 0
        self.new_count = 0

    def _target_language(self):
        return current_translation_language() or self.settings.data["languages"]["target"]

    def _max_tr(self):
        return int(clamp(self.settings.data["dictionary"]["max_translations"],1,20))

    def _deepl_configured(self):
        values=self.settings.data["deepl"]
        return bool(values["enabled"] and values["key"].strip())

    def network_allowed(self, provider):
        return network_allowed(self.settings.data,provider)

    def _cache_path(self) -> str:
        return os.path.join(os.path.dirname(self.settings.path()), "cache.json")


    def load_card_cache(self) -> None:
        self.file_state.clear()
        self.file_state.update(path=self._cache_path(), writable=True)
        try:
            with open(self._cache_path(), encoding="utf-8-sig") as f:
                data = decode_cards(json.load(f))
            if isinstance(data, dict):
                limit = int(clamp(self.settings.data["cache"]["max_entries"], 0, 5000))
                valid = [(k, v) for k, v in data.items()
                         if isinstance(v, dict) and v.get("kind") == "word"]
                with self.lock:
                    self.cards.clear()
                    if limit:
                        self.cards.update(valid[-limit:])
        except FileNotFoundError:
            return  # a new installation has no disk cache yet
        except (OSError, ValueError):
            self.file_state["writable"] = False
            self.logger.exception('word_lookup_storage.load_card_cache')


    def cache_disk_kb(self) -> float:
        try:
            return os.path.getsize(self._cache_path()) / 1024
        except OSError:
            return 0.0


    def save_card_cache(self) -> None:
        if (self.file_state.get("path") == self._cache_path()
                and not self.file_state.get("writable", True)):
            return
        limit = int(clamp(self.settings.data["cache"]["max_entries"], 0, 5000))
        with self.lock:
            try:
                if limit == 0:
                    data = {}
                else:
                    data = dict(list(self.cards.items())[-limit:])
                self.settings.writer(self._cache_path(), encode_cards(data), indent=None)
            except (OSError, ValueError):
                self.logger.exception('word_lookup_storage.save_card_cache')


    def clear_card_cache(self) -> None:
        self.file_state.clear()
        self.text_cache.clear(os.path.join(os.path.dirname(self.settings.path()), "translations.json"))
        with self.lock:
            self.generation += 1
            self.cards.clear()
            try:
                os.remove(self._cache_path())
            except FileNotFoundError:
                return
            except OSError:
                self.logger.exception('word_lookup_storage.clear_card_cache')


    def cached_word_card(self, key):
        """Картка кожного пошуку незалежна: контекст не протікає в наступний запит."""
        if int(clamp(self.settings.data["cache"]["max_entries"], 0, 5000)) == 0:
            return None
        with self.lock:
            card = self.cards.pop(key, None)
            if card is not None:
                self.cards[key] = card
                return copy.deepcopy(card)
        return None


    def _clean_context(self, word: str, context: str) -> str:
        ctx = " ".join((context or "").split())[:300]
        return "" if ctx.lower() == word.lower() else ctx


    def _cache_key(self, word: str, context: str = "") -> str:
        key = f"{word.lower()}|{self._target_language()}|{self._max_tr()}"
        if self._deepl_configured():  # переклад залежить від контексту -> окремі записи кешу
            key += "|dl"
            ctx = self._clean_context(word, context)
            if ctx:
                key += "|" + hashlib.md5(ctx.lower().encode("utf-8")).hexdigest()[:8]
        return key


    def _cache_word_card(self, key: str, card: dict, *, expected_generation=None) -> None:
        """Кладе повну (без note/dict_note) картку в кеш і час від часу зберігає на диск."""
        limit = int(clamp(self.settings.data["cache"]["max_entries"], 0, 5000))
        if limit == 0:
            return  # кешування вимкнено
        with self.lock:
            if expected_generation is not None and expected_generation != self.generation:
                return
            self.cards.pop(key, None)
            self.cards[key] = copy.deepcopy(card)
            while len(self.cards) > limit:
                del self.cards[next(iter(self.cards))]
            self.new_count += 1
            if self.new_count % 5 == 0:
                self.save_card_cache()


    def _dictionary_required(self) -> bool:
        sections, popup = self.settings.data["sections"], self.settings.data["popup"]
        return bool(sections["definitions"] or sections["thesaurus"]
                    or popup["phonetic"] or popup["btn_speak"])


    def _dictionary_fetch_enabled(self) -> bool:
        return self._dictionary_required() and (self.network_allowed("dictionaryapi") or self.network_allowed("datamuse"))


    def _dictionary_sources(self):
        return [provider for provider in ("dictionaryapi", "datamuse") if self.network_allowed(provider)]


    def _cached_card_for_lookup(self, key):
        card = self.cached_word_card(key)
        # A translation-only lookup is useful in the cache, but newly enabled
        # dictionary sections must still be allowed to load their missing content.
        if card is not None and self._dictionary_fetch_enabled():
            if not card.get("_dictionary_complete", True):
                return None
            previous_sources = card.get("_dictionary_sources")
            if previous_sources is not None and any(source not in previous_sources for source in self._dictionary_sources()):
                return None
        return card

