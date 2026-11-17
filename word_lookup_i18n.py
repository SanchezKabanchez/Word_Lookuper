"""gettext boundary for display text; persisted keys and provider IDs stay stable."""
from functools import lru_cache
import gettext
import os
from pathlib import Path

LOCALE_DIR = Path(__file__).resolve().parent / 'wordlookup' / 'locale'


@lru_cache(maxsize=8)
def translator(language: str) -> gettext.NullTranslations:
    if language == 'uk':
        return gettext.NullTranslations()
    return gettext.translation('wordlookup', localedir=str(LOCALE_DIR), languages=[language], fallback=True)


def _(message: str) -> str:
    return translator(os.environ.get('WORD_LOOKUP_UI_LANGUAGE', 'uk')).gettext(message)
