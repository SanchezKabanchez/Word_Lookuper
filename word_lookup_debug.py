import os
def diag_report(settings, dbg: dict, hit, err, pos):
    """Текстовий звіт + шлях до знімка з рамками розпізнаних слів."""
    lines = [f"Курсор: {pos}"]
    path = None
    if err:
        lines.append(err)
    if dbg.get("img") is not None:
        lines.append(f"Рушій: {dbg['engine']}; знімок: {dbg['shot'][0]}×{dbg['shot'][1]} px")
        if "time" in dbg:
            lines.append(f"Час OCR (усі спроби): {dbg['time']:.2f} с")
        try:
            from PIL import ImageDraw
            img = dbg["img"].copy()
            d = ImageDraw.Draw(img)
            colors = ["#00c000", "#ff8800"]
            for i, (name, words) in enumerate(dbg["variants"]):
                sample = (": " + " ".join(w.text for w in words[:20])) if words else ""
                lines.append(f"{name}: розпізнано слів — {len(words)}{sample}")
                for w in words:
                    d.rectangle((w.x0, w.y0, w.x1, w.y1), outline=colors[i % 2], width=2)
            cx, cy = dbg["cursor"]
            d.line((cx - 12, cy, cx + 12, cy), fill="#ff0000", width=2)
            d.line((cx, cy - 12, cx, cy + 12), fill="#ff0000", width=2)
            path = os.path.join(os.path.dirname(settings.path()), "ocr_debug.png")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            img.save(path)
            if all(len(w) == 0 for _, w in dbg["variants"]):
                lines.append("OCR нічого не розпізнав. Відкрий знімок: якщо він чорний — гра в "
                             "повноекранному режимі (потрібен borderless); якщо текст видно — "
                             "збільш «Збільшення знімка» до 3 або спробуй Tesseract.")
        except Exception as e:
            lines.append(f"Не вдалося зберегти знімок: {e}")
    lines.append(f"Слово під курсором: {hit['word']}" if hit else "Слово під курсором: НЕ знайдено")
    return "\n".join(lines), path
