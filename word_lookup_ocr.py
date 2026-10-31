"""OCR engines, capture geometry, word and sentence recognition.

The runtime supplies configuration, shared state and overridable service adapters.
The entry point reexports this API for existing integrations.
"""
from __future__ import annotations

runtime = None


def configure(application):
    global runtime
    runtime = application


class OcrError(Exception):
    pass


def _default_tesseract() -> str:
    if runtime.sys.platform.startswith("win"):
        for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                  r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
            if runtime.os.path.exists(p):
                return p
    return ""


def _configure_tesseract_cmd():
    """Підключає pytesseract і виставляє шлях до tesseract.exe. Не звертається до
    самого tesseract (без subprocess) — це швидко й безпечно викликати часто."""
    try:
        import pytesseract
    except ImportError:
        return None
    cmd = runtime.cfg["ocr"]["tesseract_cmd"] or runtime._default_tesseract()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    return pytesseract


def tesseract_ready() -> bool:
    """Чи можна реально запустити tesseract. Виконує subprocess із жорстким
    тайм-аутом 2 с, щоб зламаний чи заблокований (антивірусом) шлях ніколи не
    підвісив програму — раніше тут не було тайм-ауту взагалі."""
    pytesseract = runtime._configure_tesseract_cmd()
    if pytesseract is None:
        return False
    cmd = pytesseract.pytesseract.tesseract_cmd or "tesseract"
    try:
        kwargs = {"creationflags": runtime.subprocess.CREATE_NO_WINDOW} if runtime.sys.platform.startswith("win") else {}
        runtime.subprocess.run([cmd, "--version"], capture_output=True, timeout=2, **kwargs)
        return True
    except Exception:
        return False


def rapid_ready() -> bool:
    return any(runtime.importlib.util.find_spec(m) for m in ("rapidocr_onnxruntime", "rapidocr"))


def pick_engine() -> str:
    """Вибір рушія; результат кешується на хвилину (перевірка Tesseract запускає процес)."""
    key = (runtime.cfg["ocr"]["engine"], runtime.cfg["ocr"]["tesseract_cmd"])
    if runtime._engine_cache["key"] == key and runtime.time.time() - runtime._engine_cache["t"] < 60:
        return runtime._engine_cache["val"]
    val = runtime._pick_engine_uncached()
    runtime._engine_cache.update(key=key, val=val, t=runtime.time.time())
    return val


def _pick_engine_uncached() -> str:
    eng = runtime.cfg["ocr"]["engine"]
    if eng == "tesseract" and runtime.tesseract_ready():
        return "tesseract"
    if eng == "rapidocr" and runtime.rapid_ready():
        return "rapidocr"
    if eng == "auto":
        if runtime.tesseract_ready():
            return "tesseract"
        if runtime.rapid_ready():
            return "rapidocr"
    raise runtime.OcrError("Не знайдено OCR-рушій. Встанови: pip install rapidocr-onnxruntime "
                   "(або Tesseract + pytesseract).")


def ocr_tesseract(img, scale: float):
    pytesseract = runtime._configure_tesseract_cmd()
    if pytesseract is None:
        raise runtime.OcrError("pytesseract не встановлено")
    data = pytesseract.image_to_data(img.convert("L"), lang="eng", config="--psm 6",
                                     output_type=pytesseract.Output.DICT)
    out = []
    for i, t in enumerate(data["text"]):
        t = (t or "").strip()
        if not t:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = 0
        if conf < 15:
            continue
        x, y, w, h = (data[k][i] for k in ("left", "top", "width", "height"))
        line = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        out.append(runtime.W(t, x / scale, y / scale, (x + w) / scale, (y + h) / scale, line))
    return out


def _char_w(ch: str) -> float:
    if ch in runtime._NARROW:
        return 0.45
    if ch in runtime._WIDE:
        return 1.5
    return 1.15 if ch.isupper() else 1.0


def split_line_words(text, x0, y0, x1, y1, line_id):
    """Ділить рядок OCR на слова пропорційно ширині символів."""
    tokens = list(runtime.re.finditer(r"\S+", text))
    if not tokens:
        return []
    cum = [0.0]
    for ch in text:
        cum.append(cum[-1] + runtime._char_w(ch))
    k = (x1 - x0) / (cum[-1] or 1)
    return [runtime.W(m.group(), x0 + cum[m.start()] * k, y0, x0 + cum[m.end()] * k, y1, line_id)
            for m in tokens]


def _speed_up_rapid(eng) -> None:
    """Детекція без багаторазового збільшення знімка — набагато швидше (best-effort)."""
    try:
        pre = eng.text_det.preprocess_op
        pre.limit_type, pre.limit_side_len = "max", 960
        runtime.log("RapidOCR: швидка детекція увімкнена")
    except Exception:
        runtime.log("RapidOCR: швидку детекцію ввімкнути не вдалося (працюватиме повільніше)")


def _rapid_engine():
    with runtime._rapid_lock:
        if runtime._rapid_run is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
                eng = RapidOCR()
                runtime._speed_up_rapid(eng)

                def run(arr):
                    try:  # трохи нижчі пороги — краще ловить ігровий текст
                        res, _ = eng(arr, use_cls=False, text_score=0.3, box_thresh=0.3)
                    except TypeError:
                        res, _ = eng(arr)
                    return [(b, t, s) for b, t, s in (res or [])]
            except ImportError:
                from rapidocr import RapidOCR
                eng = RapidOCR()
                runtime._speed_up_rapid(eng)

                def run(arr):
                    try:
                        out = eng(arr, use_cls=False)
                    except TypeError:
                        out = eng(arr)
                    if out is None or getattr(out, "boxes", None) is None:
                        return []
                    return list(zip(out.boxes, out.txts, out.scores))
            runtime._rapid_run = run
        return runtime._rapid_run


def _gap_segments(img, n, x0, y0, x1, y1, min_gap_frac=0.2):
    """Межі n слів усередині рядка за РЕАЛЬНИМИ проміжками на зображенні (стовпці без
    «чорнила»), а не за оцінкою ширини літер — та накопичує похибку вздовж довгого
    рядка. Повертає [(x_початок, x_кінець), ...] у координатах img або None, якщо
    надійно визначити не вдалось (складний фон, замало проміжків) — тоді
    викликач лишає стару пропорційну оцінку."""
    if n < 2:
        return None
    try:
        import numpy as np
    except ImportError:
        return None
    X0, Y0 = max(int(x0), 0), max(int(y0), 0)
    X1, Y1 = min(int(x1) + 1, img.width), min(int(y1) + 1, img.height)
    if X1 - X0 < 2 * n or Y1 - Y0 < 4:
        return None
    arr = np.asarray(img.crop((X0, Y0, X1, Y1)).convert("L"), dtype=np.float32)
    # «чорнило» стовпця = розкид яскравості (макс − мін): не залежить від кольору фону і
    # не ламається, коли жирний текст займає понад половину рамки (медіана тоді
    # стала б кольором самого тексту й усе інвертувалось)
    ink = arr.max(axis=0) - arr.min(axis=0)
    if ink.max() < 8:  # майже немає контрасту — не довіряємо
        return None
    is_bg = ink <= max(ink.max() * 0.12, 2.0)
    cols = np.flatnonzero(~is_bg)
    if len(cols) == 0:
        return None
    first, last = int(cols[0]), int(cols[-1])
    min_gap = max(2, int((Y1 - Y0) * min_gap_frac))
    runs, i = [], first
    while i <= last:
        if is_bg[i]:
            j = i
            while j <= last and is_bg[j]:
                j += 1
            if j - i >= min_gap:
                runs.append((i, j))
            i = j
        else:
            i += 1
    if len(runs) < n - 1:
        return None
    widest = sorted(sorted(runs, key=lambda r: r[1] - r[0], reverse=True)[:n - 1])
    bounds, start = [], first
    for a, b in widest:
        bounds.append((start, a))
        start = b
    bounds.append((start, last + 1))
    return [(X0 + a, X0 + b) for a, b in bounds]


def ocr_rapid(img, scale: float):
    import numpy as np
    arr = np.ascontiguousarray(np.array(img.convert("RGB"))[:, :, ::-1])  # RGB -> BGR
    out = []
    for i, (box, text, _score) in enumerate(runtime._rapid_engine()(arr)):
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        tokens = list(runtime.re.finditer(r"\S+", text))
        segs = runtime._gap_segments(img, len(tokens), min(xs), min(ys), max(xs), max(ys))
        if segs is not None:
            out += [runtime.W(m.group(), a / scale, min(ys) / scale, b / scale, max(ys) / scale, i)
                    for m, (a, b) in zip(tokens, segs)]
        else:  # запасний варіант: пропорційна оцінка за шириною літер
            out += runtime.split_line_words(text, min(xs) / scale, min(ys) / scale,
                                    max(xs) / scale, max(ys) / scale, i)
    return out


def pick_word(words, cx, cy, tol):
    """Слово, що містить точку (cx, cy) або найближче до неї в межах tol."""
    best, best_d = None, 1e9
    for wd in words:
        dx = max(wd.x0 - cx, 0, cx - wd.x1)
        dy = max(wd.y0 - cy, 0, cy - wd.y1)
        d = runtime.math.hypot(dx, dy)
        if d < best_d:
            best, best_d = wd, d
    return best if best_d <= tol else None


def clean_word(raw: str):
    """Повертає (слово, частка_початку, частка_кінця) без пунктуації навколо."""
    t = raw.replace("’", "'")
    lead = len(t) - len(t.lstrip(runtime.STRIP_CHARS))
    trail = len(t) - len(t.rstrip(runtime.STRIP_CHARS))
    core = t.strip(runtime.STRIP_CHARS)
    if not runtime.WORD_RE.fullmatch(core):
        m = runtime.re.search(r"[A-Za-z][A-Za-z'-]*", core)
        core = m.group(0).strip("'-") if m else ""
    n = max(len(t), 1)
    return core, lead / n, trail / n


def _grab(x0, y0, x1, y1, frozen=None):
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
            raise runtime.OcrError("Область надто мала або поза межами знімка")
        img = fimg.crop((left - fox, top - foy, right - fox, bottom - foy))
        return img, left, top, right, bottom, 1.0, 1.0
    try:
        import mss
        from PIL import Image
    except ImportError:
        raise runtime.OcrError("Встанови бібліотеки: pip install mss pillow")
    try:
        with mss.mss() as sct:
            vs = sct.monitors[0]
            left, top = max(vs["left"], int(x0)), max(vs["top"], int(y0))
            right = min(vs["left"] + vs["width"], int(x1))
            bottom = min(vs["top"] + vs["height"], int(y1))
            if right - left < 4 or bottom - top < 4:
                raise runtime.OcrError("Область надто мала або поза екраном")
            shot = sct.grab({"left": left, "top": top,
                             "width": right - left, "height": bottom - top})
    except runtime.OcrError:
        raise
    except Exception as e:
        raise runtime.OcrError(f"Не вдалося зробити знімок екрана: {e}")
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return (img, left, top, right, bottom,
            shot.width / (right - left), shot.height / (bottom - top))


def _attempts(engine, height_px=None):
    """Спроби OCR по черзі (назва, збільшення, інвертувати) — далі йдемо, лише якщо попередня не знайшла."""
    if engine == "tesseract":
        up = (runtime.clamp(runtime.cfg["ocr"]["upscale"], 1.0, 4.0) if height_px is None
              else runtime.clamp(48 / max(height_px, 1), 1.0, 4.0))
        return [("звичайний", up, False), ("інвертований", up, True)]
    return [("звичайний", 1.0, False), ("інвертований", 1.0, True), ("збільшений", 2.0, False)]


def _run_ocr(engine, img, up, invert):
    from PIL import Image, ImageOps
    work = img
    if up != 1.0:
        work = work.resize((max(1, int(img.width * up)), max(1, int(img.height * up))),
                           Image.LANCZOS)
    if invert:
        work = ImageOps.invert(work.convert("RGB"))
    return (runtime.ocr_tesseract if engine == "tesseract" else runtime.ocr_rapid)(work, up)


def _find_word_gap(img, box, min_gap_frac=0.26):
    """Шукає в межах box найширшу вертикальну прогалину — можливий пробіл, який OCR
    об'єднав із сусіднім словом. Повертає x-координату розриву (в межах img) або None."""
    x0, y0, x1, y1 = (int(v) for v in box)
    x0, y0 = max(x0, 0), max(y0, 0)
    x1, y1 = min(x1, img.width), min(y1, img.height)
    if x1 - x0 < 14 or y1 - y0 < 5:
        return None
    try:
        import numpy as np
    except ImportError:
        return None
    arr = np.asarray(img.crop((x0, y0, x1, y1)).convert("L"), dtype=np.float32)
    ink = arr.max(axis=0) - arr.min(axis=0)  # розкид яскравості по стовпцях (див. _gap_segments)
    if ink.max() < 8:  # надто мало контрасту — не варто довіряти
        return None
    is_bg = ink <= max(ink.max() * 0.12, 2.0)
    h, n = y1 - y0, len(is_bg)
    min_gap = max(3, int(h * min_gap_frac))
    runs, i = [], 0
    while i < n:
        if is_bg[i]:
            j = i
            while j < n and is_bg[j]:
                j += 1
            if j - i >= min_gap and i > 2 and j < n - 2:
                runs.append((i, j))
            i = j
        else:
            i += 1
    if not runs:
        return None
    a, b = max(runs, key=lambda ab: ab[1] - ab[0])
    return x0 + (a + b) / 2


def _reocr_crop(img, box, engine):
    """Вирізає фрагмент зображення і розпізнає його окремо — для уточнення однієї
    половини слова, яке OCR помилково злив із сусіднім."""
    from PIL import Image
    pad = 4
    x0, y0, x1, y1 = (int(v) for v in box)
    x0, y0 = max(x0 - pad, 0), max(y0 - pad, 0)
    x1, y1 = min(x1 + pad, img.width), min(y1 + pad, img.height)
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None
    up = 3.0
    crop = img.crop((x0, y0, x1, y1))
    big = crop.resize((max(1, int(crop.width * up)), max(1, int(crop.height * up))), Image.LANCZOS)
    try:
        words = (runtime.ocr_tesseract if engine == "tesseract" else runtime.ocr_rapid)(big, up)
    except Exception:
        return None
    text = " ".join(w.text for w in words).strip()
    return runtime.clean_word(text)[0] or None


def _split_merged_word(img, hit, cx, engine):
    """Якщо розпізнане «слово» без пробілу насправді два злиті слова (видно проміжок
    на зображенні), повертає уточнений hit лише з тим словом, де курсор. Інакше — None."""
    if not runtime.cfg["ocr"]["split_merged_words"] or " " in hit.text or len(hit.text) < 5:
        return None
    gap_x = runtime._find_word_gap(img, (hit.x0, hit.y0, hit.x1, hit.y1))
    if gap_x is None:
        return None
    side = (hit.x0, hit.y0, gap_x, hit.y1) if cx < gap_x else (gap_x, hit.y0, hit.x1, hit.y1)
    refined = runtime._reocr_crop(img, side, engine)
    if not refined or refined.lower() == hit.text.lower():
        return None
    runtime.log(f"OCR: «{hit.text}» виглядає як злиті слова, уточнено до «{refined}»")
    return hit._replace(text=refined, x0=side[0], x1=side[2])


def _find_word_at(x, y, debug, wide, frozen=None):
    runtime.LAST_OCR["count"] = 0
    engine = runtime.pick_engine()
    O = runtime.cfg["ocr"]
    rw = int(runtime.clamp(O["region_w"], 200, 2000) * (1.8 if wide else 1.0))
    rh = int(runtime.clamp(O["region_h"], 40, 600))
    img, left, top, right, bottom, sx, sy = runtime._grab(x - rw / 2, y - rh / 2, x + rw / 2, y + rh / 2, frozen)
    cx, cy = (x - left) * sx, (y - top) * sy
    if debug is not None:
        debug.update(img=img, cursor=(cx, cy), engine=engine, variants=[],
                     region=(left, top, right, bottom), shot=img.size)
    hit, hit_words = None, []
    try:
        for name, up, invert in runtime._attempts(engine):
            words = runtime._run_ocr(engine, img, up, invert)
            runtime.LAST_OCR["count"] = max(runtime.LAST_OCR["count"], len(words))
            if debug is not None:
                debug["variants"].append((name, words))
            if hit is None:
                cand = [w for w in words if runtime.re.search(r"[A-Za-z]", w.text)]
                found = runtime.pick_word(cand, cx, cy, tol=14 * max(sx, 1.0))
                if found:
                    hit, hit_words = found, words
            if hit is not None and debug is None:
                break
    except runtime.OcrError:
        raise
    except Exception as e:
        runtime.log_exc()
        raise runtime.OcrError(f"Помилка OCR: {type(e).__name__}: {e}")
    if hit is None:
        return None

    line_words = next((ln for ln in runtime.cluster_lines(hit_words) if hit in ln), [hit])
    refined = runtime._split_merged_word(img, hit, cx, engine)
    if refined is not None:
        hit = refined
    word, lead, trail = runtime.clean_word(hit.text)
    if not word:
        return None
    wd = hit.x1 - hit.x0
    x0, x1 = hit.x0 + wd * lead, hit.x1 - wd * trail
    return {
        "word": word,
        "box": (int(left + x0 / sx), int(top + hit.y0 / sy),
                int(left + x1 / sx), int(top + hit.y1 / sy)),
        "line_text": " ".join(w.text for w in line_words),
    }


def find_word_at(x: int, y: int, debug=None, wide=False, frozen=None):
    """Знімок екрана навколо (x, y) -> OCR -> слово під курсором.
    debug (dict) — збирає знімок і результати всіх спроб; wide=True — ширша область (для рядка);
    frozen=(img, ox, oy) — не знімати новий скріншот, а взяти вже готовий «стоп-кадр»
    (напр. якщо в грі підказка (тултіп) зникає при русі миші — її вже «заморожено»)."""
    t0 = runtime.time.time()
    try:
        return runtime._find_word_at(x, y, debug, wide, frozen)
    finally:
        runtime.LAST_OCR["time"] = runtime.time.time() - t0


def cluster_lines(words):
    """Групує слова у рядки за фактичним вертикальним розташуванням (перекриттям
    по Y центру й висоті), а НЕ за 'id рядка' з самого OCR. RapidOCR часто дає
    кожному виявленому текстовому фрагменту власний id, навіть якщо він візуально
    на одному рядку з сусідніми (напр. "Slowdive", "·", "Sleep (2022 Remastered)")
    — тоді групування за id розбиває один рядок на кілька й порядок слів ламається.
    Повертає список рядків (згори вниз), кожен — список слів (зліва направо)."""
    if not words:
        return []
    items = sorted(words, key=lambda w: (w.y0 + w.y1) / 2)
    lines = []
    for w in items:
        wc = (w.y0 + w.y1) / 2
        wh = max(w.y1 - w.y0, 1)
        placed = False
        for line in lines:
            lc = sum((x.y0 + x.y1) / 2 for x in line) / len(line)
            lh = sum(x.y1 - x.y0 for x in line) / len(line)
            if abs(wc - lc) <= max(lh, wh) * 0.55:  # достатньо близько по вертикалі -> той самий рядок
                line.append(w)
                placed = True
                break
        if not placed:
            lines.append([w])
    lines.sort(key=lambda line: min(x.y0 for x in line))
    return [sorted(line, key=lambda w: w.x0) for line in lines]


def ocr_region(x0, y0, x1, y1, debug=None, frozen=None):
    """OCR виділеної рамки. Повертає слова в екранних координатах у порядку читання.
    frozen=(img, ox, oy) — брати дані з уже готового «стоп-кадру», а не з нового знімка."""
    runtime.LAST_OCR["count"] = 0
    engine = runtime.pick_engine()
    m = 6  # невеликий запас навколо рамки — не відрізати краї літер
    img, L, T, R, B, sx, sy = runtime._grab(x0 - m, y0 - m, x1 + m, y1 + m, frozen)
    sel = ((x0 - L) * sx, (y0 - T) * sy, (x1 - L) * sx, (y1 - T) * sy)
    tol = 3 * max(sx, 1.0)
    inside = []
    try:
        for name, up, invert in runtime._attempts(engine, (y1 - y0) * sy):
            words = runtime._run_ocr(engine, img, up, invert)
            runtime.LAST_OCR["count"] = max(runtime.LAST_OCR["count"], len(words))
            if debug is not None:
                debug.setdefault("variants", []).append((name, words))
            inside = [w for w in words
                      if sel[0] - tol <= (w.x0 + w.x1) / 2 <= sel[2] + tol
                      and sel[1] - tol <= (w.y0 + w.y1) / 2 <= sel[3] + tol]
            if inside:
                break
    except runtime.OcrError:
        raise
    except Exception as e:
        runtime.log_exc()
        raise runtime.OcrError(f"Помилка OCR: {type(e).__name__}: {e}")
    if not inside:
        return []
    if len(inside) == 1:
        merged = inside[0]
        refined = runtime._split_merged_word(img, merged, (sel[0] + sel[2]) / 2, engine)
        if refined is not None:
            inside = [refined]
    ordered = [w for line in runtime.cluster_lines(inside) for w in line]
    return [runtime.W(w.text, L + w.x0 / sx, T + w.y0 / sy, L + w.x1 / sx, T + w.y1 / sy, w.line)
            for w in ordered]


def _ends_sentence(tok: str) -> bool:
    """Токен завершує речення: .!? (з можливими лапками/дужками після), але не «Dr.»
    і не три крапки (пауза в субтитрах — речення триває)."""
    t = tok.rstrip("\"'”’»)]}")
    if not t or t[-1] not in ".!?" or t.endswith(("...", "…")):
        return False
    return not (t.endswith(".") and t[:-1].lower() in runtime._ABBR)


def _sentence_around(tokens: list, idx: int, max_tokens: int = 40) -> str:
    """Речення, що містить токен idx. tokens — список рядків."""
    start = idx
    while start > 0 and idx - start < max_tokens and not runtime._ends_sentence(tokens[start - 1]):
        start -= 1
    end = idx
    while end < len(tokens) - 1 and end - idx < max_tokens and not runtime._ends_sentence(tokens[end]):
        end += 1
    seg = tokens[start:end + 1]
    if len(seg) > 1 and seg[0].endswith(":") and len(seg[0]) <= 16:  # «Evelyn:» — не частина речення
        seg = seg[1:]
    return " ".join(seg)


def _ocr_wide(img, engine, invert=False):
    """OCR широкої смуги шматками по ≤900 px із перекриттям: RapidOCR зменшує вхід до ~960 px,
    тож по всій ширині дрібний текст псувався б. Слова з перекриття беруться лише з
    того шматка, де вони в «серцевині» (обрізані на краю відкидаються)."""
    from PIL import ImageOps
    work = ImageOps.invert(img.convert("RGB")) if invert else img
    run = runtime.ocr_tesseract if engine == "tesseract" else runtime.ocr_rapid
    total = work.width
    if engine == "tesseract" or total <= 960:
        return run(work, 1.0)
    chunk, overlap = 900, 120
    out = []
    for i, st in enumerate(range(0, max(1, total - overlap), chunk - overlap)):
        en = min(total, st + chunk)
        if en - st < 60:
            continue
        lo = st + (overlap // 2 if i > 0 else 0)
        hi = en - (overlap // 2 if en < total else 0)
        for w in run(work.crop((st, 0, en, work.height)), 1.0):
            if lo <= st + (w.x0 + w.x1) / 2 < hi:
                out.append(w._replace(x0=w.x0 + st, x1=w.x1 + st))
    return out


def _pick_anchor(words: list, hit: dict):
    """Слово зі смуги, що відповідає знайденому слову (за текстом і близькістю)."""
    bx0, by0, bx1, by1 = hit["box"]
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    target = hit["word"].lower()
    best, best_d = None, 1e9
    for w in words:
        d = runtime.math.hypot((w.x0 + w.x1) / 2 - cx, (w.y0 + w.y1) / 2 - cy)
        if runtime.clean_word(w.text)[0].lower() == target and d < best_d and d <= max(60, 2 * (by1 - by0)):
            best, best_d = w, d
    if best:
        return best
    inside = [w for w in words if w.x0 <= cx <= w.x1 and w.y0 <= cy <= w.y1]
    return inside[0] if inside else None


def find_sentence(hit: dict, frozen=None) -> str:
    """Повне речення, у якому стоїть знайдене слово. Знімає смугу екрана навколо слова на
    повну ширину (±1100 px) і висоту ~2 рядки вгору/вниз, розпізнає її, збирає
    підписи, перенесені на кілька рядків, і обрізає по межах речення. Порожній
    рядок — якщо надійно визначити не вдалось (тоді лишається короткий рядок)."""
    engine = runtime.pick_engine()
    bx0, by0, bx1, by1 = hit["box"]
    h = max(10.0, by1 - by0)
    cy = (by0 + by1) / 2
    img, left, top, right, bottom, sx, sy = runtime._grab(bx0 - 1100, cy - 2.3 * h, bx1 + 1100, cy + 2.3 * h, frozen)
    anchor, screen = None, []
    for invert in (False, True):  # світлий текст на темному і навпаки
        words = [w for w in runtime._ocr_wide(img, engine, invert) if runtime.re.search(r"[A-Za-z0-9]", w.text)]
        screen = [w._replace(x0=left + w.x0 / sx, x1=left + w.x1 / sx,
                             y0=top + w.y0 / sy, y1=top + w.y1 / sy) for w in words]
        anchor = runtime._pick_anchor(screen, hit)
        if anchor is not None:
            break
    if anchor is None:
        return ""
    lines = runtime.cluster_lines(screen)
    ai = next(i for i, ln in enumerate(lines) if anchor in ln)

    def height(ln): return sum(w.y1 - w.y0 for w in ln) / len(ln)
    def centre(ln): return sum((w.y0 + w.y1) / 2 for w in ln) / len(ln)
    def near(a, b): return abs(centre(a) - centre(b)) <= 1.9 * height(a) and 0.6 <= height(b) / height(a) <= 1.7
    def starts_lower(ln): return ln[0].text[:1].islower()
    lo = hi = ai
    # рядок продовжує речення, лише якщо попередній не закінчився крапкою, а цей починається
    # з малої літери — так пункти меню («RESUME GAME / OPTIONS») не склеюються в «речення»
    while lo > 0 and near(lines[lo], lines[lo - 1]) and starts_lower(lines[lo]) \
            and not runtime._ends_sentence(lines[lo - 1][-1].text):
        lo -= 1
    while hi < len(lines) - 1 and near(lines[hi], lines[hi + 1]) and starts_lower(lines[hi + 1]) \
            and not runtime._ends_sentence(lines[hi][-1].text):
        hi += 1
    ordered = [w for ln in lines[lo:hi + 1] for w in ln]
    idx = next(i for i, w in enumerate(ordered) if w is anchor)
    sentence = runtime._sentence_around([w.text for w in ordered], idx)
    return sentence if len(sentence.split()) >= 2 else ""
