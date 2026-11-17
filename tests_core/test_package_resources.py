import sqlite3
import zipfile
import pytest
from word_lookup_package import write_package


class Package:
    media_files = []
    def write_to_db(self, cursor, timestamp, id_gen):
        cursor.execute('create table sample (word text)')
        cursor.execute('insert into sample values (?)', ('kindness',))


def test_package_can_be_read_and_its_database_is_not_left_behind(tmp_path):
    result = tmp_path / 'deck.apkg'
    write_package(Package(), str(result))
    with zipfile.ZipFile(result) as package:
        assert package.namelist() == ['collection.anki2', 'media']
        database = tmp_path / 'read.anki2'
        database.write_bytes(package.read('collection.anki2'))
        connection = sqlite3.connect(database)
        try:
            assert connection.execute('select word from sample').fetchone() == ('kindness',)
        finally:
            connection.close()
            database.unlink()
    assert list(tmp_path.iterdir()) == [result]


def test_failed_export_preserves_previous_package(tmp_path):
    class BrokenPackage(Package):
        def write_to_db(self, *args):
            raise OSError('disk error')
    result = tmp_path / 'existing.apkg'
    result.write_bytes(b'previous study deck')
    with pytest.raises(OSError):
        write_package(BrokenPackage(), str(result))
    assert result.read_bytes() == b'previous study deck'
    assert list(tmp_path.iterdir()) == [result]
