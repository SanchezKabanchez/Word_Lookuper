"""OCR engines and capture geometry with bounded native CPU use."""
from __future__ import annotations
import csv
import io
import os
import sys
import importlib.util
import subprocess
import math
import re
import threading
import time
import word_lookup_geometry as geometry
from word_lookup_geometry import W, STRIP_CHARS
from wordlookup.network.budget import LookupCancelled, check_cancelled
from word_lookup_profiles import current_profile
from word_lookup_ocr_budget import (cpu_threads, max_side, bounded_number, fit_image, ocr_slot, create_rapid_engine)
from word_lookup_utils import clamp

class OcrError(Exception):
    pass



class Ocr:
    def __init__(self, settings, diagnostics, logger):
        self.settings, self.diagnostics, self.logger = settings, diagnostics, logger
        self.engine_cache = {"key": None, "val": None, "t": 0.0}
        self.rapid_run = None
        self.rapid_settings = None
        self.rapid_lock = threading.Lock()
        self.last = {"count": 0, "time": 0.0}

    def _default_tesseract(self) -> str:
        if sys.platform.startswith("win"):
            for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                      r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
                if os.path.exists(p):
                    return p
        return ""


    def _configure_tesseract_cmd(self):
        """Підключає pytesseract і виставляє шлях до tesseract.exe. Не звертається до
        самого tesseract (без subprocess) — це швидко й безпечно викликати часто."""
        try:
            import pytesseract
        except ImportError:
            return None
        cmd = self.ocr_settings()["tesseract_cmd"] or self._default_tesseract()
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        return pytesseract


    def tesseract_ready(self) -> bool:
        """Чи можна реально запустити tesseract. Виконує subprocess із жорстким
        тайм-аутом 2 с, щоб зламаний чи заблокований (антивірусом) шлях ніколи не
        підвісив програму — раніше тут не було тайм-ауту взагалі."""
        pytesseract = self._configure_tesseract_cmd()
        if pytesseract is None:
            return False
        cmd = pytesseract.pytesseract.tesseract_cmd or "tesseract"
        try:
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform.startswith("win") else {}
            subprocess.run([cmd, "--version"], capture_output=True, timeout=2, **kwargs)
            return True
        except (OSError, subprocess.SubprocessError):
            return False


    def rapid_ready(self) -> bool:
        return importlib.util.find_spec("rapidocr_onnxruntime") is not None


    def pick_engine(self) -> str:
        """Вибір рушія; результат кешується на хвилину (перевірка Tesseract запускає процес)."""
        key = (self.ocr_settings()["engine"], self.ocr_settings()["tesseract_cmd"])
        if self.engine_cache["key"] == key and time.time() - self.engine_cache["t"] < 60:
            return self.engine_cache["val"]
        val = self._pick_engine_uncached()
        self.engine_cache.update(key=key, val=val, t=time.time())
        return val


    def _pick_engine_uncached(self) -> str:
        eng = self.ocr_settings()["engine"]
        if eng == "tesseract" and self.tesseract_ready():
            return "tesseract"
        if eng == "rapidocr" and self.rapid_ready():
            return "rapidocr"
        if eng == "auto":
            if self.tesseract_ready():
                return "tesseract"
            if self.rapid_ready():
                return "rapidocr"
        raise OcrError("Не знайдено OCR-рушій. Встанови: pip install rapidocr-onnxruntime "
                       "(або Tesseract + pytesseract).")


    def ocr_tesseract(self, img, scale: float):
        pytesseract = self._configure_tesseract_cmd()
        if pytesseract is None:
            raise OcrError("pytesseract не встановлено")
        with ocr_slot():
            settings = self.ocr_settings()
            work, sx, sy = fit_image(img, settings)
            payload = io.BytesIO()
            work.convert("L").save(payload, format="PNG")
            # pytesseract has no per-call environment argument. Launch its same TSV
            # command directly so concurrent jobs never mutate os.environ globally.
            environment = os.environ.copy()
            environment["OMP_THREAD_LIMIT"] = str(cpu_threads(settings))
            command = pytesseract.pytesseract.tesseract_cmd or "tesseract"
            flags = ({"creationflags": subprocess.CREATE_NO_WINDOW}
                     if sys.platform.startswith("win") else {})
            timeout = bounded_number(settings, "tesseract_timeout", 8, 2, 30)
            try:
                result = subprocess.run(
                    [command, "stdin", "stdout", "-l", "eng", "--psm", "6", "tsv"],
                    input=payload.getvalue(), capture_output=True, timeout=timeout,
                    env=environment, **flags)
            except subprocess.TimeoutExpired as error:
                raise OcrError(f"Tesseract перевищив тайм-аут {timeout:g} с") from error
            if result.returncode:
                detail = result.stderr.decode("utf-8", "replace").strip()[-300:]
                raise OcrError(f"Tesseract: {detail or 'помилка запуску'}")
            check_cancelled()
            out = []
            rows = csv.DictReader(io.StringIO(result.stdout.decode("utf-8", "replace")),
                                  delimiter="\t", quoting=csv.QUOTE_NONE)
            for row in rows:
                text = (row.get("text") or "").strip()
                if not text:
                    continue
                try:
                    if float(row["conf"]) < 15:
                        continue
                    x, y, w, h = (float(row[key]) for key in ("left", "top", "width", "height"))
                    line = tuple(int(row[key]) for key in ("block_num", "par_num", "line_num"))
                except (KeyError, TypeError, ValueError):
                    continue
                out.append(W(text, x / (scale * sx), y / (scale * sy),
                                     (x + w) / (scale * sx), (y + h) / (scale * sy), line))
            return out


    _char_w = staticmethod(geometry._char_w)


    split_line_words = staticmethod(geometry.split_line_words)


    def _speed_up_rapid(self, eng) -> None:
        """Use the supported detector setting, not its recreated preprocess object."""
        eng.text_det.limit_type = "max"
        eng.max_side_len = max_side(self.ocr_settings())


    def _rapid_engine(self):
        settings = self.ocr_settings()
        key = (cpu_threads(settings), max_side(settings))
        with ocr_slot(), self.rapid_lock:
            if self.rapid_run is None or self.rapid_settings != key:
                try:
                    eng = create_rapid_engine(*key)
                    self._speed_up_rapid(eng)

                    def run(arr):
                        # Validated API of the pinned backend; do not repeat a
                        # failed native invocation through a speculative fallback.
                        res, _ = eng(arr, use_cls=False, text_score=0.3, box_thresh=0.3,
                                     return_word_box=True)
                        # Keep CTC character positions. A whole-line rectangle and
                        # guessed letter widths cannot reliably locate a word.
                        return res or []
                except (ImportError, RuntimeError) as error:
                    raise OcrError(str(error) or
                        "Встанови rapidocr-onnxruntime==1.4.4 для обмеження CPU") from error
                self.rapid_run = run
                self.rapid_settings = key
            return self.rapid_run


    _line_gaps = staticmethod(geometry._line_gaps)


    _gap_segments = staticmethod(geometry._gap_segments)


    _ctc_words = staticmethod(geometry._ctc_words)


    def ocr_rapid(self, img, scale: float):
        with ocr_slot():
            import numpy as np
            work, sx, sy = fit_image(img, self.ocr_settings())
            arr = np.ascontiguousarray(np.array(work.convert("RGB"))[:, :, ::-1])  # RGB -> BGR
            recognized = self._rapid_engine()(arr)
            check_cancelled()
            out = []
            for i, record in enumerate(recognized):
                box, text, _score = record[:3]
                positioned = self._ctc_words(work, record, i)
                if positioned is not None:
                    out += [word._replace(x0=word.x0 / (scale * sx), x1=word.x1 / (scale * sx),
                                          y0=word.y0 / (scale * sy), y1=word.y1 / (scale * sy))
                            for word in positioned]
                    continue
                xs, ys = [p[0] for p in box], [p[1] for p in box]
                tokens = list(re.finditer(r"\S+", text))
                segs = self._gap_segments(work, len(tokens), min(xs), min(ys), max(xs), max(ys))
                if segs is not None:
                    out += [W(m.group(), a / (scale * sx), min(ys) / (scale * sy),
                                      b / (scale * sx), max(ys) / (scale * sy), i)
                            for m, (a, b) in zip(tokens, segs)]
                elif len(tokens) == 1:
                    out.append(W(text, min(xs) / (scale * sx), min(ys) / (scale * sy),
                                         max(xs) / (scale * sx), max(ys) / (scale * sy), i))
                # Multiword lines without valid geometry are deliberately omitted;
                # proportional widths can highlight a different word from the text.
            return out


    pick_word = staticmethod(geometry.pick_word)


    clean_word = staticmethod(geometry.clean_word)


    def _grab(self, x0, y0, x1, y1, frozen=None):
        """Знімок прямокутника екрана (обрізаний по межах усіх моніторів). Якщо передано
        frozen=(img, ox, oy) — уже готовий «стоп-кадр» (напр. з RegionSelector) — вирізає
        з нього, без нового звернення до екрана. Так виділена рамкою підказка, що зникає
        при русі миші (тултіп у грі), лишається на застиглому зображенні."""
        if frozen is not None:
            fimg, fox, foy = frozen
            left, top = max(fox, int(x0)), max(foy, int(y0))
            right = min(fox + fimg.width, int(x1))
            bottom = min(foy + fimg.height, int(y1))
            if right - left < 4 or bottom - top < 4:
                raise OcrError("Область надто мала або поза межами знімка")
            img = fimg.crop((left - fox, top - foy, right - fox, bottom - foy))
            return img, left, top, right, bottom, 1.0, 1.0
        try:
            import mss
            from PIL import Image
        except ImportError:
            raise OcrError("Встанови бібліотеки: pip install mss pillow")
        try:
            with mss.MSS() as sct:
                vs = sct.monitors[0]
                left, top = max(vs["left"], int(x0)), max(vs["top"], int(y0))
                right = min(vs["left"] + vs["width"], int(x1))
                bottom = min(vs["top"] + vs["height"], int(y1))
                if right - left < 4 or bottom - top < 4:
                    raise OcrError("Область надто мала або поза екраном")
                shot = sct.grab({"left": left, "top": top,
                                 "width": right - left, "height": bottom - top})
        except OcrError:
            raise
        except Exception as e:
            raise OcrError(f"Не вдалося зробити знімок екрана: {e}")
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return (img, left, top, right, bottom,
                shot.width / (right - left), shot.height / (bottom - top))


    def _attempts(self, engine, height_px=None):
        """Спроби OCR по черзі (назва, збільшення, інвертувати) — далі йдемо, лише якщо попередня не знайшла."""
        if engine == "tesseract":
            up = (clamp(self.ocr_settings()["upscale"], 1.0, 4.0) if height_px is None
                  else clamp(48 / max(height_px, 1), 1.0, 4.0))
            return [("звичайний", up, False), ("інвертований", up, True)]
        return [("звичайний", 1.0, False), ("інвертований", 1.0, True), ("збільшений", 2.0, False)]


    def _run_ocr(self, engine, img, up, invert):
        from PIL import ImageOps
        from time import perf_counter
        started = perf_counter()
        check_cancelled()
        # Preserve narrow subtitle text: tile a very wide strip rather than shrink
        # its letters below their original height. Full frames use the size cap.
        if img.width > max_side(self.ocr_settings()) and img.height <= 160:
            words = self._ocr_wide(img, engine, invert)
            self.diagnostics.capture_ocr(img, words, engine)
            self.diagnostics.record("ocr.duration", milliseconds=(perf_counter() - started) * 1000)
            return words
        with ocr_slot():
            work, sx, sy = fit_image(img, self.ocr_settings(), up)
            if invert:
                work = ImageOps.invert(work.convert("RGB"))
            words = (self.ocr_tesseract if engine == "tesseract" else self.ocr_rapid)(work, 1.0)
            words = [word._replace(x0=word.x0 / sx, x1=word.x1 / sx,
                                  y0=word.y0 / sy, y1=word.y1 / sy) for word in words]
        self.diagnostics.capture_ocr(img, words, engine)
        self.diagnostics.record("ocr.duration", milliseconds=(perf_counter() - started) * 1000)
        return words


    _find_word_gap = staticmethod(geometry._find_word_gap)


    def _reocr_crop(self, img, box, engine, point=None):
        """Вирізає фрагмент зображення і розпізнає його окремо — для уточнення однієї
        половини слова, яке OCR помилково злив із сусіднім."""
        pad = 4
        x0, y0, x1, y1 = (int(v) for v in box)
        x0, y0 = max(x0 - pad, 0), max(y0 - pad, 0)
        x1, y1 = min(x1 + pad, img.width), min(y1 + pad, img.height)
        if x1 - x0 < 6 or y1 - y0 < 6:
            return None
        crop = img.crop((x0, y0, x1, y1))
        try:
            words = self._run_ocr(engine, crop, 3.0, False)
        except LookupCancelled:
            raise
        except (OcrError, OSError, RuntimeError, ValueError):
            self.logger.exception("Уточнення OCR")
            return None
        if point is not None:
            chosen = self.pick_word(words, point[0] - x0, point[1] - y0, tol=0)
            if chosen is None:
                return None
            text = chosen.text
        elif len(words) == 1:
            text = words[0].text
        else:
            # Joining a crop's words and taking the first one labels the whole crop
            # with that word, even when the pointer is over its last word.
            return None
        if len(re.findall(r"\S+", text)) != 1:
            return None
        return self.clean_word(text)[0] or None


    def _split_merged_word(self, img, hit, cx, engine):
        """Якщо розпізнане «слово» без пробілу насправді два злиті слова (видно проміжок
        на зображенні), повертає уточнений hit лише з тим словом, де курсор.
        None — уточнення не потрібне; False — неоднозначну рамку слід відхилити."""
        if not self.ocr_settings()["split_merged_words"] or len(hit.text) < 5:
            return None
        found = self._line_gaps(img, hit.x0, hit.y0, hit.x1, hit.y1, min_gap_frac=0.26)
        if found is None or not found[2]:
            return None
        first, last, gaps = found
        left, right = first, last
        for start, end in gaps:
            if start <= cx < end:
                return False  # Ambiguous: whitespace must not keep the broad box.
            if end <= cx:
                left = end
            elif start > cx:
                right = start
                break
        side = (left, hit.y0, right, hit.y1)
        refined = self._reocr_crop(img, side, engine, point=(cx, (hit.y0 + hit.y1) / 2))
        if not refined:
            return False  # The broad box contains multiple spans; do not guess.
        if refined.lower() == hit.text.lower():
            return None
        self.logger.write(f"OCR: «{hit.text}» виглядає як злиті слова, уточнено до «{refined}»")
        return hit._replace(text=refined, x0=side[0], x1=side[2])


    def _find_word_at(self, x, y, debug, wide, frozen=None):
        check_cancelled()
        self.last["count"] = 0
        engine = self.pick_engine()
        options = self.ocr_settings()
        rw = int(clamp(options["region_w"], 200, 2000) * (1.8 if wide else 1.0))
        rh = int(clamp(options["region_h"], 40, 600))
        img, left, top, right, bottom, sx, sy = self._grab(x - rw / 2, y - rh / 2, x + rw / 2, y + rh / 2, frozen)
        cx, cy = (x - left) * sx, (y - top) * sy
        if debug is not None:
            debug.update(img=img, cursor=(cx, cy), engine=engine, variants=[],
                         region=(left, top, right, bottom), shot=img.size)
        hit, hit_words = None, []
        try:
            for name, up, invert in self._attempts(engine):
                check_cancelled()
                words = self._run_ocr(engine, img, up, invert)
                self.last["count"] = max(self.last["count"], len(words))
                if debug is not None:
                    debug["variants"].append((name, words))
                if hit is None:
                    cand = [w for w in words if re.search(r"[A-Za-z]", w.text)]
                    found = self.pick_word(cand, cx, cy, tol=0)
                    if found:
                        hit, hit_words = found, words
                if hit is not None and debug is None:
                    break
        except (OcrError, LookupCancelled):
            raise
        except Exception as e:
            self.logger.exception()
            raise OcrError(f"Помилка OCR: {type(e).__name__}: {e}")
        if hit is None:
            return None

        line_words = next((ln for ln in self.cluster_lines(hit_words) if hit in ln), [hit])
        refined = self._split_merged_word(img, hit, cx, engine)
        if refined is False:
            return None
        if refined is not None:
            hit = refined
        word, lead, trail = self.clean_word(hit.text)
        core = hit.text.replace("’", "'").strip(STRIP_CHARS)
        if not word or core.lower() != word.lower():
            # clean_word's permissive text extraction is useful for diagnostics,
            # but its first substring is not a valid label for a whole-line box.
            return None
        wd = hit.x1 - hit.x0
        x0, x1 = hit.x0 + wd * lead, hit.x1 - wd * trail
        if not (x0 <= cx < x1 and hit.y0 <= cy < hit.y1):
            return None
        box = (math.floor(left + x0 / sx), math.floor(top + hit.y0 / sy),
               math.ceil(left + x1 / sx), math.ceil(top + hit.y1 / sy))
        return {
            "word": word,
            "box": box,
            "line_text": " ".join(w.text for w in line_words),
        }


    def find_word_at(self, x: int, y: int, debug=None, wide=False, frozen=None):
        """Знімок екрана навколо (x, y) -> OCR -> слово під курсором.
        debug (dict) — збирає знімок і результати всіх спроб; wide=True — ширша область (для рядка);
        frozen=(img, ox, oy) — не знімати новий скріншот, а взяти вже готовий «стоп-кадр»
        (напр. якщо в грі підказка (тултіп) зникає при русі миші — її вже «заморожено»)."""
        t0 = time.time()
        try:
            return self._find_word_at(x, y, debug, wide, frozen)
        finally:
            self.last["time"] = time.time() - t0


    cluster_lines = staticmethod(geometry.cluster_lines)


    def ocr_region(self, x0, y0, x1, y1, debug=None, frozen=None):
        """OCR виділеної рамки. Повертає слова в екранних координатах у порядку читання.
        frozen=(img, ox, oy) — брати дані з уже готового «стоп-кадру», а не з нового знімка."""
        check_cancelled()
        self.last["count"] = 0
        engine = self.pick_engine()
        m = 6  # невеликий запас навколо рамки — не відрізати краї літер
        img, L, T, R, B, sx, sy = self._grab(x0 - m, y0 - m, x1 + m, y1 + m, frozen)
        sel = ((x0 - L) * sx, (y0 - T) * sy, (x1 - L) * sx, (y1 - T) * sy)
        tol = 3 * max(sx, 1.0)
        inside = []
        try:
            for name, up, invert in self._attempts(engine, (y1 - y0) * sy):
                check_cancelled()
                words = self._run_ocr(engine, img, up, invert)
                self.last["count"] = max(self.last["count"], len(words))
                if debug is not None:
                    debug.setdefault("variants", []).append((name, words))
                inside = [w for w in words
                          if sel[0] - tol <= (w.x0 + w.x1) / 2 <= sel[2] + tol
                          and sel[1] - tol <= (w.y0 + w.y1) / 2 <= sel[3] + tol]
                if inside:
                    break
        except (OcrError, LookupCancelled):
            raise
        except Exception as e:
            self.logger.exception()
            raise OcrError(f"Помилка OCR: {type(e).__name__}: {e}")
        if not inside:
            return []
        if len(inside) == 1:
            merged = inside[0]
            refined = self._split_merged_word(img, merged, (sel[0] + sel[2]) / 2, engine)
            if refined is False:
                return []
            if refined is not None:
                inside = [refined]
        ordered = [w for line in self.cluster_lines(inside) for w in line]
        return [W(w.text, L + w.x0 / sx, T + w.y0 / sy, L + w.x1 / sx, T + w.y1 / sy, w.line)
                for w in ordered]


    _ends_sentence = staticmethod(geometry._ends_sentence)


    _sentence_around = staticmethod(geometry._sentence_around)


    def _ocr_wide(self, img, engine, invert=False):
        """OCR широкої смуги шматками по ≤900 px із перекриттям: RapidOCR зменшує вхід до ~960 px,
        тож по всій ширині дрібний текст псувався б. Слова з перекриття беруться лише з
        того шматка, де вони в «серцевині» (обрізані на краю відкидаються)."""
        check_cancelled()
        from PIL import ImageOps
        work = ImageOps.invert(img.convert("RGB")) if invert else img
        run = self.ocr_tesseract if engine == "tesseract" else self.ocr_rapid
        total = work.width
        chunk = min(900 if engine == "rapidocr" else 1600,
                    max_side(self.ocr_settings()))
        if total <= chunk:
            return run(work, 1.0)
        overlap = 120
        out = []
        for i, st in enumerate(range(0, max(1, total - overlap), chunk - overlap)):
            check_cancelled()
            en = min(total, st + chunk)
            if en - st < 60:
                continue
            lo = st + (overlap // 2 if i > 0 else 0)
            hi = en - (overlap // 2 if en < total else 0)
            for w in run(work.crop((st, 0, en, work.height)), 1.0):
                if lo <= st + (w.x0 + w.x1) / 2 < hi:
                    out.append(w._replace(x0=w.x0 + st, x1=w.x1 + st))
        return out


    _pick_anchor = staticmethod(geometry._pick_anchor)


    def find_sentence(self, hit: dict, frozen=None) -> str:
        """Повне речення, у якому стоїть знайдене слово. Знімає смугу екрана навколо слова на
        повну ширину (±1100 px) і висоту ~2 рядки вгору/вниз, розпізнає її, збирає
        підписи, перенесені на кілька рядків, і обрізає по межах речення. Порожній
        рядок — якщо надійно визначити не вдалось (тоді лишається короткий рядок)."""
        check_cancelled()
        frame_words = hit.get("_frame_words")
        if frame_words is not None:
            # The keyboard/hover selector already recognized this exact frozen
            # frame. Reuse its screen coordinates instead of repeating inference.
            bx0, by0, bx1, by1 = hit["box"]
            height = max(10.0, by1 - by0)
            centre = (by0 + by1) / 2
            nearby = [word for word in frame_words
                      if bx0 - 1100 <= (word.x0 + word.x1) / 2 <= bx1 + 1100
                      and centre - 2.3 * height <= (word.y0 + word.y1) / 2 <= centre + 2.3 * height]
            reused = self._sentence_from_words(nearby, hit)
            if reused is not None:
                return reused
        engine = self.pick_engine()
        bx0, by0, bx1, by1 = hit["box"]
        h = max(10.0, by1 - by0)
        cy = (by0 + by1) / 2
        img, left, top, right, bottom, sx, sy = self._grab(bx0 - 1100, cy - 2.3 * h, bx1 + 1100, cy + 2.3 * h, frozen)
        anchor, screen = None, []
        for invert in (False, True):  # світлий текст на темному і навпаки
            check_cancelled()
            words = [w for w in self._ocr_wide(img, engine, invert) if re.search(r"[A-Za-z0-9]", w.text)]
            screen = [w._replace(x0=left + w.x0 / sx, x1=left + w.x1 / sx,
                                 y0=top + w.y0 / sy, y1=top + w.y1 / sy) for w in words]
            anchor = self._pick_anchor(screen, hit)
            if anchor is not None:
                break
        if anchor is None:
            return ""
        return self._sentence_from_words(screen, hit) or ""


    _sentence_from_words = staticmethod(geometry._sentence_from_words)


    def ocr_settings(self):
        settings = dict(self.settings.data["ocr"])
        profile = current_profile()
        for key in ("region_w", "region_h"):
            if key in profile:
                settings[key] = profile[key]
        return settings

