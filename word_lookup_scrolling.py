"""One wheel dispatcher per Tk window, before native control bindings."""
import tkinter as tk
import weakref

_SEQUENCES = ("<MouseWheel>", "<Button-4>", "<Button-5>")
_SCROLL_CLASSES = {"Text", "Treeview", "Listbox", "Canvas"}


def register_panel(panel):
    top = panel.winfo_toplevel()
    router = getattr(top, "_word_lookup_wheel_router", None)
    if router is None:
        router = WheelRouter(top)
        top._word_lookup_wheel_router = router
    router.panels.add(panel)
    router.attach_tree(panel)
    return router


class WheelRouter:
    def __init__(self, top):
        self.top = top
        # Tkinter's bind_class registers Python callbacks on the interpreter's
        # root even when invoked through a Toplevel. Delete from that owner too.
        self._bind_owner = top._root()
        self.panels = weakref.WeakSet()
        self.tag = f"WordLookupWheel{id(self)}"
        self._system = top.tk.call("tk", "windowingsystem")
        sequences = _SEQUENCES if self._system == "x11" else _SEQUENCES[:1]
        self._commands = {seq: top.bind_class(self.tag, seq, self.route) for seq in sequences}
        self._map_id = top.bind("<Map>", self._mapped, add="+")
        self._target = None
        self._remainder = 0.0
        self.attach_tree(top)

    def attach_tree(self, widget):
        if widget.winfo_toplevel() is not self.top:
            return
        self._attach(widget)
        for child in widget.winfo_children():
            self.attach_tree(child)

    def _attach(self, widget):
        tags = widget.bindtags()
        if self.tag not in tags:
            widget.bindtags((tags[0], self.tag, *tags[1:]))

    def _mapped(self, event):
        widget = event.widget
        # Native combobox popdowns can have a Tcl pathname without a Python
        # widget. Their native list scrolling remains in charge.
        if isinstance(widget, tk.Misc) and widget.winfo_toplevel() is self.top:
            self._attach(widget)

    def _under_pointer(self, event):
        try:
            widget = self.top.winfo_containing(event.x_root, event.y_root)
        except (tk.TclError, KeyError):
            return None
        if widget is not None and widget.winfo_toplevel() is self.top and widget.winfo_viewable():
            return widget
        return None

    @staticmethod
    def _direction(event):
        number = getattr(event, "num", None)
        if number in (4, 5):
            return -1 if number == 4 else 1
        delta = getattr(event, "delta", 0)
        return -1 if delta > 0 else 1 if delta < 0 else 0

    @staticmethod
    def _can_scroll(widget, direction):
        first, last = widget.yview()
        return first > 1e-6 if direction < 0 else last < 1.0 - 1e-6

    def _units(self, event, target):
        if target is not self._target:
            self._target, self._remainder = target, 0.0
        if getattr(event, "num", None) in (4, 5):
            return -3 if event.num == 4 else 3
        delta = getattr(event, "delta", 0)
        self._remainder += -delta if self._system == "aqua" else -delta / 120 * 3
        units = int(self._remainder)
        self._remainder -= units
        return units

    def route(self, event):
        direction = self._direction(event)
        if not direction or getattr(event, "state", 0) & 1:
            return  # Shift+wheel keeps native horizontal scrolling.
        source = getattr(event, "widget", None)
        source_class = source.winfo_class() if isinstance(source, tk.Misc) else ""
        if source_class == "TCombobox" and source.instate(("pressed",)):
            return  # An opened native drop-down owns its list scrolling.
        widget = self._under_pointer(event)
        candidates, seen, in_panel = [], set(), False
        while widget is not None:
            canvas = getattr(widget, "_word_lookup_scroll_canvas", None)
            if canvas is not None:
                candidate, in_panel = canvas, True
            elif widget.winfo_class() in _SCROLL_CLASSES:
                candidate = widget
            else:
                candidate = None
            if candidate is not None and candidate not in seen:
                candidates.append(candidate)
                seen.add(candidate)
            if widget is self.top:
                break
            widget = getattr(widget, "master", None)
        for candidate in candidates:
            if self._can_scroll(candidate, direction):
                units = self._units(event, candidate)
                if units:
                    candidate.yview_scroll(units, "units")
                return "break"
        # A closed spinbox/combobox must not change a preference when the page
        # is already at its edge. Keyboard and drop-down selection stay native.
        if in_panel or source_class in ("TCombobox", "TSpinbox", "Spinbox"):
            return "break"

    def unregister(self, panel):
        self.panels.discard(panel)
        if self.panels:
            return
        self._target = None
        self.top.unbind("<Map>", self._map_id)
        for seq, command in self._commands.items():
            self.top.unbind_class(self.tag, seq)
            self._bind_owner.deletecommand(command)
        self._detach_tree(self.top)
        del self.top._word_lookup_wheel_router

    def _detach_tree(self, widget):
        if widget.winfo_toplevel() is not self.top:
            return
        widget.bindtags(tuple(tag for tag in widget.bindtags() if tag != self.tag))
        for child in widget.winfo_children():
            self._detach_tree(child)
