"""Translation providers and card assembly with explicit domain dependencies."""
from __future__ import annotations
import io
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from wordlookup.network.budget import (REQUEST_GATE, LookupCancelled, check_cancelled, scoped_task_until,
    current_translation_language, translation_scope, scoped_task, correct_ocr_word as _explicit_ocr_word)
from wordlookup.network.policy import (remote_request, mobile_translation, network_allowed as _network_allowed, LockedSession)
from word_lookup_lexicon import (IRREGULAR_FORMS, IRREGULAR_AMBIGUOUS,
    correct_ocr as _local_correct_ocr, base_forms as _local_base_forms, manual_word_active)
from wordlookup.network.official import OfficialTranslators, ProviderRateLimited as RateLimited
from word_lookup_offline import OfflineTranslator
from word_lookup_models import TranslationOutcome, LAST_TRANSLATOR
from word_lookup_utils import clamp
from word_lookup_version import __version__

DM_POS = {"n": "noun", "v": "verb", "adj": "adjective", "adv": "adverb", "u": ""}

HEADERS = {"User-Agent": f"WordLookup/{__version__} (+https://github.com/SanchezKabanchez/Word_Lookuper)"}
UA_POS = {
    "noun": "іменник", "verb": "дієслово", "adjective": "прикметник",
    "adverb": "прислівник", "pronoun": "займенник", "preposition": "прийменник",
    "conjunction": "сполучник", "interjection": "вигук", "exclamation": "вигук",
    "article": "артикль", "abbreviation": "скорочення", "particle": "частка",
    "numeral": "числівник", "determiner": "детермінатив", "prefix": "префікс",
    "suffix": "суфікс", "phrase": "фраза", "auxiliary verb": "допоміжне дієслово",
    "modal verb": "модальне дієслово", "contraction": "скорочення",
}
_FORM_OF_RE = re.compile(r"\b(past|participle|plural|comparative|superlative|tense|inflection)\b.{0,60}\bof\b",
                         re.I)
_HUD_GLUE_RE = re.compile(r"([A-Za-zА-Яа-яІіЇїЄєҐґ])([:\d])|([:\d])([A-Za-zА-Яа-яІіЇїЄєҐґ])")

class TranslateError(Exception):
    def __init__(self, message, limited=False):
        super().__init__(message)
        self.limited = limited


class DeeplError(Exception):
    pass



def _language_scoped(function):
    @wraps(function)
    def run(self, *args, **kwargs):
        with translation_scope(self._target_language()):
            return function(self, *args, **kwargs)
    return run


class Providers:
    def __init__(self, settings, cache, logger, game_source=lambda: ""):
        self.game_source = game_source
        self.settings, self.cache, self.logger = settings, cache, logger
        self.session = LockedSession(HEADERS)
        self.cooldowns = {}
        self.cooldown_lock = threading.RLock()
        self.official = OfficialTranslators(lambda: settings.data, lambda *a, **k: self._remote_post(*a, **k), self._target_language)
        self.offline = OfflineTranslator(lambda: settings.data, check_cancelled)
        self.chain = (("Argos (локально)", self._p_argos), ("Google Cloud", self.official.google_cloud),
                      ("LibreTranslate", self.official.libre_translate), ("Google", self._p_gtx),
                      ("Google (Chrome)", self._p_chrome), ("Google (mobile)", self._p_deep), ("MyMemory", self._p_mymemory))

    def _target_language(self):
        language = current_translation_language()
        return self.settings.data["languages"]["target"] if language is None else language


    def pos_name(self, pos: str) -> str:
        return UA_POS.get(pos, pos) if self._target_language() == "uk" else pos


    def network_allowed(self, provider):
        return _network_allowed(self.settings.data, provider)


    def _remote_get(self, url, **kwargs):
        return remote_request(lambda: self.settings.data, self.session, REQUEST_GATE, "get", url, **kwargs)


    def _remote_post(self, url, **kwargs):
        return remote_request(lambda: self.settings.data, self.session, REQUEST_GATE, "post", url, **kwargs)


    def _http_json(self, url, params, timeout=5):
        r = self._remote_get(url, params=params, timeout=timeout)
        if r.status_code == 429:
            raise RateLimited("HTTP 429")
        r.raise_for_status()
        return r.json()


    def _max_tr(self) -> int:
        return int(clamp(self.settings.data["dictionary"]["max_translations"], 1, 20))


    def _p_gtx(self, text, sl, want_dict):
        """Неофіційний ендпойнт Google Translate (для слів — з частинами мови)."""
        data = self._http_json("https://translate.googleapis.com/translate_a/single",
                          {"client": "gtx", "sl": sl, "tl": self._target_language(),
                           "dt": ["t", "bd"] if want_dict else ["t"], "q": text})
        if not isinstance(data, list) or not data or not isinstance(data[0], list):
            raise ValueError("Google GTX: несумісний формат відповіді")
        segments = data[0]
        if any(not isinstance(seg, list) or not seg or not isinstance(seg[0], str) for seg in segments):
            raise ValueError("Google GTX: несумісний формат сегментів")
        main = "".join(seg[0] for seg in segments)
        if not main:
            raise ValueError("Google GTX: порожня відповідь")
        groups = []
        if want_dict and len(data) > 1 and data[1]:
            for entry in data[1]:
                if entry[1]:
                    groups.append((self.pos_name(entry[0]), entry[1][:self._max_tr()]))
        return main, groups


    def _p_chrome(self, text, sl, want_dict):
        """Ендпойнт Google-словника для Chrome — окремий ліміт запитів."""
        data = self._http_json("https://clients5.google.com/translate_a/t",
                          {"client": "dict-chrome-ex", "sl": sl,
                           "tl": self._target_language(), "q": text})
        main, groups = "", []
        if isinstance(data, dict):
            main = "".join(s.get("trans", "") for s in data.get("sentences", []))
            if want_dict:
                for e in data.get("dict") or []:
                    if e.get("terms"):
                        groups.append((self.pos_name(e.get("pos", "")), e["terms"][:self._max_tr()]))
        elif isinstance(data, list) and data:
            first = data[0]
            while isinstance(first, list) and first:
                first = first[0]
            main = first if isinstance(first, str) else ""
        if not main:
            raise ValueError("порожня відповідь")
        return main, groups


    def _p_deep(self, text, sl, want_dict):
        """Мобільна сторінка Google з явним тайм-аутом, як в інших джерел."""
        text = text.strip()
        if not text or sl == self._target_language():
            return text, []
        if len(text) > 5000:
            raise ValueError("Завеликий текст для мобільного перекладача")

        response = self._remote_get("https://translate.google.com/m", params={
            "sl": sl, "tl": self._target_language(), "q": text}, timeout=8)
        try:
            if response.status_code == 429:
                raise RateLimited("Google mobile: HTTP 429")
            response.raise_for_status()
            out = mobile_translation(response.text)
            if not out:
                raise ValueError("Мобільний перекладач не повернув переклад")
            return out, []
        finally:
            response.close()


    def _chunks(self, text, size=450):
        out, cur = [], ""
        for w in text.split():
            if cur and len(cur) + 1 + len(w) > size:
                out.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        if cur:
            out.append(cur)
        return out or [text]


    def _p_mymemory(self, text, sl, want_dict):
        """MyMemory — безкоштовний сервіс на іншій інфраструктурі (запасний варіант)."""
        sl = "en" if sl == "auto" else sl
        tl = self._target_language()
        chunks = self._chunks(text)
        parts, groups = [], []
        for chunk in chunks:
            data = self._http_json("https://api.mymemory.translated.net/get",
                              {"q": chunk, "langpair": f"{sl}|{tl}"})
            out = (data.get("responseData") or {}).get("translatedText") or ""
            if str(data.get("responseStatus")) == "429" or "MYMEMORY WARNING" in out.upper():
                raise RateLimited("MyMemory: ліміт")
            parts.append(out)
            if want_dict and len(chunks) == 1 and out:
                seen, alts = {out.lower()}, []
                for m in sorted(data.get("matches") or [],
                                key=lambda m: float(m.get("match") or 0), reverse=True):
                    t = (m.get("translation") or "").strip()
                    if (str(m.get("segment", "")).strip().lower() == text.lower() and t
                            and t.lower() not in seen and len(t) <= 40 and "\n" not in t):
                        seen.add(t.lower())
                        alts.append(t)
                groups = [("", [out] + alts[: self._max_tr() - 1])]
        return " ".join(parts).strip(), groups


    def _p_argos(self, text, source, want_dictionary):
        return self.offline.translate(text, source, self._target_language()), []


    def provider_cooldown(self, name):
        with self.cooldown_lock:
            return self.cooldowns.get(name, (0, False))


    def set_provider_cooldown(self, name, value):
        with self.cooldown_lock:
            self.cooldowns[name] = value


    def clear_provider_cooldown(self, name):
        with self.cooldown_lock:
            self.cooldowns.pop(name, None)


    def translate_with_fallback(self, text: str, sl: str, want_dict: bool):
        """Пробує джерела по черзі; те, що видало ліміт (429), відпочиває 90 с."""
        limited = False
        available = False
        for name, fn in self.chain:
            provider = {"MyMemory": "mymemory", "Google Cloud": "googlecloud",
                        "LibreTranslate": "libretranslate", "Argos (локально)": "argos"}.get(name, "google")
            if provider == "argos":
                enabled = self.settings.data["offline_translation"]["enabled"]
            else:
                enabled = self.network_allowed(provider)
            if not enabled or (provider == "googlecloud" and not self.settings.data["googlecloud"]["key"].strip()):
                continue
            if provider == "libretranslate" and not self.settings.data["libretranslate"]["url"].strip():
                continue
            available = True
            check_cancelled()
            until, was_limit = self.provider_cooldown(name)
            if time.time() < until:
                limited = limited or was_limit
                continue
            try:
                main, groups = fn(text, sl, want_dict)
            except LookupCancelled:
                raise
            except RateLimited:
                self.set_provider_cooldown(name, (time.time() + 90, True))
                limited = True
                self.logger.write(f"{name}: ліміт запитів, пауза 90 с")
                continue
            except Exception as e:
                self.set_provider_cooldown(name, (time.time() + 10, False))
                self.logger.write(f"{name}: {type(e).__name__}: {e}")
                continue
            if main or groups:
                self.logger.write(f"Переклад: {name}")
                return main, groups, name
        if not available:
            if self.settings.data.get("network", {}).get("offline", False):
                raise TranslateError("Офлайн-режим: цього перекладу немає в кеші. Увімкни Argos і встанови локальну модель.")
            raise TranslateError("Увімкни джерело перекладу або налаштуй DeepL у налаштуваннях мережі.")
        if limited:
            raise TranslateError("Джерела перекладу тимчасово обмежили запити з твоєї адреси. Спробуй за кілька "
                                 "хвилин або зміни мережу (VPN, мобільний інтернет).", limited=True)
        raise TranslateError("Переклад недоступний — перевір інтернет-з'єднання.")


    def correct_ocr_word(self, word):
        """Use explicit font fixes first, then a conservative local corpus suggestion."""
        if manual_word_active():
            return word
        fixed = _explicit_ocr_word(word)
        return fixed if fixed != word else (_local_correct_ocr(word) or word)


    def base_form_candidates(self, word: str):
        """running -> run, cities -> city, that's -> that."""
        w = word.lower().replace("’", "'")
        base = IRREGULAR_FORMS.get(w) if w not in IRREGULAR_AMBIGUOUS else None
        out = [base, w] if base else [w]
        if "'" in w:
            out.append(w.split("'")[0])
        for suf, rep in (("ies", "y"), ("es", ""), ("s", ""), ("ing", ""),
                         ("ed", ""), ("ing", "e"), ("ed", "e")):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                stem = w[: -len(suf)] + rep
                out.append(stem)
                if len(stem) > 3 and stem[-1] == stem[-2]:
                    out.append(stem[:-1])
        out.extend(c for c in _local_base_forms(w) if c != w and w not in IRREGULAR_AMBIGUOUS)
        return list(dict.fromkeys(c for c in out if c))[:5]


    def _datamuse_get(self, params, timeout=5):
        if not self.network_allowed("datamuse"):
            return []
        r = self._remote_get("https://api.datamuse.com/words", params=params, timeout=timeout)
        if r.status_code != 200:
            return []
        try:
            return r.json()
        except ValueError:
            return []


    def datamuse_dictionary(self, word: str) -> dict:
        """Запасне джерело визначень і синонімів на основі WordNet — використовується,
        коли dictionaryapi.dev недоступний. Без транскрипції та аудіо."""
        empty = {"base": "", "phonetic": "", "audios": [], "meanings": []}
        base, defs_by_pos = "", {}
        for cand in self.base_form_candidates(word)[:3]:
            data = self._datamuse_get({"sp": cand, "md": "d", "max": 1})
            if data and data[0].get("word", "").lower() == cand and data[0].get("defs"):
                base = cand
                for entry in data[0]["defs"]:
                    pos_code, _, definition = entry.partition("	")
                    defs_by_pos.setdefault(DM_POS.get(pos_code, pos_code), []).append(definition.strip())
                break
        if not base:
            return empty

        def rel(kind):
            return [d["word"] for d in self._datamuse_get({f"rel_{kind}": base, "max": 15}) if d.get("word")]

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_syn, f_ant = pool.submit(scoped_task(rel, "syn")), pool.submit(scoped_task(rel, "ant"))
            syn, ant = f_syn.result(), f_ant.result()

        meanings = [{"pos": self.pos_name(pos_en) if pos_en else "", "defs": [{"d": d, "e": ""} for d in defs],
                    "syn": syn if i == 0 else [], "ant": ant if i == 0 else []}
                   for i, (pos_en, defs) in enumerate(defs_by_pos.items())]
        return {"base": base, "phonetic": "", "audios": [], "meanings": meanings}


    def _freedictionary_lookup(self, word: str) -> dict:
        """Транскрипція, аудіо, визначення, приклади, синоніми (dictionaryapi.dev)."""
        empty = {"base": "", "phonetic": "", "audios": [], "meanings": []}
        if not self.network_allowed("dictionaryapi"):
            return empty
        cands = self.base_form_candidates(word)[:3]

        def fetch(c):
            try:
                r = self._remote_get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{c}", timeout=6)
            except LookupCancelled:
                raise
            except Exception:
                return "err", None
            if r.status_code == 200:
                try:
                    return "ok", r.json()
                except ValueError:
                    return "err", None
            return ("missing" if r.status_code == 404 else "err"), None

        # Stop at the first real entry: do not send speculative suffix requests in parallel.
        results = []
        for candidate in cands:
            check_cancelled()
            result = fetch(candidate)
            results.append(result)
            if result[0] in {"ok", "err"}:
                break
        errors = [c for c, (status, _p) in zip(cands, results) if status == "err"]
        for cand, (status, payload) in zip(cands, results):
            if status != "ok":
                continue
            phonetic, audios, merged = "", {}, {}
            for entry in payload:
                phonetic = phonetic or entry.get("phonetic") or ""
                for p in entry.get("phonetics", []):
                    phonetic = phonetic or p.get("text") or ""
                    a = p.get("audio") or ""
                    if a:
                        url = "https:" + a if a.startswith("//") else a
                        low = url.lower()
                        label = ("BrE" if ("-uk" in low or "_gb_" in low)
                                 else "AmE" if ("-us" in low or "_us_" in low) else "")
                        audios.setdefault(label, url)
                for m in entry.get("meanings", []):
                    pos = m.get("partOfSpeech", "")
                    slot = merged.setdefault(pos, {"pos": pos, "defs": [], "syn": [], "ant": []})
                    for d in m.get("definitions", []):
                        slot["defs"].append({"d": d.get("definition", ""), "e": d.get("example", "")})
                        slot["syn"] += d.get("synonyms", [])
                        slot["ant"] += d.get("antonyms", [])
                    slot["syn"] += m.get("synonyms", [])
                    slot["ant"] += m.get("antonyms", [])
            meanings = []
            for slot in merged.values():
                slot["syn"] = list(dict.fromkeys(slot["syn"]))
                slot["ant"] = list(dict.fromkeys(slot["ant"]))
                meanings.append(slot)
            labelled = [(lab, audios[lab]) for lab in ("BrE", "AmE") if lab in audios]
            chosen = labelled or list(audios.items())[:1]
            return {"base": cand, "phonetic": phonetic.strip("/ "),
                    "audios": chosen, "meanings": meanings}
        if errors:
            raise RuntimeError(f"словник dictionaryapi.dev недоступний (перевірено: {', '.join(errors)})")
        return empty


    def _datamuse_spellcheck(self, word: str):
        """Коли слова немає в жодному словнику — шукає найближче реальне англійське слово
        через нечіткий пошук Datamuse. Рятує від типових помилок OCR на стилізованих
        ігрових шрифтах (напр. «ccpyright» -> «copyright», «diskibution» -> «distribution»)."""
        try:
            data = self._datamuse_get({"sp": word, "max": 3, "md": "d"})
        except LookupCancelled:
            raise
        except Exception:
            return None
        for item in data:
            cand = item.get("word", "")
            if cand and cand.lower() != word.lower() and item.get("defs"):
                return cand
        return None


    def _looks_like_form_of(self, info: dict) -> bool:
        """Перший запис слова — це лише «форма іншого слова» (past tense of fight тощо)."""
        for m in info.get("meanings", [])[:2]:
            for d in m.get("defs", [])[:1]:
                if _FORM_OF_RE.search(d.get("d", "")):
                    return True
        return False


    @_language_scoped
    def english_dictionary(self, word: str) -> dict:
        """Словникова стаття для слова. Для неправильних форм («fought», «went», «children»)
        бере статтю початкового слова («fight», «go», «child»): сама форма в словниках — лише
        відсилка «past tense of …», без справжнього пояснення."""
        word = self.correct_ocr_word(word)
        low = word.lower()
        base = IRREGULAR_FORMS.get(low)
        if base and base != low:
            direct = None
            if low in IRREGULAR_AMBIGUOUS:
                try:
                    direct = self._english_lookup(word)
                except LookupCancelled:
                    raise
                except Exception:
                    direct = None
                if direct and direct.get("meanings") and not self._looks_like_form_of(direct):
                    return direct
            try:
                info = self._english_lookup(base)
            except LookupCancelled:
                raise
            except Exception:
                info = None
            if info and info.get("meanings"):
                self.logger.write(f"Словник: «{word}» — неправильна форма слова «{base}»")
                return {**info, "base": base}
            if direct is not None:
                return direct
        return self._english_lookup(word)


    def _english_lookup(self, word: str) -> dict:
        """dictionaryapi.dev, а якщо він недоступний або нічого не знайшов — запасне джерело
        Datamuse. Якщо і те, і те не знайшли ЖОДНОГО реального слова — востаннє пробує
        виправити ймовірну помилку OCR (нечітким пошуком) і повторює пошук для нього."""
        empty = {"base": "", "phonetic": "", "audios": [], "meanings": []}
        err = None
        try:
            primary = self._freedictionary_lookup(word)
        except LookupCancelled:
            raise
        except Exception as e:
            primary, err = empty, str(e)
        if primary["meanings"]:
            return primary
        try:
            fallback = self.datamuse_dictionary(word)
        except LookupCancelled:
            raise
        except Exception:
            fallback = empty
        if fallback["meanings"]:
            self.logger.write("Словник: основне джерело без результату, використано запасне (Datamuse)")
            return fallback
        corrected = self._datamuse_spellcheck(word)
        if corrected:
            try:
                retry = self.datamuse_dictionary(corrected)
            except LookupCancelled:
                raise
            except Exception:
                retry = empty
            if retry["meanings"]:
                self.logger.write(f"Словник: «{word}» не знайдено, схоже це помилка OCR — використано «{corrected}»")
                retry["corrected_from"] = word
                return retry
        if err:
            raise RuntimeError(err + " Резервне джерело (Datamuse) теж не знайшло визначень.")
        return empty


    def _deepl_configured(self) -> bool:
        d = self.settings.data.get("deepl", {})
        return bool(d.get("enabled") and str(d.get("key", "")).strip())


    def _deepl_active(self) -> bool:
        return self.network_allowed("deepl") and self._deepl_configured()


    def _deepl_base(self) -> str:
        # безкоштовні ключі закінчуються на ":fx" і працюють з іншим сервером
        return ("https://api-free.deepl.com" if self.settings.data["deepl"]["key"].strip().endswith(":fx")
                else "https://api.deepl.com")


    def _deepl_headers(self) -> dict:
        return {"Authorization": f"DeepL-Auth-Key {self.settings.data['deepl']['key'].strip()}"}


    def deepl_translate(self, text: str, sl: str = "auto", context: str = "") -> str:
        """Переклад через офіційний API DeepL. context — сусідній текст, який DeepL враховує
        для вибору значення слова, але сам не перекладає (напр. рядок навколо «chest»)."""
        data = {"text": text, "target_lang": self._target_language().upper()}
        if sl and sl != "auto":
            data["source_lang"] = sl.upper()
        if context:
            data["context"] = context[:500]
        r = self._remote_post(self._deepl_base() + "/v2/translate", data=data,
                         headers=self._deepl_headers(), timeout=6)
        if r.status_code in (429, 456):  # забагато запитів / вичерпано місячний ліміт
            raise RateLimited(f"DeepL HTTP {r.status_code}")
        if r.status_code in (401, 403):
            raise DeeplError("ключ DeepL недійсний")
        r.raise_for_status()
        out = " ".join(t.get("text", "") for t in (r.json().get("translations") or [])).strip()
        if not out:
            raise DeeplError("порожня відповідь DeepL")
        return out


    def deepl_usage(self):
        """(використано, ліміт) символів за поточний місяць — для перевірки ключа."""
        if not self.settings.data["deepl"]["key"].strip():
            raise DeeplError("ключ не вказано")
        r = self._remote_get(self._deepl_base() + "/v2/usage", headers=self._deepl_headers(), timeout=6)
        if r.status_code in (401, 403):
            raise DeeplError("ключ недійсний")
        r.raise_for_status()
        j = r.json()
        return int(j.get("character_count", 0)), int(j.get("character_limit", 0))


    def _try_deepl(self, text: str, sl: str = "auto", context: str = ""):
        """Переклад DeepL або None (не налаштовано / збій / ліміт) — тоді працює звичайний
        ланцюжок Google → MyMemory. Після збою DeepL кілька секунд не смикаємо."""
        if not self._deepl_active():
            return None
        until, _ = self.provider_cooldown("DeepL")
        if time.time() < until:
            return None
        try:
            return self.deepl_translate(text, sl, context)
        except RateLimited:
            self.set_provider_cooldown("DeepL", (time.time() + 300, True))
            self.logger.write("DeepL: ліміт запитів або символів, пауза 5 хв")
        except LookupCancelled:
            raise
        except Exception as e:
            self.set_provider_cooldown("DeepL", (time.time() + 30, False))
            self.logger.write(f"DeepL: {type(e).__name__}: {e}")
        return None


    @_language_scoped
    def translate_text(self, text: str) -> str:
        """Reuse sentence/definition translations across lookups, languages and launches."""
        text = " ".join(text.split())
        if not text:
            return ""
        def compute():
            dl = self._try_deepl(text, "auto", "")
            if dl:
                return {"text": dl, "provider": "DeepL"}
            main, _groups, _name = self.translate_with_fallback(text, "auto", False)
            return {"text": main, "provider": _name}
        key = json.dumps([self._target_language(), bool(self._deepl_configured()), text], ensure_ascii=False)
        limit = int(clamp(self.settings.data["cache"]["max_entries"], 0, 1000))
        path = os.path.join(os.path.dirname(self.settings.path()), "translations.json")
        result = self.cache.text_cache.translate(key, compute, limit=limit, path=path,
                                     on_error=lambda error: self.logger.write(f"Кеш перекладів: {error}"))
        if isinstance(result, str):
            LAST_TRANSLATOR.set("Кеш")
            return result
        LAST_TRANSLATOR.set(result["provider"])
        return result["text"]


    def clean_hud_line(self, text: str) -> str:
        """У «рядок» для перекладу часто потрапляють елементи інтерфейсу гри поруч зі
        звичайним текстом — лічильники, мітки команд тощо, зліплені без пробілу
        (напр. «COMMANDER:0»). Перекладач бачить таке як одне нечитабельне «слово» і
        просто лишає його без перекладу. Ця функція розділяє літери й цифри/двокрапки
        пробілом, щоб реальне слово («COMMANDER») переклалося окремо від цифр."""
        def sep(m):
            a, b, c, d = m.groups()
            if a is not None:
                return a if b == ":" else f"{a} {b}"
            return d if c == ":" else f"{c} {d}"
        prev = None
        while text != prev:  # можуть бути кілька зліплених місць підряд (літера-цифра-літера)
            prev = text
            text = _HUD_GLUE_RE.sub(sep, text)
        return text


    def text_card(self, original: str, translation: str) -> dict:
        return {"kind": "text", "original": original, "word": original,
                "translation": translation, "entry_kind": "phrase", "source": self.game_source(),
                "translator": LAST_TRANSLATOR.get()}


    def _google_word(self, word: str):
        try:
            # Google-словник (bd) часто дає МЕНШЕ варіантів для слова з великої літери
            # (напр. "Chest" з інтерфейсу гри) — рядкові й прописні там різні статті
            main, groups, name = self.translate_with_fallback(word.lower(), "en", True)
            return TranslationOutcome(main, groups, provider=name)
        except LookupCancelled:
            raise
        except TranslateError as e:
            return TranslationOutcome("", [], str(e), e.limited)
        except Exception as e:
            return TranslationOutcome("", [], f"Переклад недоступний ({type(e).__name__})")


    @_language_scoped
    def _translate_word(self, word: str, context: str = ""):
        """Prefer configured DeepL; only ask Google when DeepL is unavailable.

        Avoid a second translation request merely to obtain alternative word groups.
        Dictionary definitions still load independently for the word card.
        """
        if self._deepl_active():
            dl = self._try_deepl(word, "en", self.cache._clean_context(word, context))
            if dl:
                return TranslationOutcome(dl, [], provider="DeepL")
        return self._google_word(word)


    @_language_scoped
    def build_word_card(self, word: str, context: str = "") -> dict:
        """Повна картка за один виклик: чекає і переклад, і словник одночасно."""
        word = self.correct_ocr_word(word)
        key = self.cache._cache_key(word, context)
        with self.cache.lock:
            cache_generation = self.cache.generation
        cached = self.cache._cached_card_for_lookup(key)
        if cached is not None:
            cached["word"] = word
            return cached
        empty = {"base": "", "phonetic": "", "audios": [], "meanings": []}
        dictionary_sources = self.cache._dictionary_sources() if self.cache._dictionary_required() else []
        dictionary_enabled = bool(dictionary_sources)
        pool = ThreadPoolExecutor(max_workers=2) if dictionary_enabled else None
        stopped = threading.Event()
        try:
            if pool is None:
                translated = TranslationOutcome.from_legacy(self._translate_word(word, context))
                main, groups, note, limited = translated
                info, dict_note = empty, ""
            else:
                f_t = pool.submit(scoped_task_until(stopped, self._translate_word, word, context))
                f_e = pool.submit(scoped_task_until(stopped, self.english_dictionary, word))
                translated = TranslationOutcome.from_legacy(f_t.result(timeout=12))
                main, groups, note, limited = translated
                try:
                    info, dict_note = f_e.result(timeout=9), ""
                except LookupCancelled:
                    raise
                except Exception as e:
                    info, dict_note = empty, (str(e) if isinstance(e, RuntimeError) else
                                              "Англійський словник не відповів за 9 с — можливо, повільна мережа.")
        finally:
            stopped.set()
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        if info.get("corrected_from"):  # OCR, ймовірно, помилився — перекладаємо вже виправлене слово
            corrected = TranslationOutcome.from_legacy(self._translate_word(info["base"]))
            main2, groups2, note2, limited2 = corrected
            if main2 or groups2:
                main, groups, note, limited = main2, groups2, note2, limited2
                translated = corrected
        if not main and not groups and not info["meanings"]:
            raise TranslateError(note or dict_note or "Слово не знайдено в словниках", limited=limited)
        dictionary_sources = [source for source in dictionary_sources if self.network_allowed(source)]
        card = {"kind": "word", "word": word, "translation": main, "groups": groups, "translator": translated.provider,
                "note": note, "dict_note": dict_note, "_dictionary_complete": bool(dictionary_sources),
                "_dictionary_sources": dictionary_sources, **info}
        check_cancelled()
        if not note and not dict_note:
            self.cache._cache_word_card(key, card, expected_generation=cache_generation)
        return card


    @_language_scoped
    def build_word_card_fast(self, word: str, on_partial=None, context: str = ""):
        """Двофазний пошук: спершу лише переклад (зазвичай < 1 с) — викликає on_partial(картка)
        одразу, ще до звернення по визначення й синоніми. Потім довантажує словник і повертає
        ПОВНУ картку (як build_word_card). Якщо слово вже в кеші — on_partial узагалі не викликається,
        бо готова картка і так з'явиться миттєво."""
        word = self.correct_ocr_word(word)
        key = self.cache._cache_key(word, context)
        with self.cache.lock:
            cache_generation = self.cache.generation
        cached = self.cache._cached_card_for_lookup(key)
        if cached is not None:
            cached["word"] = word
            return cached, False
        empty = {"base": "", "phonetic": "", "audios": [], "meanings": []}
        dictionary_sources = self.cache._dictionary_sources() if self.cache._dictionary_required() else []
        dictionary_enabled = bool(dictionary_sources)
        pool = ThreadPoolExecutor(max_workers=1) if dictionary_enabled else None
        stopped = threading.Event()
        try:
            # Обидва джерела стартують разом; показ першого перекладу не чекає словника.
            dictionary = pool.submit(scoped_task_until(stopped, self.english_dictionary, word)) if pool else None
            translated = TranslationOutcome.from_legacy(self._translate_word(word, context))
            main, groups, note, limited = translated
            partial = {"kind": "word", "word": word, "translation": main, "groups": groups, "translator": translated.provider,
                      "note": note, "dict_note": "", "dict_pending": bool(dictionary), **empty}
            if on_partial and dictionary is not None:
                on_partial(partial)
            try:
                info, dict_note = dictionary.result(timeout=9) if dictionary else empty, ""
            except LookupCancelled:
                raise
            except Exception as e:
                info, dict_note = empty, (str(e) if isinstance(e, RuntimeError) else
                                          "Англійський словник не відповів — можливо, повільна мережа.")
        finally:
            stopped.set()
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        if info.get("corrected_from"):  # OCR, ймовірно, помилився — перекладаємо вже виправлене слово
            corrected = TranslationOutcome.from_legacy(self._translate_word(info["base"]))
            main2, groups2, note2, limited2 = corrected
            if main2 or groups2:
                main, groups, note, limited = main2, groups2, note2, limited2
                translated = corrected
        if not main and not groups and not info["meanings"]:
            raise TranslateError(note or dict_note or "Слово не знайдено в словниках", limited=limited)
        dictionary_sources = [source for source in dictionary_sources if self.network_allowed(source)]
        full = {**partial, "translation": main, "groups": groups, "note": note, "translator": translated.provider,
                "dict_note": dict_note, "dict_pending": False, "_dictionary_complete": bool(dictionary_sources),
                "_dictionary_sources": dictionary_sources, **info}
        check_cancelled()
        if not note and not dict_note:
            self.cache._cache_word_card(key, full, expected_generation=cache_generation)
        return full, dictionary is not None


    def play_audio(self, url: str) -> None:
        if not url or url == "preview" or not self.network_allowed("dictionaryapi"):
            return

        def run():
            try:
                import pygame
                response = self._remote_get(url, audio=True, timeout=8)
                try:
                    response.raise_for_status()
                    data = response.content
                finally:
                    response.close()
                if not pygame.mixer.get_init():
                    pygame.mixer.init()
                pygame.mixer.music.load(io.BytesIO(data))
                pygame.mixer.music.play()
            except Exception:
                self.logger.exception("Відтворення аудіо")
        threading.Thread(target=run, daemon=True).start()

