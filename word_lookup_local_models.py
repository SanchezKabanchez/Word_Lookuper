"""Local Argos-format SentencePiece models through the CTranslate2 CPU runtime.

This uses the documented CTranslate2/SentencePiece APIs directly, without loading
Argos' Stanza, spaCy or Torch dependencies or running a sentence-boundary model.
"""
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import zipfile


@dataclass(frozen=True)
class Model:
    directory: Path
    source: str
    target: str
    prefix: str = ''


def read_model(directory: Path) -> Model:
    metadata = directory / 'metadata.json'
    if metadata.stat().st_size > 256 * 1024:
        raise ValueError('Завеликі метадані локальної моделі.')
    data = json.loads(metadata.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('Некоректні метадані моделі.')
    source, target = data.get('from_code'), data.get('to_code')
    if not all(isinstance(code, str) and re.fullmatch(r'[a-z]{2,3}(?:-[A-Za-z0-9]+)?', code) for code in (source, target)):
        raise ValueError('Модель не має коректної мовної пари.')
    if data.get('type', 'translate') != 'translate':
        raise ValueError('Це не модель перекладу.')
    prefix = data.get('target_prefix', '')
    if not isinstance(prefix, str) or len(prefix) > 200:
        raise ValueError('Некоректний префікс моделі.')
    if not (directory / 'sentencepiece.model').is_file():
        raise ValueError('Потрібна Argos-модель із sentencepiece.model; BPE-моделі не підтримуються.')
    if not (directory / 'model' / 'model.bin').is_file():
        raise ValueError('Немає CTranslate2 model/model.bin.')
    return Model(directory, source, target, prefix)


class LocalTranslation:
    def __init__(self, model: Model, threads: int):
        import ctranslate2
        import sentencepiece
        self.model = model
        self.tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(model.directory / 'sentencepiece.model'))
        self.engine = ctranslate2.Translator(str(model.directory / 'model'), device='cpu',
            inter_threads=1, intra_threads=threads, compute_type='int8')

    def translate(self, text: str) -> str:
        tokens = self.tokenizer.encode(text, out_type=str)
        if len(tokens) > 2048:
            raise ValueError('Завеликий текст для локальної моделі.')
        kwargs = {'target_prefix': [self.model.prefix.split()]} if self.model.prefix else {}
        result = self.engine.translate_batch([tokens], beam_size=4, max_batch_size=1,
            max_input_length=0, max_decoding_length=min(2048, max(128, 2 * len(tokens) + 32)),
            replace_unknowns=True, **kwargs)
        output = result[0].hypotheses[0]
        if self.model.prefix:
            prefix = self.model.prefix.split()
            if output[:len(prefix)] == prefix:
                output = output[len(prefix):]
        return self.tokenizer.decode(output).replace('▁', ' ').strip()


class Language:
    def __init__(self, code, catalog):
        self.code, self.catalog = code, catalog

    def get_translation(self, target):
        model = next((item for item in self.catalog.models if item.source == self.code and item.target == target.code), None)
        return LocalTranslation(model, self.catalog.threads) if model else None


class ModelCatalog:
    def __init__(self, directory: Path, threads: int):
        self.directory, self.threads = directory, threads
        self.models = []

    @lru_cache(maxsize=1)
    def get_installed_languages(self):
        self.models = []
        if self.directory.exists():
            for metadata in sorted(self.directory.glob('*/metadata.json')):
                try:
                    self.models.append(read_model(metadata.parent))
                except (OSError, ValueError, KeyError, TypeError):
                    continue  # one damaged optional model must not hide other pairs
        return [Language(code, self) for code in sorted({code for model in self.models for code in (model.source, model.target)})]

    def install(self, filename: Path) -> None:
        if filename.suffix.lower() != '.argosmodel' or filename.stat().st_size > 512 * 1024**2:
            raise ValueError('Вибери .argosmodel розміром до 512 МБ.')
        digest = hashlib.sha256()
        with filename.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / digest.hexdigest()[:24]
        with zipfile.ZipFile(filename) as archive, tempfile.TemporaryDirectory(prefix='.install-', dir=self.directory) as temp:
            entries = archive.infolist()
            if len(entries) > 20_000 or sum(entry.file_size for entry in entries) > 2 * 1024**3:
                raise ValueError('Завеликий архів моделі.')
            for entry in entries:
                path = PurePosixPath(entry.filename.replace('\\', '/'))
                if (not path.parts or path.is_absolute() or '..' in path.parts or ':' in path.parts[0]
                        or stat.S_ISLNK(entry.external_attr >> 16)):
                    raise ValueError('Небезпечний шлях в архіві моделі.')
            metadata = [entry for entry in entries if PurePosixPath(entry.filename).name == 'metadata.json'
                        and len(PurePosixPath(entry.filename).parts) <= 2]
            if len(metadata) != 1:
                raise ValueError('Архів повинен містити одну модель з metadata.json.')
            package = PurePosixPath(metadata[0].filename).parent
            root = Path(temp)
            for entry in entries:
                path = PurePosixPath(entry.filename.replace('\\', '/'))
                try:
                    relative = path.relative_to(package)
                except ValueError:
                    continue
                # Model binaries and tokenizer are data; Python/pickle/Stanza files
                # are deliberately unnecessary to this runtime and are not installed.
                if not relative.parts or entry.is_dir():
                    continue
                if relative.parts[0] != 'model' and relative.name not in {'metadata.json', 'sentencepiece.model', 'LICENSE', 'LICENSE.txt', 'README.md'}:
                    continue
                if relative.suffix.lower() in {'.py', '.pyc', '.pkl', '.pickle', '.pt', '.pth', '.dll', '.so', '.exe'}:
                    continue
                destination = root.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, destination.open('wb') as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            read_model(root)
            if target.exists():
                read_model(target)
            else:
                os.replace(root, target)
        self.get_installed_languages.cache_clear()
