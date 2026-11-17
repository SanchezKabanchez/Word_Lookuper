"""Model imports install data only; native translation is a separate boundary."""
import json
from types import SimpleNamespace
import sys
import zipfile
import pytest
from word_lookup_local_models import ModelCatalog, LocalTranslation, Model, read_model


def model_archive(path, extra=None, *, tokenizer=True):
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('sample/metadata.json', json.dumps({'from_code': 'en', 'to_code': 'uk'}))
        archive.writestr('sample/model/model.bin', b'native fixture data')
        if tokenizer:
            archive.writestr('sample/sentencepiece.model', b'tokenizer fixture data')
        archive.writestr('sample/stanza/unsafe.pt', b'never loaded')
        archive.writestr('sample/run.py', 'raise RuntimeError("must not execute")')
        for name, data in (extra or {}).items():
            archive.writestr(name, data)


def test_import_registers_pair_and_never_installs_executable_or_pickle(tmp_path):
    source = tmp_path / 'model.argosmodel'
    model_archive(source)
    catalog = ModelCatalog(tmp_path / 'models', 2)
    catalog.install(source)
    assert {language.code for language in catalog.get_installed_languages()} == {'en', 'uk'}
    installed = list((tmp_path / 'models').glob('*/metadata.json'))
    assert len(installed) == 1
    assert read_model(installed[0].parent).target == 'uk'
    assert not list((tmp_path / 'models').rglob('*.pt'))
    assert not list((tmp_path / 'models').rglob('*.py'))
    catalog.install(source)
    assert len(list((tmp_path / 'models').glob('*/metadata.json'))) == 1


@pytest.mark.parametrize('path', ['../outside', 'sample/../../outside', 'C:/outside', '/outside', 'sample/..\\outside'])
def test_import_rejects_archive_path_traversal(tmp_path, path):
    source = tmp_path / 'model.argosmodel'
    model_archive(source, {path: b'bad'})
    with pytest.raises(ValueError):
        ModelCatalog(tmp_path / 'models', 2).install(source)
    assert not list((tmp_path / 'models').glob('*/metadata.json'))


def test_bpe_model_is_rejected_with_explicit_message(tmp_path):
    source = tmp_path / 'model.argosmodel'
    model_archive(source, tokenizer=False)
    with pytest.raises(ValueError, match='sentencepiece.model'):
        ModelCatalog(tmp_path / 'models', 2).install(source)


def test_native_adapter_uses_cpu_budget_and_never_truncates_silently(tmp_path, monkeypatch):
    calls = []
    class Tokenizer:
        def __init__(self, **kwargs):
            pass
        def encode(self, text, out_type):
            return text.split()
        def decode(self, pieces):
            return ' '.join(pieces)
    class Native:
        def __init__(self, path, **kwargs):
            calls.append(kwargs)
        def translate_batch(self, tokens, **kwargs):
            calls.append(kwargs)
            return [SimpleNamespace(hypotheses=[['доброго', 'дня']])]
    monkeypatch.setitem(sys.modules, 'ctranslate2', SimpleNamespace(Translator=Native))
    monkeypatch.setitem(sys.modules, 'sentencepiece', SimpleNamespace(SentencePieceProcessor=Tokenizer))
    engine = LocalTranslation(Model(tmp_path, 'en', 'uk'), 2)
    assert engine.translate('Good day') == 'доброго дня'
    assert calls[0] == {'device': 'cpu', 'inter_threads': 1, 'intra_threads': 2, 'compute_type': 'int8'}
    assert calls[1]['max_input_length'] == 0
    with pytest.raises(ValueError):
        engine.translate('token ' * 2049)
