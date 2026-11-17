"""Compile this project's simple singular gettext catalogs without external tools."""
import ast
from pathlib import Path
import struct


def compile_catalog(source: Path, destination: Path) -> None:
    messages = {}
    msgid = msgstr = ''
    section = None
    def save():
        if msgid or msgstr:
            messages[msgid] = msgstr
    for raw in source.read_text(encoding='utf-8').splitlines() + ['']:
        line = raw.strip()
        if not line:
            save()
            msgid = msgstr = ''
            section = None
        elif line.startswith('#'):
            continue
        elif line.startswith('msgid '):
            section, msgid = 'id', ast.literal_eval(line[6:])
        elif line.startswith('msgstr '):
            section, msgstr = 'str', ast.literal_eval(line[7:])
        elif line.startswith('"'):
            if section == 'id':
                msgid += ast.literal_eval(line)
            elif section == 'str':
                msgstr += ast.literal_eval(line)
        else:
            raise ValueError('Only singular catalogs are supported: ' + line[:100])
    items = sorted((key.encode('utf-8'), value.encode('utf-8')) for key, value in messages.items() if value)
    count = len(items)
    offset = 28 + 16 * count
    keys = b''.join(key + b'\0' for key, _value in items)
    values = b''.join(value + b'\0' for _key, value in items)
    key_table = []
    value_table = []
    key_at, value_at = offset, offset + len(keys)
    for key, value in items:
        key_table.extend((len(key), key_at))
        value_table.extend((len(value), value_at))
        key_at += len(key) + 1
        value_at += len(value) + 1
    data = struct.pack('<7I', 0x950412DE, 0, count, 28, 28 + 8 * count, 0, 0)
    data += struct.pack('<' + 'I' * (4 * count), *(key_table + value_table)) + keys + values
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def main() -> None:
    root = Path(__file__).resolve().parents[1] / 'wordlookup' / 'locale'
    for source in root.glob('*/LC_MESSAGES/*.po'):
        compile_catalog(source, source.with_suffix('.mo'))
        print(source.relative_to(root))


if __name__ == '__main__':
    main()
