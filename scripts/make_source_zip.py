"""Build a source-only zip for local handoff. Excludes caches, venvs, and private data."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {'.git', '.venv', 'venv', '__pycache__', '.pytest_cache', '.mypy_cache', 'dist', 'reports', 'uploads'}
EXCLUDE_SUFFIXES = {'.pyc', '.pyo', '.log'}


def include(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if any(part in EXCLUDE_DIRS for part in relative.parts):
        return False
    if path.suffix in EXCLUDE_SUFFIXES or path.name == '.DS_Store':
        return False
    if path.suffix == '.zip':
        return False
    return path.is_file()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write('Usage: python3 scripts/make_source_zip.py OUTPUT.zip\n')
        return 2
    destination = Path(argv[1]).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(path for path in ROOT.rglob('*') if include(path))
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(ROOT).as_posix())
    sys.stdout.write(f'Wrote {destination} ({len(files)} files)\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
