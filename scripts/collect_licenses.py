"""Collect installed dependency notices and source URLs into the EXE distribution."""
from importlib.metadata import distributions
import json
from pathlib import Path


def collect(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    index = []
    for dist in sorted(distributions(), key=lambda item: item.metadata.get('Name', '').casefold()):
        name = dist.metadata.get('Name', 'unknown')
        safe = ''.join(char if char.isalnum() or char in '-_.' else '_' for char in name)
        folder = destination / safe
        folder.mkdir(exist_ok=True)
        metadata = dist.read_text('METADATA')
        if metadata:
            (folder / 'METADATA.txt').write_text(metadata, encoding='utf-8')
        for file in dist.files or []:
            if any(term in str(file).lower() for term in ('license', 'copying', 'notice')):
                source = Path(dist.locate_file(file))
                if source.is_file() and source.stat().st_size <= 2 * 1024 * 1024:
                    relative = '__'.join(part for part in file.parts if part not in ('..', '.'))
                    (folder / relative).write_bytes(source.read_bytes())
        index.append({'name': name, 'version': dist.version,
            'license': dist.metadata.get('License-Expression') or dist.metadata.get('License'),
            'project_urls': dist.metadata.get_all('Project-URL', [])})
    (destination / 'INDEX.json').write_text(json.dumps(index, indent=2), encoding='utf-8')
