"""Named results and UI messages; field additions do not change tuple offsets."""
from dataclasses import dataclass
from contextvars import ContextVar

LAST_TRANSLATOR = ContextVar("word_lookup_last_translator", default="")


@dataclass(frozen=True)
class TranslationOutcome:
    text: str
    groups: list
    note: str = ""
    limited: bool = False
    provider: str = ""

    def __iter__(self):
        # Compatibility at the old four-value adapter boundary. Controllers and
        # new providers use named attributes, including the provider identity.
        yield from (self.text, self.groups, self.note, self.limited)

    @classmethod
    def from_legacy(cls, value):
        if isinstance(value, cls):
            return value
        return cls(*value)


class UiEvent:
    """Named production fields with a legacy sequence adapter for integrations."""
    def legacy(self) -> tuple:
        raise NotImplementedError

    def __getitem__(self, index):
        return self.legacy()[index]

    def __len__(self):
        return len(self.legacy())

    def __iter__(self):
        return iter(self.legacy())

    def __eq__(self, other):
        return self.legacy() == (other.legacy() if isinstance(other, UiEvent) else other)


@dataclass(frozen=True, eq=False)
class ShowCard(UiEvent):
    card: dict
    hit: dict | None
    anchor: tuple | None
    token: int | None = None

    def legacy(self):
        result = ("show", self.card, self.hit, self.anchor)
        return result if self.token is None else (*result, self.token)


@dataclass(frozen=True, eq=False)
class UpdateCard(UiEvent):
    token: int
    word: str
    card: dict

    def legacy(self):
        return "update", self.token, self.word, self.card


@dataclass(frozen=True, eq=False)
class LookupFailure(UiEvent):
    message: str
    token: int | None = None

    def legacy(self):
        return "toast", self.message


@dataclass(frozen=True, eq=False)
class ScopedEvent(UiEvent):
    token: int
    event: UiEvent

    def legacy(self):
        return "lookup_event", self.token, self.event


@dataclass(frozen=True, eq=False)
class ShowLoading(UiEvent):
    anchor: tuple | None

    def legacy(self):
        return "loading", self.anchor


@dataclass(frozen=True, eq=False)
class HighlightReady(UiEvent):
    hit: dict
    token: int

    def legacy(self):
        return "hl", self.hit, self.token


@dataclass(frozen=True, eq=False)
class EscapePressed(UiEvent):
    def legacy(self):
        return ("esc",)


@dataclass(frozen=True, eq=False)
class ActionRequested(UiEvent):
    action: str

    def legacy(self):
        return "action", self.action


@dataclass(frozen=True, eq=False)
class FreeCursorKey(UiEvent):
    key: str

    def legacy(self):
        return "fc_key", self.key


@dataclass(frozen=True, eq=False)
class RecordingStatus(UiEvent):
    state: str
    text: str

    def legacy(self):
        return "rec", self.state, self.text


@dataclass(frozen=True, eq=False)
class MouseInput(UiEvent):
    phase: str
    x: int
    y: int
    button: str
    modifiers: set | None = None

    def legacy(self):
        value = (self.phase, self.x, self.y, self.button)
        return (*value, self.modifiers) if self.phase == "press" else value


@dataclass(frozen=True, eq=False)
class DiagnosticReady(UiEvent):
    text: str
    path: str | None

    def legacy(self):
        return "diag", (self.text, self.path)


@dataclass(frozen=True, eq=False)
class ControlStatus(UiEvent):
    control: str
    text: str

    def legacy(self):
        kind = {"dict": "dict_diag", "ocr": "ocr_status", "deepl": "deepl_status"}[self.control]
        return kind, self.text


@dataclass(frozen=True, eq=False)
class ExportFinished(UiEvent):
    format: str
    count: int
    path: str
    error: str

    def legacy(self):
        return "export_done", self.format, self.count, self.path, self.error


@dataclass(frozen=True, eq=False)
class AnkiSyncFinished(UiEvent):
    result: dict | None
    error: str

    def legacy(self):
        return "anki_sync_done", self.result, self.error


@dataclass(frozen=True, eq=False)
class TrayCommand(UiEvent):
    command: str

    def legacy(self):
        return "tray", self.command


@dataclass(frozen=True)
class ServiceStatus:
    service: str
    message: str


@dataclass(frozen=True)
class Notice:
    message: str


def as_event(value):
    """Compatibility boundary; tuple positions are interpreted only here."""
    if isinstance(value, (UiEvent, ServiceStatus, Notice)):
        return value
    kind = value[0]
    if kind == "show":
        return ShowCard(value[1], value[2], value[3], value[4] if len(value) > 4 else None)
    if kind == "update":
        return UpdateCard(value[1], value[2], value[3])
    if kind == "lookup_event":
        return ScopedEvent(value[1], as_event(value[2]))
    if kind == "toast":
        return LookupFailure(value[1])
    if kind == "loading":
        return ShowLoading(value[1])
    if kind == "hl":
        return HighlightReady(value[1], value[2])
    if kind == "esc":
        return EscapePressed()
    if kind == "action":
        return ActionRequested(value[1])
    if kind == "fc_key":
        return FreeCursorKey(value[1])
    if kind == "rec":
        return RecordingStatus(value[1], value[2])
    if kind in ("press", "release"):
        return MouseInput(kind, value[1], value[2], value[3], value[4] if len(value) > 4 else None)
    if kind == "diag":
        return DiagnosticReady(*value[1])
    if kind in ("dict_diag", "ocr_status", "deepl_status"):
        return ControlStatus({"dict_diag": "dict", "ocr_status": "ocr", "deepl_status": "deepl"}[kind], value[1])
    if kind == "export_done":
        return ExportFinished(value[1], value[2], value[3], value[4])
    if kind == "anki_sync_done":
        return AnkiSyncFinished(value[1], value[2])
    if kind == "tray":
        return TrayCommand(value[1])
    raise ValueError("Unknown UI event: " + str(kind))
