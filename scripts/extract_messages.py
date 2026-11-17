"""Extract static gettext display strings; leave persisted identifiers untouched."""
import ast
import json
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    messages = {}
    for source in sorted(root.glob('word_lookup*.py')):
        for node in ast.walk(ast.parse(source.read_text(encoding='utf-8'))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {'_', 'tr_ui'}
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                messages.setdefault(node.args[0].value, []).append(source.name + ':' + str(node.lineno))
    header = 'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
    body = ''.join('#: ' + ' '.join(locations) + '\nmsgid ' + json.dumps(text, ensure_ascii=False)
                   + '\nmsgstr ""\n\n' for text, locations in sorted(messages.items()))
    destination = root / 'wordlookup/locale/wordlookup.pot'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(header + body, encoding='utf-8')
    print(f'{len(messages)} display messages')


if __name__ == '__main__':
    main()
