"""Build the standard Windows app from a clean, locked Python 3.12 environment."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from word_lookup_version import __version__  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true', help='Exercise the packaged app without game hooks')
    args = parser.parse_args()
    if sys.platform != 'win32' or sys.version_info[:2] != (3, 12):
        raise SystemExit('Use Windows and Python 3.12 with requirements-build.lock.')
    from collect_licenses import collect
    from compile_translations import main as compile_translations
    compile_translations()
    collect(ROOT / 'build' / 'THIRD_PARTY_LICENSES')
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm',
                    str(ROOT / 'WordLookup.spec')], cwd=ROOT, check=True)
    directory = ROOT / 'dist' / 'WordLookup'
    if args.smoke:
        import tempfile
        with tempfile.TemporaryDirectory(prefix='wordlookup-build-') as profile:
            report = Path(profile) / 'smoke.json'
            subprocess.run([str(directory / 'WordLookup.exe'), '--self-test', str(report)],
                env={**os.environ, 'WORD_LOOKUP_DATA_DIR': profile}, timeout=60, check=True)
            result = json.loads(report.read_text(encoding='utf-8'))
            if result.get('version') != __version__ or not result.get('ok'):
                raise SystemExit('Packaged self-test failed')
    archive = ROOT / 'dist' / f'WordLookup-{__version__}-Windows-x64.zip'
    timestamp = (2026, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as handle:
        for file in sorted(directory.rglob('*')):
            if file.is_file():
                info = zipfile.ZipInfo('WordLookup/' + file.relative_to(directory).as_posix(), timestamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                handle.writestr(info, file.read_bytes())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{digest}  {archive.name}\n', encoding='ascii')
    print(archive)


if __name__ == '__main__':
    main()
