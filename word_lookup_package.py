"""Atomic Anki package writing with deterministic ownership of temporary files."""
from contextlib import closing
import itertools
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Iterator, Protocol, Sequence
import zipfile


class AnkiPackage(Protocol):
    media_files: Sequence[str]

    def write_to_db(self, cursor: sqlite3.Cursor, timestamp: float, id_gen: Iterator[int]) -> None: ...


def write_package(package: AnkiPackage, destination: str) -> None:
    """Keep an existing export intact on failure and remove the temporary SQLite DB."""
    target = Path(destination)
    temporary = None
    try:
        with tempfile.TemporaryDirectory(prefix='wordlookup-anki-') as directory:
            database = Path(directory) / 'collection.anki2'
            timestamp = time.time()
            with closing(sqlite3.connect(database)) as connection:
                with closing(connection.cursor()) as cursor:
                    package.write_to_db(cursor, timestamp, itertools.count(int(timestamp * 1000)))
                connection.commit()
            descriptor, temporary = tempfile.mkstemp(prefix='.wordlookup-export-', suffix='.apkg', dir=target.parent)
            os.close(descriptor)
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as output:
                output.write(database, 'collection.anki2')
                media = dict(enumerate(package.media_files))
                output.writestr('media', json.dumps({index: os.path.basename(path) for index, path in media.items()}))
                for index, path in media.items():
                    output.write(path, str(index))
            os.replace(temporary, target)
            temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
