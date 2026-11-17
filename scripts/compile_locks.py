"""Regenerate universal locks with uv 0.12.23, without Torch/Stanza/spaCy."""
import argparse
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upgrade', action='store_true', help='Refresh transitive pins as well')
    args = parser.parse_args()
    version = subprocess.run(['uv', '--version'], capture_output=True, text=True, check=True).stdout.split()[1]
    if version != '0.12.23':
        raise SystemExit('Install uv==0.12.23 for these lock files.')
    for source, target in [('requirements-dev.txt', 'requirements.lock'),
                           ('requirements-build.txt', 'requirements-build.lock'),
                           ('requirements-offline.txt', 'requirements-offline.lock')]:
        command = ['uv', 'pip', 'compile', '--universal', '--python-version', '3.11', source, '-o', target]
        if args.upgrade:
            command.append('--upgrade')
        subprocess.run(command, cwd=ROOT, env=os.environ.copy(), check=True, stdout=subprocess.DEVNULL)
        if 'offline' in source:
            text = (ROOT / target).read_text(encoding='utf-8')
            if any(name + '==' in text for name in ('torch', 'stanza', 'spacy', 'nvidia-cublas-cu12')):
                raise RuntimeError('Unexpected heavyweight dependency in local runtime')


if __name__ == '__main__':
    main()
