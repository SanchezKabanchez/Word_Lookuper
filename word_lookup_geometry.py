"""Stateless OCR word geometry and sentence boundaries."""
import math
import re
from collections import namedtuple
from wordlookup.network.budget import check_cancelled
W = namedtuple("W", "text x0 y0 x1 y1 line")
STRIP_CHARS = " \t\n.,;:!?\"'()[]{}<>“”‘’«»…—–*_/\\|"
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")
_NARROW = set("iljtfIr.,;:'\"!|()[]1 ")
_WIDE = set("mwMW@")
_ABBR = {"mr", "mrs", "ms", "dr", "st", "vs", "jr", "sr", "prof", "mt"}


def _char_w(ch: str) -> float:
    if ch in _NARROW:
        return 0.45
    if ch in _WIDE:
        return 1.5
    return 1.15 if ch.isupper() else 1.0



def split_line_words(text, x0, y0, x1, y1, line_id):
    """Ділить рядок OCR на слова пропорційно ширині символів."""
    tokens = list(re.finditer(r"\S+", text))
    if not tokens:
        return []
    cum = [0.0]
    for ch in text:
        cum.append(cum[-1] + _char_w(ch))
    k = (x1 - x0) / (cum[-1] or 1)
    return [W(m.group(), x0 + cum[m.start()] * k, y0, x0 + cum[m.end()] * k, y1, line_id)
            for m in tokens]



def _line_gaps(img, x0, y0, x1, y1, min_gap_frac=0.2):
    """Visible word-size whitespace, in image coordinates; no word-count guess."""
    try:
        import numpy as np
    except ImportError:
        return None
    X0, Y0 = max(int(x0), 0), max(int(y0), 0)
    X1, Y1 = min(int(x1) + 1, img.width), min(int(y1) + 1, img.height)
    if X1 - X0 < 4 or Y1 - Y0 < 4:
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
    return X0 + first, X0 + last + 1, [(X0 + a, X0 + b) for a, b in runs]



def _gap_segments(img, n, x0, y0, x1, y1, min_gap_frac=0.2):
    """Use image gaps only when they agree with the recognized word count."""
    if n < 2:
        return None
    found = _line_gaps(img, x0, y0, x1, y1, min_gap_frac)
    if found is None:
        return None
    first, last, runs = found
    if len(runs) != n - 1:
        # Choosing just the widest gaps silently shifts labels when OCR omits
        # a word/space. Ambiguous geometry must not select a neighbouring word.
        return None
    bounds, start = [], first
    for a, b in runs:
        bounds.append((start, a))
        start = b
    bounds.append((start, last))
    return bounds



def _ctc_words(img, record, line_id):
    """Locate tokens using recognizer positions, including OCR-dropped spaces."""
    if len(record) < 5:
        return None
    box, text, _score, boxes, characters = record[:5]
    if len(boxes) != len(characters) or "".join(characters) != text:
        return None
    entries = []
    for char, points in zip(characters, boxes):
        if not points:
            return None
        xs, ys = [point[0] for point in points], [point[1] for point in points]
        entries.append((char, min(xs), min(ys), max(xs), max(ys)))
    if not entries:
        return []
    xs, ys = [point[0] for point in box], [point[1] for point in box]
    found = _line_gaps(img, min(xs), min(ys), max(xs), max(ys))
    gaps = found[2] if found else []
    visible = [entry for entry in entries if not entry[0].isspace()]
    advances = [(right[1] + right[3] - left[1] - left[3]) / 2
                for left, right in zip(visible, visible[1:])]
    positive = sorted(value for value in advances if value > 0)
    typical = positive[len(positive) // 2] if positive else 1
    groups, current = [], []
    for entry in entries:
        if entry[0].isspace():
            if current:
                groups.append(current)
                current = []
            continue
        if current:
            previous = current[-1]
            left = (previous[1] + previous[3]) / 2
            right = (entry[1] + entry[3]) / 2
            # A missing OCR space needs both physical whitespace and a larger
            # recognizer advance. Letter kerning alone cannot create a break.
            if right - left >= 1.35 * typical and any(
                    left < (a + b) / 2 < right for a, b in gaps):
                groups.append(current)
                current = []
        current.append(entry)
    if current:
        groups.append(current)
    return [W("".join(entry[0] for entry in group),
                      min(entry[1] for entry in group), min(entry[2] for entry in group),
                      max(entry[3] for entry in group), max(entry[4] for entry in group), line_id)
            for group in groups]



def pick_word(words, cx, cy, tol):
    """Select one containing box; whitespace and overlapping boxes are ambiguous.

    The legacy tol parameter remains accepted for integrations. It no longer
    turns a nearby word into a purported word under the cursor.
    """
    inside = [word for word in words if word.x0 <= cx < word.x1 and word.y0 <= cy < word.y1]
    return inside[0] if len(inside) == 1 else None



def clean_word(raw: str):
    """Повертає (слово, частка_початку, частка_кінця) без пунктуації навколо."""
    t = raw.replace("’", "'")
    lead = len(t) - len(t.lstrip(STRIP_CHARS))
    trail = len(t) - len(t.rstrip(STRIP_CHARS))
    core = t.strip(STRIP_CHARS)
    if not WORD_RE.fullmatch(core):
        m = re.search(r"[A-Za-z][A-Za-z'-]*", core)
        core = m.group(0).strip("'-") if m else ""
    n = max(len(t), 1)
    return core, lead / n, trail / n



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



def _ends_sentence(tok: str) -> bool:
    """Токен завершує речення: .!? (з можливими лапками/дужками після), але не «Dr.»
    і не три крапки (пауза в субтитрах — речення триває)."""
    t = tok.rstrip("\"'”’»)]}")
    if not t or t[-1] not in ".!?" or t.endswith(("...", "…")):
        return False
    return not (t.endswith(".") and t[:-1].lower() in _ABBR)



def _sentence_around(tokens: list, idx: int, max_tokens: int = 40) -> str:
    """Речення, що містить токен idx. tokens — список рядків."""
    start = idx
    while start > 0 and idx - start < max_tokens and not _ends_sentence(tokens[start - 1]):
        start -= 1
    end = idx
    while end < len(tokens) - 1 and end - idx < max_tokens and not _ends_sentence(tokens[end]):
        end += 1
    seg = tokens[start:end + 1]
    if len(seg) > 1 and seg[0].endswith(":") and len(seg[0]) <= 16:  # «Evelyn:» — не частина речення
        seg = seg[1:]
    return " ".join(seg)



def _pick_anchor(words: list, hit: dict):
    """Слово зі смуги, що відповідає знайденому слову (за текстом і близькістю)."""
    bx0, by0, bx1, by1 = hit["box"]
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    target = hit["word"].lower()
    best, best_d = None, 1e9
    for w in words:
        d = math.hypot((w.x0 + w.x1) / 2 - cx, (w.y0 + w.y1) / 2 - cy)
        if clean_word(w.text)[0].lower() == target and d < best_d and d <= max(60, 2 * (by1 - by0)):
            best, best_d = w, d
    if best:
        return best
    inside = [w for w in words if w.x0 <= cx <= w.x1 and w.y0 <= cy <= w.y1]
    return inside[0] if inside else None



def _sentence_from_words(screen, hit):
    """None means missing anchor; an empty sentence needs no repeated OCR."""
    check_cancelled()
    anchor = _pick_anchor(screen, hit)
    if anchor is None:
        return None
    lines = cluster_lines(screen)
    ai = next(i for i, ln in enumerate(lines) if anchor in ln)

    def height(ln): return sum(w.y1 - w.y0 for w in ln) / len(ln)
    def centre(ln): return sum((w.y0 + w.y1) / 2 for w in ln) / len(ln)
    def near(a, b): return abs(centre(a) - centre(b)) <= 1.9 * height(a) and 0.6 <= height(b) / height(a) <= 1.7
    def starts_lower(ln): return ln[0].text[:1].islower()
    lo = hi = ai
    # рядок продовжує речення, лише якщо попередній не закінчився крапкою, а цей починається
    # з малої літери — так пункти меню («RESUME GAME / OPTIONS») не склеюються в «речення»
    while lo > 0 and near(lines[lo], lines[lo - 1]) and starts_lower(lines[lo]) \
            and not _ends_sentence(lines[lo - 1][-1].text):
        lo -= 1
    while hi < len(lines) - 1 and near(lines[hi], lines[hi + 1]) and starts_lower(lines[hi + 1]) \
            and not _ends_sentence(lines[hi][-1].text):
        hi += 1
    ordered = [w for ln in lines[lo:hi + 1] for w in ln]
    idx = next(i for i, w in enumerate(ordered) if w is anchor)
    sentence = _sentence_around([w.text for w in ordered], idx)
    return sentence if len(sentence.split()) >= 2 else ""

