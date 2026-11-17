"""Versioned disk envelopes; legacy formats are decoded without losing data."""
from typing import Any


class UnsupportedSchema(ValueError):
    pass


def _decode(payload: Any, field: str, kind: str, legacy_type: type) -> Any:
    if isinstance(payload, dict) and "schema" in payload:
        if type(payload["schema"]) is not int or payload["schema"] != 1 or payload.get("kind") != kind:
            raise UnsupportedSchema("Невідома або новіша схема даних; файл не змінено.")
        payload = payload.get(field)
    if not isinstance(payload, legacy_type):
        raise ValueError("Неправильний формат збережених даних")
    return payload


def decode_words(payload: Any) -> list[dict]:
    values = _decode(payload, "words", "wordlookup.library", list)
    if any(not isinstance(e, dict) or not isinstance(e.get("word"), str) or not e["word"].strip()
           for e in values):
        raise ValueError("Файл має містити список записів із непорожнім полем word.")
    return values


def encode_words(values: list[dict]) -> dict:
    return {"schema": 1, "kind": "wordlookup.library", "words": values}


def decode_cards(payload: Any) -> dict:
    return _decode(payload, "cards", "wordlookup.card-cache", dict)


def encode_cards(values: dict) -> dict:
    return {"schema": 1, "kind": "wordlookup.card-cache", "cards": values}


def decode_translations(payload: Any) -> dict:
    return _decode(payload, "translations", "wordlookup.text-cache", dict)


def encode_translations(values: dict) -> dict:
    return {"schema": 1, "kind": "wordlookup.text-cache", "translations": values}
