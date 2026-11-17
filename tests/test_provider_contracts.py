"""Pinned adapter samples are parsed offline through the actual provider methods."""
import json
from pathlib import Path
import pytest
import word_lookup as w

FIXTURES = Path(__file__).parent / 'fixtures/providers'


@pytest.mark.parametrize('fixture,method', [
    ('google_gtx.json', '_p_gtx'), ('google_chrome.json', '_p_chrome'),
    ('google_chrome_array.json', '_p_chrome'), ('mymemory.json', '_p_mymemory')])
def test_translation_contract_samples(monkeypatch, fixture, method):
    data = json.loads((FIXTURES / fixture).read_text(encoding='utf-8'))
    monkeypatch.setattr(w._providers, '_http_json', lambda *_a, **_k: data)
    text, groups = getattr(w._providers, method)('hello', 'en', True)
    assert text == 'привіт'
    assert isinstance(groups, list)
    assert all(isinstance(term, str) for _pos, terms in groups for term in terms)


@pytest.mark.parametrize('data', [{}, [], [None], [42], 'changed format'])
def test_incompatible_gtx_response_is_rejected(monkeypatch, data):
    monkeypatch.setattr(w._providers, '_http_json', lambda *_a, **_k: data)
    with pytest.raises(ValueError):
        w._providers._p_gtx('hello', 'en', True)
