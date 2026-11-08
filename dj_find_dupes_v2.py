#!/usr/bin/env python3
"""Filename-only, conservative DJ candidate review. Never modifies source files.

Version 2.1.0 keeps the supplied v2 filename rules. It adds a stream reader,
spreadsheet-cell neutralization for report CSVs, and rejects NUL/CR/LF or
overlong path cells. Directory walk and SHA-256 helpers stay Linux-only and
are not used by the review app.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

ENGINE_NAME = 'dj_find_dupes_v2'
ENGINE_VERSION = '2.1.0'
ANALYSIS_TIMEZONE = 'America/New_York'
SNAPSHOT_DATE_PROVENANCE = 'operator-supplied; not inferred from file mtime'
MAX_INVENTORY_ROWS = 200_000
MAX_PATH_CHARS = 8192
AUDIO_EXTENSIONS = frozenset('.mp3 .m4a .wav .flac .aiff .aif .aac .ogg .opus .wma .alac'.split())
# Only explicit descriptor contexts are parsed. Longer alternatives win first.
ALIASES = {
    'bootleg': ['bootleg'], 'mashup': ['mashup'], 'blend': ['blend'],
    'versus': ['versus', 'vs'], 'vip': ['vip'], 'dubplate': ['dubplate'],
    'dub': ['dub'], 'refix': ['refix'], 'rework': ['rework'], 'edit': ['edit'],
    'redrum': ['re drum', 'redrum'], 'reintro': ['re intro', 'reintro'],
    'intro': ['intro'], 'outro': ['outro'], 'transition': ['transition'],
    'quick hit': ['quick hit', 'quickhit'], 'short edit': ['short edit'],
    'hype': ['hype'], 'breakdown': ['breakdown'], 'percapella': ['percapella'],
    'acapella': ['acapella', 'acap', 'accapella', 'a cappella'],
    'acapella intro': ['acapella intro', 'acap intro', 'accapella intro'],
    'club mix': ['club mix'], 'extended': ['extended'], 'radio edit': ['radio edit'],
    'dj tool': ['dj tool', 'djtools'], 'party starter': ['party starter'],
    'hype intro': ['hype intro', 'hypeintro'],
    'super clean': ['super clean', 'superclean'], 'dirty': ['dirty'], 'clean': ['clean'],
    'instrumental': ['instrumental', 'inst'], 'vocal': ['vocal', 'full vocal'],
    'phonk mix': ['phonk mix'], 'jersey club remix': ['jersey club remix'],
    'baltimore club': ['baltimore club'], 'remix': ['remix'], 'mix': ['mix'],
    'apercu': ['apercu'],
}
LOOKUP = {alias: tag for tag, aliases in ALIASES.items() for alias in aliases}
TAG_RE = re.compile(r'(?<!\w)(?:' + '|'.join(re.escape(s) for s in
                    sorted(LOOKUP, key=lambda x: (-len(x.split()), -len(x), x))) + r')(?!\w)')
BRACKET_RE = re.compile(r'\s*(?:\(([^()]*)\)|\[([^\[\]]*)\])\s*$')
LOCAL_FS = {'overlay', 'ext4', 'ext3', 'ext2', 'xfs', 'btrfs', 'tmpfs', 'ramfs', 'zfs'}


def normalized(text):
    text = unicodedata.normalize('NFKC', text).casefold()
    text = text.translate(str.maketrans({'–': '-', '—': '-', '‑': '-', '_': ' '}))
    return ' '.join(text.split())


def descriptor(text):
    """Return markers and retained qualifier. Unknown context is kept verbatim-normalized."""
    raw = normalized(text)
    match_text = re.sub(r'[-\s]+', ' ', raw)
    matches = list(TAG_RE.finditer(match_text))
    tags = {LOOKUP[m.group()] for m in matches}
    # 'versus' alone is often a title/credit, not a version descriptor.
    if tags == {'versus'}:
        return (), raw
    residual = TAG_RE.sub(' ', match_text)
    residual = re.sub(r'[\s,;/+&.\-]+', '', residual)
    # Names, numbers, unknown qualifiers, punctuation remain key material.
    qualifier = raw if residual else ''
    return tuple(sorted(tags)), qualifier


def parse_dj_filename(filename):
    """Structured suffixes only; unannotated names remain uncertain, never Original."""
    stem = normalized(Path(filename).stem)
    core = stem
    blocks = []
    # Keep numeric brackets intact: '(2)' is merely a review hint, not removed.
    copy_hint = bool(re.search(r'\(\d+\)\s*$', stem))
    while True:
        match = BRACKET_RE.search(core)
        if not match:
            break
        block = next(x for x in match.groups() if x is not None)
        if block.strip().isdigit():
            break
        tags, qualifier = descriptor(block)
        # Unknown bracketed text is retained too, but never changes its identity.
        blocks.append((tags, qualifier or (normalized(block) if not tags else '')))
        core = core[:match.start()].strip()
    # An explicit third segment is a conventional version suffix; never artist/title.
    pieces = re.split(r'\s+-\s+', core)
    while len(pieces) >= 3:
        tags, qualifier = descriptor(pieces[-1])
        if not tags:
            break
        blocks.append((tags, qualifier))
        pieces.pop()
        core = ' - '.join(pieces)
    tags = sorted({tag for block_tags, _ in blocks for tag in block_tags})
    qualifiers = sorted(q for _, q in blocks if q)
    signature = json.dumps({'markers': tags, 'qualifiers': qualifiers}, ensure_ascii=False, sort_keys=True)
    return {'core': core, 'signature': signature,
            'version_state': 'annotated_uncertain' if blocks else 'unlabeled_uncertain',
            'copy_hint': copy_hint}


def inside(path, root):
    return path == root or root in path.parents


def csv_cell(value):
    """Prefix spreadsheet formula triggers. Grouping is unchanged.

    JSON reports keep exact text. This guard is only for CSV and plain-text
    report cells that a spreadsheet might execute.
    """
    text = '' if value is None else str(value)
    probe = text.lstrip(' \t')
    if text[:1] in '\t\r' or (probe and probe[0] in '=+-@'):
        return "'" + text
    return text


def safe_local_path(path, root):
    """Reject links/remote mounts BEFORE traversing them; Linux mount table required.

    Not used by the review app. A missing mount table means this host is
    unsupported for live reads; callers must use a saved CSV instead.
    """
    path, root = Path(path).absolute(), Path(root).absolute()
    if root in (Path('/'), Path.home()) or not inside(path, root):
        raise ValueError('path outside bounded allow-root, or unsafe root')
    if '..' in path.parts or '..' in root.parts:
        raise ValueError('parent traversal forbidden')
    mountinfo = Path('/proc/self/mountinfo')
    if not mountinfo.exists():
        raise ValueError('local filesystem verification unavailable on this host; use saved CSV')
    mounts = []
    for line in mountinfo.read_text().splitlines():
        left, right = line.split(' - ', 1)
        target = left.split()[4]
        target = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), target)
        mounts.append((Path(target), right.split()[0]))
    # Check each component so a network-mounted ancestor cannot be traversed first.
    parts = [Path('/'), *reversed(path.parents), path]
    for part in dict.fromkeys(parts):
        applicable = [(m, fs) for m, fs in mounts if inside(part, m)]
        _, fs = max(applicable, key=lambda item: len(str(item[0])))
        if fs not in LOCAL_FS:
            raise ValueError('non-allowlisted filesystem: ' + fs)
        if stat.S_ISLNK(part.lstat().st_mode):
            raise ValueError('symlink forbidden: ' + str(part))
    return path


def read_inventory(filename, path_column, scope, errors, counts):
    with open(filename, newline='', encoding='utf-8-sig') as handle:
        yield from read_inventory_stream(handle, path_column, scope, errors, counts)


def read_inventory_stream(handle, path_column, scope, errors, counts):
    """Read inventory text already in memory or in an open text file.

    Yields path strings from the CSV. Does not stat, open, or resolve them.
    """
    root = PurePosixPath(scope)
    seen = set()
    reader = csv.DictReader(handle)
    headers = reader.fieldnames or []
    if len(set(headers)) != len(headers):
        raise ValueError('duplicate CSV headers')
    if path_column is None:
        found = [h for h in ('path', 'full_path', 'filepath', 'file_path') if h in headers]
        if len(found) != 1:
            raise ValueError('ambiguous/missing path column; specify --path-column')
        path_column = found[0]
    if path_column not in headers:
        raise ValueError('path column not found: ' + path_column)
    for number, row in enumerate(reader, 2):
        counts['input_rows'] += 1
        if counts['input_rows'] > MAX_INVENTORY_ROWS:
            raise ValueError(f'inventory row budget exceeded ({MAX_INVENTORY_ROWS} rows)')
        raw = row.get(path_column)
        if None in row or not raw or any(ch in raw for ch in '\x00\r\n'):
            errors.append({'line': number, 'error': 'malformed row or empty path'})
            continue
        if len(raw) > MAX_PATH_CHARS:
            errors.append({
                'line': number,
                'error': 'path cell exceeds character budget',
                'path': raw[:120],
            })
            continue
        path = PurePosixPath(raw)
        if not path.is_absolute() or '..' in path.parts or not inside(path, root):
            counts['out_of_scope'] += 1
            errors.append({'line': number, 'error': 'out-of-scope or invalid path', 'path': raw})
            continue
        if row.get('status', 'ok').casefold() != 'ok':
            errors.append({'line': number, 'error': 'inventory status is not ok', 'path': raw})
            continue
        if path.suffix.casefold() not in AUDIO_EXTENSIONS:
            counts['unsupported_rows'] += 1
            continue
        if str(path) in seen:
            counts['repeated_path_rows'] += 1
            continue
        seen.add(str(path))
        yield str(path)


def enumerate_local(root, max_files, errors, counts):
    """Optional Linux-only directory walk. The review app must not call this."""
    root = safe_local_path(root, root)
    if not root.is_dir():
        raise ValueError('directory input is not a directory')
    examined = 0
    def walk_error(exc):
        errors.append({'error': str(exc)})
    for base, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        allowed = []
        for name in sorted(dirs):
            examined += 1
            counts['directory_entries'] += 1
            if examined > max_files:
                errors.append({'error': 'directory entry budget exceeded; enumeration incomplete'})
                return
            try:
                safe_local_path(Path(base) / name, root)
                allowed.append(name)
            except (OSError, ValueError) as exc:
                errors.append({'path': str(Path(base) / name), 'error': str(exc)})
        dirs[:] = allowed
        for name in sorted(files):
            examined += 1
            if examined > max_files:
                errors.append({'error': 'directory entry budget exceeded; enumeration incomplete'})
                return
            counts['input_rows'] += 1
            path = Path(base) / name
            try:
                safe_local_path(path, root)
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError('not a regular file')
                if path.suffix.casefold() in AUDIO_EXTENSIONS:
                    yield str(path)
                else:
                    counts['unsupported_rows'] += 1
            except (OSError, ValueError) as exc:
                errors.append({'path': str(path), 'error': str(exc)})


def candidate_groups(paths):
    groups = defaultdict(list)
    for path in paths:
        parsed = parse_dj_filename(PurePosixPath(path).name)
        groups[(parsed['core'], parsed['signature'])].append({'path': path, **parsed})
    result = {}
    for (core, signature), rows in sorted(groups.items()):
        if core and len(rows) > 1:
            group_id = hashlib.sha256((core + '\0' + signature).encode()).hexdigest()[:16]
            result[group_id] = rows
    return result


def hash_selected(groups, selected, allow_root, max_files, max_bytes, errors):
    """Explicit selected groups only. SHA256 means byte identity, not version suitability.

    Linux-only. Not exposed by the review app. A hash is not deletion authority.
    """
    if not selected:
        return {'files_opened': 0, 'bytes_read': 0}
    if not allow_root:
        raise ValueError('--allow-root required with --hash-group')
    if set(selected) - set(groups):
        raise ValueError('unknown hash group; first run filename report and select its group IDs')
    files_opened = bytes_read = 0
    for group_id in sorted(set(selected)):
        for row in groups[group_id]:
            row.pop('sha256', None)
            row.pop('proof', None)
            row['hash_status'] = 'not_hashed'
            try:
                path = safe_local_path(row['path'], allow_root)
                before = path.lstat()
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError('not a regular file')
                if files_opened >= max_files or bytes_read + before.st_size > max_bytes:
                    raise ValueError('hash budget exceeded')
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                files_opened += 1
                with os.fdopen(fd, 'rb') as handle:
                    start = os.fstat(handle.fileno())
                    if (start.st_ino, start.st_dev, start.st_size, start.st_mtime_ns) != (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns):
                        raise ValueError('source changed before read')
                    digest = hashlib.sha256()
                    remaining = start.st_size
                    while remaining:
                        chunk = handle.read(min(1024 * 1024, remaining, max_bytes - bytes_read))
                        if not chunk:
                            raise ValueError('source truncated or hash budget exhausted')
                        bytes_read += len(chunk)
                        remaining -= len(chunk)
                        digest.update(chunk)
                    end = os.fstat(handle.fileno())
                    if (start.st_ino, start.st_dev, start.st_size, start.st_mtime_ns, start.st_ctime_ns) != (end.st_ino, end.st_dev, end.st_size, end.st_mtime_ns, end.st_ctime_ns):
                        raise ValueError('source changed during read')
                current = path.lstat()
                if (current.st_ino, current.st_dev, current.st_size, current.st_mtime_ns) != (end.st_ino, end.st_dev, end.st_size, end.st_mtime_ns):
                    raise ValueError('source path changed after read')
                row['sha256'] = digest.hexdigest()
                row['hash_status'] = 'hashed'
            except (OSError, ValueError) as exc:
                row['hash_status'] = 'error'
                errors.append({'path': row['path'], 'error': str(exc)})
        hashes = defaultdict(list)
        for row in groups[group_id]:
            if row.get('sha256'):
                hashes[row['sha256']].append(row)
        for rows in hashes.values():
            if len(rows) > 1:
                for row in rows:
                    row['proof'] = 'byte_identical_sha256'
    return {'files_opened': files_opened, 'bytes_read': bytes_read}


def write_reports(output, groups, summary, errors):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    fields = ['group_id', 'evidence', 'path', 'core', 'signature', 'version_state', 'copy_hint', 'hash_status', 'sha256', 'proof']
    with (output / 'filename_candidates.csv').open('x', newline='', encoding='utf-8-sig') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for group_id, rows in groups.items():
            for row in rows:
                record = {'group_id': group_id, 'evidence': 'filename_candidates',
                          'hash_status': 'not_requested', 'sha256': '', 'proof': 'none', **row}
                writer.writerow({key: csv_cell(record.get(key, '')) for key in fields})
    with (output / 'filename_candidates.txt').open('x', encoding='utf-8') as handle:
        handle.write('DJ FILENAME REVIEW CANDIDATES — NOT DUPLICATE PROOF\n')
        handle.write('BYTE EVIDENCE IS NOT AUDIO IDENTITY. A MATCH DOES NOT AUTHORIZE DELETION.\n')
        handle.write(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
        for group_id, rows in groups.items():
            handle.write(f'\n{group_id} | {csv_cell(rows[0]["core"])} | {csv_cell(rows[0]["signature"])}\n')
            for row in rows:
                handle.write(f'  {csv_cell(row["path"])} | proof={csv_cell(row.get("proof", "none"))}\n')
        handle.write('\nERRORS\n' + json.dumps(errors, ensure_ascii=False, indent=2) + '\n')
    for name, data in [('summary.json', summary), ('errors.json', errors)]:
        with (output / name).open('x', encoding='utf-8') as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--inventory', type=Path)
    source.add_argument('--directory', type=Path, help='Explicit bounded local path; Linux local-filesystem verification required')
    parser.add_argument('--path-column')
    parser.add_argument('--scope', required=True, help='Absolute allowed inventory path prefix / directory root')
    parser.add_argument('--snapshot-date', type=date.fromisoformat)
    parser.add_argument('--output', type=Path, required=True, help='New, empty report directory; existing files never overwritten')
    parser.add_argument('--max-files', type=int, default=10000)
    parser.add_argument('--hash-group', action='append', default=[])
    parser.add_argument('--allow-root', type=Path)
    parser.add_argument('--max-hash-files', type=int, default=20)
    parser.add_argument('--max-hash-bytes', type=int, default=256 * 1024 * 1024)
    args = parser.parse_args(argv)
    if not PurePosixPath(args.scope).is_absolute() or '..' in PurePosixPath(args.scope).parts or args.scope == '/':
        parser.error('--scope must be a bounded absolute path, not root')
    if not (1 <= args.max_files <= 100000 and 1 <= args.max_hash_files <= 100 and 1 <= args.max_hash_bytes <= 1024**3):
        parser.error('budgets must be positive and <= 100000 entries / 100 hashes / 1 GiB')
    if args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir())):
        parser.error('--output must be absent or empty (no overwrites)')
    if args.directory and Path(args.scope).absolute() != args.directory.absolute():
        parser.error('--scope must equal the supplied directory')
    # Never allow reports to be written into the input music tree.
    if inside(args.output.resolve(), Path(args.scope).resolve()):
        parser.error('reports cannot be written inside source scope')
    now = datetime.now(ZoneInfo(ANALYSIS_TIMEZONE))
    errors = []
    counts = defaultdict(int, input_rows=0, out_of_scope=0, unsupported_rows=0, repeated_path_rows=0)
    fatal = False
    paths = []
    groups = {}
    hash_counts = {'files_opened': 0, 'bytes_read': 0}
    try:
        if args.inventory:
            paths = list(read_inventory(args.inventory, args.path_column, args.scope, errors, counts))
        else:
            paths = list(enumerate_local(args.directory, args.max_files, errors, counts))
        groups = candidate_groups(paths)
        hash_counts = hash_selected(groups, args.hash_group, args.allow_root, args.max_hash_files, args.max_hash_bytes, errors)
    except (OSError, ValueError, UnicodeError, csv.Error) as exc:
        errors.append({'error': str(exc), 'fatal': True})
        fatal = True
    summary = {'generated_at': now.isoformat(), 'timezone': ANALYSIS_TIMEZONE,
               'engine_name': ENGINE_NAME, 'engine_version': ENGINE_VERSION,
               'source': str(args.inventory or args.directory), 'mode': 'saved_filename_inventory' if args.inventory else 'bounded_local_filenames',
               'scope': args.scope, 'snapshot_date': str(args.snapshot_date) if args.snapshot_date else 'unknown',
               'snapshot_age_days': (now.date() - args.snapshot_date).days if args.snapshot_date else None,
               'snapshot_date_provenance': SNAPSHOT_DATE_PROVENANCE,
               **counts, 'audio_paths': len(paths), 'candidate_groups': len(groups),
               'candidate_files': sum(map(len, groups.values())), 'error_count': len(errors),
               'status': 'fatal_error' if fatal else ('partial_review_errors' if errors else 'complete_filename_review'),
               'hash_requested': bool(args.hash_group), **hash_counts,
               'source_changes': 0, 'evidence': 'filename_candidates are not proof; unlabeled is uncertain',
               'byte_evidence': 'not_checked' if not args.hash_group else 'sha256_opt_in',
               'audio_evidence': 'not_checked', 'deletion_authorized': False,
               'audio_metadata_read': False, 'audio_content_read': bool(hash_counts['files_opened'])}
    write_reports(args.output, groups, summary, errors)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 2 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
