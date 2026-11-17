"""Concurrent word, sentence and definition stages for an OCR lookup.

Every queued card is a complete snapshot. The application provides explicit
settings, cache, library, OCR and translation services.
"""
from word_lookup_models import as_event
from functools import wraps
import threading
import time
from wordlookup.network.budget import LookupCancelled, translation_scope, network_job, scoped_task


def _language_scoped(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        with translation_scope(self.language):
            return method(self, *args, **kwargs)
    return run


class LookupJob:
    def __init__(self, app, mode, x, y, token, hit, frozen, source, context_image):
        self.app, self.mode, self.token = app, mode, token
        self.x, self.y, self.hit, self.frozen = x, y, hit, frozen
        self.source, self.context_image = source, context_image
        self.language = self.app.providers._target_language()
        with self.app.cache.lock:
            self.cache_generation = self.app.cache.generation
        self.started = time.time()
        self.shown_partial = False
        self.reused_sentence = ""
        self.context = self.app.library._clean_sentence(hit.get("line_text", ""))
        if self.context.lower() == hit["word"].lower():
            self.context = ""
        popup = self.app.settings.data["popup"]
        self.has_sentence = bool(popup["sentence"] or popup["sentence_translation"])
        self.short_context = 2 <= len(self.context.split()) <= 40
        self.state = {
            "context": self.context, "source": source, "_context_image": context_image,
            "sentence": self.context if self.has_sentence and self.short_context else "",
            "sentence_pending": self.has_sentence,
        }
        self.lock = threading.Lock()

    def stale(self):
        return self.token != self.app.active_token

    def emit(self, kind, changes):
        """Merge and enqueue under one lock so a later snapshot cannot overtake it."""
        with self.lock:
            changed = any(key not in self.state or
                          (self.state[key] is not value and self.state[key] != value)
                          for key, value in changes.items())
            self.state.update(changes)
            card = dict(self.state)
            if not self.stale() and (kind == "show" or changed):
                if kind == "show":
                    self.app.events.put(as_event(("show", card, self.hit, None, self.token)))
                else:
                    self.app.events.put(as_event(("update", self.token, self.hit["word"], card)))
            return card

    @_language_scoped
    def sentence(self):
        """Find wider context and translate it independently of the word card."""
        if self.stale():
            return
        sent = self.reused_sentence
        try:
            if not sent:
                # Full-frame selections carry their recognized words in hit;
                # find_sentence can assemble those lines without another OCR.
                sent = self.app.ocr.find_sentence(self.hit, self.frozen)
        except LookupCancelled:
            raise
        except Exception as error:
            self.app.logger.write(f"Пошук речення: {error}")
        if self.stale():
            return
        if not sent and self.short_context:
            sent = self.context
        self.app.logger.write(f"Речення: {len(sent.split())} слів" if sent else "Речення: не знайдено")
        if sent:
            self.emit("update", {"sentence": sent, "context": sent})
        translation = ""
        if sent and 2 <= len(sent.split()) <= 60 and self.app.settings.data["popup"]["sentence_translation"]:
            try:
                translation = self.app.providers.translate_text(self.app.providers.clean_hud_line(sent))
            except LookupCancelled:
                raise
            except Exception as error:
                self.app.logger.write(f"Переклад речення: {error}")
        self.emit("update", {"sentence": sent, "context": sent or self.context,
                             "sentence_tr": translation, "sentence_pending": False})

    def partial(self, card):
        if self.stale():
            return
        self.app.logger.write(f"Переклад: {time.time() - self.started:.2f} с (словник ще довантажується)")
        self.emit("show", card)
        self.shown_partial = True

    @_language_scoped
    def complete(self, card, showed_partial):
        """Publish definitions, then enrich the cache with their translation."""
        if self.stale():
            return
        definition = self.app.library._first_definition_text(card)
        need_translation = bool(definition) and not card.get("definition_tr") \
            and self.app.settings.data["sections"]["definitions"] and self.app.settings.data["popup"]["translate_definition"]
        self.emit("update" if showed_partial else "show", {**card, "def_tr_pending": need_translation})
        if not need_translation or self.stale():
            return
        try:
            translation = self.app.providers.translate_text(definition)
        except LookupCancelled:
            raise
        except Exception as error:
            self.app.logger.write(f"Переклад визначення: {error}")
            translation = ""
        if self.stale():
            return
        if translation:
            card["definition_tr"] = translation
            if not card.get("note") and not card.get("dict_note"):
                key = self.app.cache._cache_key(self.hit["word"], self.hit.get("line_text", ""))
                self.app.cache._cache_word_card(key, card, expected_generation=self.cache_generation)
        self.emit("update", {"definition_tr": translation, "def_tr_pending": False})

    @_language_scoped
    def line(self):
        text = self.hit["line_text"]
        card = self.app.providers.text_card(text, self.app.providers.translate_text(self.app.providers.clean_hud_line(text)))
        card.update(source=self.source, _context_image=self.context_image)
        if not self.stale():
            self.app.events.put(as_event(("show", card, None, (self.x, self.y, self.x, self.y), self.token)))

    def cached_card(self):
        key = self.app.cache._cache_key(self.hit["word"], self.hit.get("line_text", ""))
        card = self.app.cache._cached_card_for_lookup(key)
        if card is None:
            return None
        card["word"] = self.hit["word"]
        # Cache entries normally contain dictionary data only. Old entries may
        # also contain context: reuse it solely for the exact currently OCR'd
        # line, never for another scene or game that happens to use this word.
        context = self.app.library._clean_sentence(card.get("context") or card.get("sentence", ""))
        if self.has_sentence and self.context and context == self.context and card.get("sentence"):
            self.reused_sentence = card["sentence"]
            self.state.update(sentence=card["sentence"], context=context,
                              sentence_tr=card.get("sentence_tr", ""))
            if card.get("sentence_tr") or not self.app.settings.data["popup"]["sentence_translation"]:
                self.state["sentence_pending"] = False
        for field in ("context", "sentence", "sentence_tr", "sentence_pending", "source",
                      "_context_image", "image_file", "anki_tag"):
            card.pop(field, None)
        return card

    @_language_scoped
    def word(self):
        if self.stale():
            return
        card = self.cached_card()
        if self.has_sentence and self.state["sentence_pending"]:
            target = network_job(self.stale)(scoped_task(self.sentence))
            threading.Thread(target=target, daemon=True).start()
        showed_partial = False
        if card is None:
            card, showed_partial = self.app.providers.build_word_card_fast(
                self.hit["word"], self.partial, context=self.hit.get("line_text", ""))
        self.app.logger.write(f"Словник/переклад: {time.time() - self.started:.2f} с")
        if not self.stale():
            self.complete(card, showed_partial)

    def run(self):
        if self.mode == "word" and self.app.settings.data["ocr"]["highlight"]:
            self.app.events.put(as_event(("hl", self.hit, self.token)))
        try:
            self.line() if self.mode == "line" else self.word()
        except Exception as error:
            if not self.stale():
                if self.shown_partial:
                    self.emit("update", {"dict_pending": False, "note": self.state.get("note") or str(error)})
                else:
                    self.app.post_lookup(self.token, "toast", f"Помилка перекладу: {error}")


def run_lookup_result(app, mode, x, y, token, hit, frozen, source, context_image):
    LookupJob(app, mode, x, y, token, hit, frozen, source, context_image).run()
