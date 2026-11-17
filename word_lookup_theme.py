"""Display constants shared by views and controllers."""
import sys
import importlib.util
APP_NAME = "Word Lookup"
AUDIO_OK = importlib.util.find_spec("pygame") is not None
TRAY_OK = sys.platform != "darwin" and importlib.util.find_spec("pystray") is not None
FONT = "Segoe UI" if sys.platform.startswith("win") else "Helvetica" if sys.platform == "darwin" else "DejaVu Sans"

KEY = "#ff00fe"

ACTIONS = [
    ("lookup_word", "Знайти слово під курсором (OCR)"),
    ("select_region", "Вибрати слово клавіатурою / областю на стоп-кадрі"),
    ("free_cursor", "Керований курсор клавіатурами (для ігор із захопленою мишею)"),
    ("unlock_cursor", "Відкріпити курсор (зняти обмеження, яким гра тримає мишу в центрі)"),
    ("translate_line", "Перекласти рядок під курсором (OCR)"),
    ("translate_selection", "Перекласти виділений текст"),
    ("speak", "Озвучити слово"),
    ("copy_translation", "Скопіювати переклад"),
    ("toggle_pause", "Пауза / продовжити"),
    ("close_popup", "Закрити картку"),
    ("resume_game", "Відновити призупинену гру й закрити накладку"),
]

THEMES = {
    "light": dict(bg="#ffffff", fg="#202c40", dim="#59677b", head="#455978",
                  line="#e3e9f2", link="#2864dc", example="#53657b", border="#d5deed"),
    "dark": dict(bg="#1d2029", fg="#f1f1f5", dim="#a2a8b8", head="#a2a8b8",
                 line="#2b2f3b", link="#ffd43b", example="#b8becc", border="#303542"),
    "sepia": dict(bg="#f4ecd8", fg="#3b2f1e", dim="#6e5f48", head="#6b5a40",
                  line="#d8cba8", link="#a4562b", example="#4d3f2a", border="#cdbf9f"),
}

THEME_NAMES = {"Світла": "light", "Темна": "dark", "Сепія": "sepia"}

CARD_CONTENT_OPTIONS = (
    (("sections", "bilingual"), "Переклад слова"),
    (("popup", "sentence"), "Речення мовою оригіналу"),
    (("popup", "sentence_translation"), "Переклад речення"),
    (("popup", "phonetic"), "Транскрипція"),
    (("sections", "definitions"), "Визначення англійською"),
    (("popup", "translate_definition"), "Переклад визначення"),
    (("popup", "examples"), "Приклади вживання"),
    (("sections", "thesaurus"), "Синоніми й антоніми"),
    (("popup", "alternate_translations"), "Додаткові варіанти перекладу"),
    (("popup", "context_image"), "Зображення контексту"),
    (("popup", "btn_speak"), "Кнопки озвучення"),
)

LANG_NAMES = {"Українська": "uk", "Польська": "pl", "Німецька": "de", "Французька": "fr",
              "Іспанська": "es", "Італійська": "it", "Чеська": "cs", "Турецька": "tr"}

LANG_EN = {"uk": "Ukrainian", "pl": "Polish", "de": "German", "fr": "French",
           "es": "Spanish", "it": "Italian", "cs": "Czech", "tr": "Turkish"}

MOUSE_BTN_NAMES = {"Вимкнено": "none", "Середня кнопка (колесо)": "middle",
                   "Бічна кнопка «назад»": "x1", "Бічна кнопка «вперед»": "x2"}

MOUSE_MOD_NAMES = {"Без клавіші": "none", "Ctrl": "ctrl", "Alt": "alt", "Shift": "shift"}

ENGINE_NAMES = {"Авто": "auto", "Tesseract": "tesseract", "RapidOCR": "rapidocr"}

MOUSE_BTN_ALIASES = {"middle": {"middle"}, "x1": {"x1", "button8"}, "x2": {"x2", "button9"}}

STATUS_LABELS = {"Усі": "all", "Нові": "new", "Вивчаю": "learning", "Знаю": "known"}

SAMPLE_CARD = {
    "kind": "word", "word": "understatement", "base": "understatement",
    "phonetic": "ˌʌndəˈsteɪtmənt", "audios": [("BrE", "preview"), ("AmE", "preview")],
    "translation": "применшення",
    "sentence": "To say I am delighted is an understatement.",
    "sentence_tr": "Сказати, що я в захваті, — це применшення.",
    "definition_tr": "Опис чогось як меншого або менш важливого, ніж воно є насправді.",
    "groups": [("іменник", ["применшення", "стриманий вислів", "недомовка"])],
    "meanings": [{
        "pos": "noun",
        "defs": [
            {"d": "The presentation of something as being smaller or less good "
                  "or important than it really is.",
             "e": "to say I am delighted is an understatement"},
            {"d": "A statement that expresses an idea in restrained terms.", "e": ""},
        ],
        "syn": ["minimization", "trivialization", "euphemism", "understatedness",
                "restraint", "litotes", "underplaying"],
        "ant": ["exaggeration", "overstatement"],
    }],
}

