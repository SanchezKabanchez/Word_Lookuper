from __future__ import annotations
import tkinter as tk
from word_lookup_theme import (APP_NAME, SAMPLE_CARD, TRAY_OK)
from word_lookup_models import (as_event)
from word_lookup_ui import (Popup, Highlight)
try:
    if TRAY_OK:
        import pystray
except (ImportError, RuntimeError):
    pystray = None


def make_tray_icon():
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=(59, 130, 246, 255))
    try:
        try:
            font = ImageFont.load_default(size=38)
        except TypeError:
            font = ImageFont.load_default()
        d.text((32, 32), "A", fill="white", font=font, anchor="mm")
    except Exception:
        d.ellipse((20, 20, 44, 44), fill="white")
    return img

class WindowController:
    def inside_main_window(self, x, y) -> bool:
        try:
            r = self.root
            if r.state() in ("withdrawn", "iconic"):
                return False
            return (r.winfo_rootx() <= x <= r.winfo_rootx() + r.winfo_width()
                    and r.winfo_rooty() <= y <= r.winfo_rooty() + r.winfo_height())
        except Exception:
            return False


    def close_all(self):
        for name in ("popup", "highlight"):
            obj = getattr(self, name)
            if obj:
                obj.close()
                setattr(self, name, None)


    def dismiss_lookup(self):
        self._new_token()  # запізнілий мережевий результат більше не відкриє картку
        self.close_all()
        if self.toast_popup:
            self.toast_popup.close()
            self.toast_popup = None
        self._resume_game()


    def show_highlight(self, hit):
        """Миттєвий відгук: підсвічування з'являється ще до того, як прийде переклад."""
        self.close_all()
        if self.toast_popup:
            self.toast_popup.close()
            self.toast_popup = None
        self.highlight = Highlight(self, hit["word"], hit["box"])
        self.hl_hit = hit


    def show_lookup(self, card, hit, anchor=None):
        if card.get("kind") == "word" and card is not SAMPLE_CARD and self.library.saved_entry(card["word"]):
            if self.library.add_saved_word(card):
                self.refresh_saved_count()
        reuse = hit is not None and self.highlight is not None and self.hl_hit is hit
        if self.popup:
            self.popup.close()
            self.popup = None
        if not reuse and self.highlight:
            self.highlight.close()
            self.highlight = None
        if self.toast_popup:
            self.toast_popup.close()
            self.toast_popup = None
        if hit is not None and self.settings.data["ocr"]["highlight"]:
            if not reuse:
                self.highlight = Highlight(self, hit["word"], hit["box"])
                self.hl_hit = hit
            anchor = self.highlight.rect
        elif hit is not None:
            anchor = hit["box"]
        elif anchor is None:
            x, y = self.ms.position
            anchor = (x, y, x, y)
        self.popup = Popup(self, card, anchor)
        if card is not SAMPLE_CARD:
            self.last_card = card
            self._refresh_cache_label()


    def rerender(self, popup):
        card, anchor, exp = popup.card, popup.anchor, popup.expanded
        pinned = popup.pinned
        scroll = popup.viewport.canvas.yview()[0]
        editing = hasattr(popup, "correction_row") and popup.correction_row.winfo_ismapped()
        draft = popup.word_var.get() if editing else None
        cursor = popup.word_editor.index("insert") if editing else 0
        popup.close()
        self.popup = Popup(self, card, anchor, exp, pinned=pinned)
        if editing:
            self.popup.edit_word()
            self.popup.word_var.set(draft)
            self.popup.word_editor.selection_clear()
            self.popup.word_editor.icursor(cursor)
        self.popup.win.update_idletasks()
        self.popup.viewport.canvas.yview_moveto(scroll)


    def toast(self, text, timeout=None):
        if self.toast_popup:
            self.toast_popup.close()
        x, y = self.ms.position
        self.toast_popup = Popup(self, {"kind": "toast", "text": text}, (x, y, x, y),
                                 toast=True, timeout=timeout)


    def show_loading(self, anchor=None):
        """Індикатор «Завантаження…» — з'являється миттєво в тому ж місці, куди потім
        стане картка. Автоматично зникає, коли прийде готова картка або повідомлення
        про помилку (show_lookup/show_highlight/toast самі закривають toast_popup)."""
        if self.toast_popup:
            self.toast_popup.close()
        if anchor is None:
            x, y = self.ms.position
            anchor = (x, y, x, y)
        self.toast_popup = Popup(self, {"kind": "toast", "text": "Завантаження…"}, anchor,
                                 toast=True, timeout=12.0)


    def show_preview(self):
        # A sample is an explicit new view: queued network results from the
        # previous lookup must not replace it or keep its game paused.
        self._lookup_profile = {}
        self.dismiss_lookup()
        b = self.preview_btn
        anchor = (b.winfo_rootx(), b.winfo_rooty(), b.winfo_rootx() + b.winfo_width(),
                  b.winfo_rooty() + b.winfo_height())
        self.show_lookup(SAMPLE_CARD, None, anchor)


    def _refresh_preview(self):
        if self._preview_timer is not None:
            self.root.after_cancel(self._preview_timer)
        self._preview_timer = self.root.after(80, self._apply_card_appearance)


    def _apply_card_appearance(self):
        self._preview_timer = None
        if self.popup:
            self.rerender(self.popup)


    def hide_window(self):
        if self.tray:
            self.root.withdraw()
        else:
            self.root.iconify()


    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            self.logger.exception('word_lookup.show_window')


    def on_close(self):
        self.hide_window()


    def _setup_tray(self):
        if not TRAY_OK:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Відкрити налаштування",
                                 lambda icon, item: self.events.put(as_event(("tray", "show"))), default=True),
                pystray.MenuItem("Пауза / продовжити",
                                 lambda icon, item: self.events.put(as_event(("tray", "pause")))),
                pystray.MenuItem("Вийти", lambda icon, item: self.events.put(as_event(("tray", "quit")))))
            self.tray = pystray.Icon("WordLookup", make_tray_icon(), APP_NAME, menu)
            self.tray.run_detached()
        except Exception as e:
            self.logger.write(f"Трей недоступний: {e}")
            self.tray = None

