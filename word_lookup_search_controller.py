from __future__ import annotations
import copy
import re
import sys
import threading
import time
import pyperclip
from word_lookup_theme import (AUDIO_OK, TRAY_OK)
from word_lookup_models import (as_event)
from word_lookup_profiles import profile_worker
from wordlookup.network.budget import cancellable_lookup
from wordlookup.network.budget import (network_job, network_scope)
from word_lookup_lexicon import manual_word_scope
from word_lookup_jobs import run_lookup_result
import word_lookup_platform as platform
from word_lookup_ocr import OcrError
from word_lookup_geometry import STRIP_CHARS, WORD_RE
from word_lookup_debug import diag_report
try:
    if TRAY_OK:
        import pystray
except (ImportError, RuntimeError):
    pystray = None


class SearchController:
    def _check_ocr(self):
        """Перевірка йде у фоновому потоці — tesseract_ready() запускає subprocess,
        і навіть із тайм-аутом краще не тримати на цьому головний потік Tk."""
        self.ocr.engine_cache["t"] = 0.0
        self.ocr_var.set("Перевіряю…")

        def work():
            t, r = self.ocr.tesseract_ready(), self.ocr.rapid_ready()
            txt = (f"Tesseract: {'✓ знайдено' if t else '✗ не знайдено'}    "
                   f"RapidOCR: {'✓ встановлено' if r else '✗ не встановлено'}")
            txt += f"\nPython: {sys.executable}"
            if not t and not r:
                txt += ("\nПотрібен хоча б один рушій. Ставити саме в цей Python:\n"
                        f'"{sys.executable}" -m pip install rapidocr-onnxruntime')
            self.events.put(as_event(("ocr_status", txt)))
        threading.Thread(target=work, daemon=True).start()


    def test_lookup(self):
        self._lookup_profile = {}
        word = self.providers.correct_ocr_word(self.ocr.clean_word(self.test_var.get().strip())[0])
        if not word:
            return
        e = self.test_entry
        anchor = (e.winfo_rootx(), e.winfo_rooty(), e.winfo_rootx() + e.winfo_width(),
                  e.winfo_rooty() + e.winfo_height())
        token = self._new_token()

        @network_job(lambda: token != self.active_token)
        def work():
            self.post_lookup(token, "loading", anchor)

            def on_partial(partial_card, _t=token):
                if _t == self.active_token:
                    self.events.put(as_event(("show", partial_card, None, anchor, _t)))

            try:
                card, showed_partial = self.providers.build_word_card_fast(word, on_partial)
            except Exception as ex:
                if token == self.active_token:
                    self.post_lookup(token, "toast", f"Помилка: {ex}")
                return
            if token != self.active_token:
                return
            if showed_partial:
                self.events.put(as_event(("update", token, word, card)))
            else:
                self.events.put(as_event(("show", card, None, anchor, token)))
        threading.Thread(target=work, daemon=True).start()


    def diagnose_ocr(self):
        """Через 3 с робить знімок навколо курсора й показує, що бачить OCR."""
        def work():
            for n in (3, 2, 1):
                self.events.put(as_event(("toast", f"Діагностика OCR: наведи курсор на текст… {n}")))
                time.sleep(1)
            x, y = self.ms.position
            dbg, hit, err = {}, None, None
            try:
                hit = self.ocr.find_word_at(x, y, dbg)
            except OcrError as e:
                err = str(e)
            except Exception as e:
                self.logger.exception()
                err = f"Помилка: {type(e).__name__}: {e}"
            dbg["time"] = self.ocr.last["time"]
            self.events.put(as_event(("diag", diag_report(self.settings, dbg, hit, err, (x, y)))))
        threading.Thread(target=work, daemon=True).start()


    def diagnose_dict(self):
        """Перевіряє переклад і dictionaryapi.dev на слові 'hello' окремо один від одного."""
        self.events.put(as_event(("dict_diag", "Перевіряю…")))

        def work():
            lines = []
            info = info2 = {"meanings": []}
            t0 = time.time()
            try:
                main, groups, name = self.providers.translate_with_fallback("hello", "en", True)
                lines.append(f"✓ Переклад ({name}): {time.time() - t0:.1f} с — "
                             f"«{main}»" + (f", {len(groups)} груп" if groups else ""))
            except Exception as e:
                lines.append(f"✗ Переклад: {type(e).__name__}: {e}")

            t0 = time.time()
            try:
                info = self.providers._freedictionary_lookup("hello")
                dt = time.time() - t0
                if info["meanings"]:
                    lines.append(f"✓ dictionaryapi.dev: {dt:.1f} с — знайдено "
                                 f"{len(info['meanings'])} значень, транскрипція "
                                 f"«{info['phonetic'] or '—'}»")
                else:
                    lines.append(f"✗ dictionaryapi.dev: відповів за {dt:.1f} с, але без даних")
            except Exception as e:
                lines.append(f"✗ dictionaryapi.dev: {time.time() - t0:.1f} с — {e}")

            t0 = time.time()
            try:
                info2 = self.providers.datamuse_dictionary("hello")
                dt = time.time() - t0
                if info2["meanings"]:
                    lines.append(f"✓ Datamuse (резервне джерело): {dt:.1f} с — знайдено "
                                 f"{len(info2['meanings'])} значень")
                else:
                    lines.append(f"✗ Datamuse (резервне джерело): відповів за {dt:.1f} с, без даних")
            except Exception as e:
                lines.append(f"✗ Datamuse (резервне джерело): {time.time() - t0:.1f} с — {e}")

            if not info["meanings"] and not info2["meanings"]:
                lines.append("Обидва джерела визначень недоступні — лишиться тільки переклад. "
                             "Спробуй ще раз пізніше або зміни мережу (VPN/мобільний інтернет).")
            self.events.put(as_event(("dict_diag", "\n".join(lines))))
        threading.Thread(target=work, daemon=True).start()


    def retry_word(self, word, previous, anchor):
        """Correct OCR text without taking another screenshot or pausing the game."""
        token = self._new_token()
        # The edited card may come from the library, after a different game's
        # lookup. Keep its captured source/tag instead of relabelling it.
        self._lookup_profiles[token] = {}
        self.close_all()
        self.show_loading(anchor)
        preserved = {key: previous[key] for key in (
            "context", "source", "sentence", "sentence_tr", "_context_image", "image_file", "anki_tag") if key in previous}

        def work():
            if token != self.active_token:
                return
            shown = False

            def partial(card):
                nonlocal shown
                if token == self.active_token:
                    shown = True
                    self.events.put(as_event(("show", {**card, **preserved}, None, anchor, token)))

            try:
                card, _ = self.providers.build_word_card_fast(word, partial, context=preserved.get("context", ""))
                if token != self.active_token:
                    return
                card = {**card, **preserved}
                definition = self.library._first_definition_text(card)
                need_translation = bool(definition and not card.get("definition_tr")
                                        and self.settings.data["sections"]["definitions"] and self.settings.data["popup"]["translate_definition"])
                card["def_tr_pending"] = need_translation
                if shown:
                    self.events.put(as_event(("update", token, word, dict(card))))
                else:
                    self.events.put(as_event(("show", dict(card), None, anchor, token)))
                if need_translation and token == self.active_token:
                    try:
                        card["definition_tr"] = self.providers.translate_text(definition)
                    except Exception:
                        self.logger.exception("Переклад визначення після виправлення OCR")
                    card["def_tr_pending"] = False
                    if token == self.active_token:
                        self.events.put(as_event(("update", token, word, card)))
            except Exception:
                self.logger.exception("Повторний пошук слова")
                self.post_lookup(token, "toast", f"Не вдалося знайти «{word}». Спробуй ще раз.")
        def run():
            with manual_word_scope(), network_scope(lambda: token != self.active_token):
                work()
        threading.Thread(target=run, daemon=True).start()


    def _new_token(self):
        """Кожен новий пошук отримує унікальний номер і одразу стає «активним» —
        це дозволяє застарілим (уже неактуальним) результатам, що прийдуть пізніше
        з паралельних потоків, розпізнати себе й не показатись поверх нового пошуку
        («останній запит виграє» замість жорсткого блокування одне одного)."""
        token = next(self._token_counter)
        self.active_token = token
        self._lookup_profiles[token] = copy.deepcopy(self._lookup_profile)
        self._lookup_profiles = dict(list(self._lookup_profiles.items())[-16:])
        return token


    @cancellable_lookup
    @profile_worker
    def _lookup_thread(self, mode, x, y, token, frozen=None, pre_hit=None):
        self.diagnostics.record("lookup.started", mode=mode, token=token)
        def stale():
            return token != self.active_token

        if stale():
            return
        if mode == "selection":
            self._lookup_selection(x, y, token)
            return
        context_image = None
        if mode in ("word", "line") and self.settings.data["learning"]["capture_context"]:
            try:
                context_image = self.ocr._grab(x - 480, y - 240, x + 480, y + 240, frozen)[0]
            except OcrError as e:
                self.logger.write(f"Скриншот контексту: {e}")
        self.logger.write(f"Пошук слова біля ({x}, {y}), режим: {mode}")
        source = platform._game_source()  # заголовок активного вікна (гра) — поки фокус ще в ній
        try:
            hit = pre_hit if pre_hit is not None else self.ocr.find_word_at(x, y, wide=(mode == "line"), frozen=frozen)
        except OcrError as e:
            self.logger.write(str(e))
            if not stale():
                self.post_lookup(token, "toast", str(e))
            return
        except Exception as e:
            self.logger.exception()
            if not stale():
                self.post_lookup(token, "toast", f"Помилка: {type(e).__name__}: {e}")
            return
        if hit is None:
            n = self.ocr.last["count"]
            self.logger.write(f"Слово не знайдено (OCR розпізнав слів: {n})")
            if not stale():
                self.post_lookup(token, "toast", "OCR не бачить тексту тут — спробуй «Діагностика OCR»"
                            if n == 0 else
                            f"Курсор не на слові (OCR бачить {n} слів поруч)")
            return
        if stale():  # поки йшло розпізнавання, користувач уже перейшов до іншого слова
            return
        hit = dict(hit)
        raw_word = hit["word"]
        hit["word"] = self.providers.correct_ocr_word(raw_word)
        if raw_word != hit["word"]:
            hit["ocr_word"] = raw_word
        self.logger.write(f"Знайдено: {hit['word']} (OCR {self.ocr.last['time']:.2f} с)")
        run_lookup_result(self, mode, x, y, token, hit, frozen, source, context_image)


    @cancellable_lookup
    @profile_worker
    def _region_thread(self, result, frozen=None, token=None):
        if token is None:
            token = self._new_token()
        if frozen is None:
            time.sleep(0.15)  # без стоп-кадру — даємо екрану перемалюватись після зникнення накладки
        if token != self.active_token:
            return
        if result[0] == "point":
            self.post_lookup(token, "loading", (result[1], result[2], result[1], result[2]))
            self._lookup_thread("word", result[1], result[2], token, frozen)
            return
        if result[0] == "word":  # слово вже підсвічене й розпізнане на застиглому екрані
            self.post_lookup(token, "loading", (result[1], result[2], result[1], result[2]))
            self._lookup_thread("word", result[1], result[2], token, frozen, pre_hit=result[3])
            return

        def stale():
            return token != self.active_token

        _, x0, y0, x1, y1 = result
        source = platform._game_source()
        context_image = None
        if self.settings.data["learning"]["capture_context"]:
            try:
                context_image = self.ocr._grab(x0, y0, x1, y1, frozen)[0]
            except OcrError as e:
                self.logger.write(f"Скриншот контексту: {e}")
        self.post_lookup(token, "loading", (x0, y0, x1, y1))
        self.logger.write(f"Виділено область ({x0}, {y0})–({x1}, {y1})")
        try:
            words = self.ocr.ocr_region(x0, y0, x1, y1, frozen=frozen)
        except OcrError as e:
            if not stale():
                self.post_lookup(token, "toast", str(e))
            return
        except Exception as e:
            self.logger.exception()
            if not stale():
                self.post_lookup(token, "toast", f"Помилка: {type(e).__name__}: {e}")
            return
        if not words:
            if not stale():
                self.post_lookup(token, "toast", "У рамці не знайдено тексту" if self.ocr.last["count"] == 0
                            else "У рамці немає розпізнаного тексту — виділи трохи ширше")
            return
        if stale():
            return
        text = " ".join(w.text for w in words)
        core = self.providers.correct_ocr_word(self.ocr.clean_word(words[0].text)[0]) if len(words) == 1 else ""
        try:
            if core:  # одне слово -> словникова картка
                w = words[0]
                _, lead, trail = self.ocr.clean_word(w.text)
                wd = w.x1 - w.x0
                hit = {"word": core, "line_text": text,
                       "box": (int(w.x0 + wd * lead), int(w.y0),
                               int(w.x1 - wd * trail), int(w.y1))}
                if self.settings.data["ocr"]["highlight"]:
                    self.events.put(as_event(("hl", hit, token)))

                def on_partial(partial_card, _hit=hit, _t=token):
                    if _t == self.active_token:
                        self.events.put(as_event(("show", {**partial_card, "_context_image": context_image, "source": source}, _hit, None, _t)))

                card, showed_partial = self.providers.build_word_card_fast(core, on_partial)
                card = {**card, "_context_image": context_image, "source": source}
                if stale():
                    return
                if showed_partial:
                    self.events.put(as_event(("update", token, core, card)))
                else:
                    self.events.put(as_event(("show", card, hit, None, token)))
            elif re.search(r"[A-Za-z]", text):  # кілька слів / рядок -> переклад тексту
                if not stale():
                    card = self.providers.text_card(text, self.providers.translate_text(self.providers.clean_hud_line(text)))
                    card.update(_context_image=context_image, source=source)
                    self.events.put(as_event(("show", card, None, (x0, y0, x1, y1), token)))
            else:
                if not stale():
                    self.post_lookup(token, "toast", "У рамці немає англійського тексту")
        except Exception as e:
            if not stale():
                self.post_lookup(token, "toast", f"Помилка перекладу: {e}")


    @cancellable_lookup
    @profile_worker
    def _lookup_selection(self, x, y, token):
        def stale():
            return token != self.active_token

        text = self._copy_selection()
        if not text:
            if not stale():
                self.post_lookup(token, "toast", "Нічого не виділено")
            return
        text = " ".join(text.split())[:1500]
        anchor = (x, y, x, y)
        try:
            core = self.providers.correct_ocr_word(text.strip(STRIP_CHARS))
            if core and WORD_RE.fullmatch(core):
                def on_partial(partial_card, _t=token):
                    if _t == self.active_token:
                        self.events.put(as_event(("show", partial_card, None, anchor, _t)))

                card, showed_partial = self.providers.build_word_card_fast(core, on_partial)
                if stale():
                    return
                if showed_partial:
                    self.events.put(as_event(("update", token, core, card)))
                    return
            else:
                card = self.providers.text_card(text, self.providers.translate_text(text))
        except Exception as e:
            if not stale():
                self.post_lookup(token, "toast", f"Помилка перекладу: {e}")
            return
        if not stale():
            self.events.put(as_event(("show", card, None, anchor, token)))


    def _start_lookup(self, mode, pos=None):
        self._prepare_game_profile()
        x, y = pos if pos is not None else self.ms.position
        token = self._new_token()
        frozen = None
        if mode in ("word", "line"):
            self.close_all()
            if self.toast_popup:
                self.toast_popup.close()
                self.toast_popup = None
            if not self._begin_game_pause():
                return
            if self._game_pause is not None:
                try:
                    root = self.root
                    sx, sy = root.winfo_vrootx(), root.winfo_vrooty()
                    width, height = root.winfo_vrootwidth(), root.winfo_vrootheight()
                    if width < 100 or height < 100:
                        sx, sy = 0, 0
                        width, height = root.winfo_screenwidth(), root.winfo_screenheight()
                    frozen = self.ocr._grab(sx, sy, sx + width, sy + height)[:3]
                    if not self._after_capture_pause():
                        self.toast("Не підтверджено відновлення гри — перевір журнал", timeout=8)
                        return
                except Exception as error:
                    self._resume_game()
                    self.logger.exception("Стоп-кадр для пошуку під курсором")
                    self.toast(f"Не вдалося зробити стоп-кадр: {error}", timeout=8)
                    return
        else:
            # Clipboard copying needs the source app running and never pauses it.
            if not self._resume_game():
                return
        self.post_lookup(token, "loading", (x, y, x, y))
        try:
            threading.Thread(target=self._lookup_thread, args=(mode, x, y, token, frozen), daemon=True).start()
        except Exception:
            self._resume_game()
            self.logger.exception("Запуск пошуку")
            self.toast("Не вдалося запустити пошук", timeout=8)


    def do_action(self, name):
        if name == "resume_game":
            if self.selector:
                self.selector.finish(None)
            if self.free_cursor:
                self.free_cursor.finish(None)
            self.dismiss_lookup()
            return
        if name == "toggle_pause":
            self.toggle_pause()
            return
        if self.paused:
            return
        if name == "lookup_word":
            self._start_lookup("word")
        elif name == "translate_line":
            self._start_lookup("line")
        elif name == "select_region":
            self.start_region_select()
        elif name == "free_cursor":
            self.start_free_cursor()
        elif name == "unlock_cursor":
            self.toast("Курсор відкріплено" if platform.release_cursor_clip()
                       else "Не вдалося (працює лише на Windows)")
        elif name == "translate_selection":
            self._start_lookup("selection")
        elif name == "speak":
            card = self.last_card
            audios = card.get("audios") if card else None
            if audios and AUDIO_OK:
                self.providers.play_audio(audios[0][1])
            else:
                self.toast("Немає озвучення (потрібен pygame)" if not AUDIO_OK else "Немає озвучення")
        elif name == "copy_translation":
            if self.last_card:
                pyperclip.copy(self.last_card["translation"])
                self.toast("Переклад скопійовано")
        elif name == "close_popup":
            self.dismiss_lookup()

