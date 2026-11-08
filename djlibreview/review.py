"""Saved-inventory review. Paths from the CSV are text and are never opened."""

from __future__ import annotations

import csv
import io
import json
import math
import re
import secrets
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

import dj_find_dupes_v2 as dj

from djlibreview import APP_NAME, APP_VERSION

MAX_UPLOAD_BYTES = 48 * 1024 * 1024
MAX_ERROR_DETAILS = 500
GROUP_FILE_PREVIEW = 8
DEFAULT_PAGE_SIZE = 6
EXAMPLE_SCOPE = '/synthetic/dj-library'
EXAMPLE_SNAPSHOT_DATE = '2026-03-01'
EXAMPLE_DATASET_LABEL = 'Example synthetic library'
EXAMPLE_SOURCE_NAME = 'synthetic_inventory.csv'
EVIDENCE_CODE = 'filename_candidate_not_confirmed_audio_duplicate'
EVIDENCE_LABEL = 'Filename candidate — not a confirmed audio duplicate'
ACTION_ALLOWED = 'review_only_do_not_delete'

_EXAMPLE_PATH = Path(__file__).resolve().parent / 'examples' / 'synthetic_inventory.csv'
EXAMPLE_BYTES = _EXAMPLE_PATH.read_bytes()

_VERSION_LABELS = {
    'annotated_uncertain': 'Annotated, still uncertain',
    'unlabeled_uncertain': 'Unlabeled, uncertain — not Original',
}
_ROW_MESSAGES = {
    'malformed row or empty path': (
        'This row is empty, missing a path, or contains a line break or null. It was skipped and not opened.'
    ),
    'out-of-scope or invalid path': (
        'This path is not an absolute path inside the scope, or it contains "..". It was skipped and not opened.'
    ),
    'inventory status is not ok': 'The status column is not ok, so this row was skipped and not opened.',
    'path cell exceeds character budget': (
        'This path cell is longer than 8192 characters. It was skipped and not opened.'
    ),
}


class ReviewRejected(Exception):
    """The request is invalid. A previous in-memory review is left as it was."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ReviewFile:
    path: str
    filename: str
    folder_text: str
    extension: str
    core: str
    signature: str
    version_state: str
    version_state_label: str
    copy_hint: bool
    markers: tuple[str, ...]
    qualifiers: tuple[str, ...]


@dataclass(frozen=True)
class CandidateGroup:
    group_id: str
    core: str
    signature: str
    markers: tuple[str, ...]
    qualifiers: tuple[str, ...]
    version_state: str
    version_state_label: str
    evidence_label: str
    uncertainty: str
    comparison_note: str
    files: tuple[ReviewFile, ...]
    haystack: str

    @property
    def any_copy_hint(self) -> bool:
        return any(item.copy_hint for item in self.files)


@dataclass(frozen=True)
class ReviewResult:
    status: str
    example_data: bool
    dataset_label: str
    source_name: str
    scope: str
    snapshot_date: str
    snapshot_age_days: int
    age_note: str
    analysis_display: str
    analysis_generated_at: str
    path_column: str
    input_rows: int
    audio_paths: int
    out_of_scope: int
    unsupported_rows: int
    repeated_path_rows: int
    error_count: int
    error_details_truncated: bool
    errors: tuple[dict[str, object], ...]
    groups: dict[str, CandidateGroup]


def version_state_label(state: str) -> str:
    return _VERSION_LABELS.get(state, state + ' — uncertain')


def fold(text: str) -> str:
    return unicodedata.normalize('NFKC', text).casefold()


def same_bytes(left: bytes, right: bytes) -> bool:
    if len(left) != len(right):
        return False
    return secrets.compare_digest(left, right)


def clean_label(value: str) -> str:
    text = ' '.join(str(value).split())
    if not text:
        raise ReviewRejected('Name this dataset. The label is shown on screen and in the report.')
    if len(text) > 200:
        raise ReviewRejected('Dataset label must be 200 characters or fewer.')
    return text


def clean_source_name(value: str) -> str:
    name = str(value).replace('\\', '/').split('/')[-1]
    name = ''.join(ch for ch in name if ch >= ' ' and ch != '\x7f')
    name = name.strip()[:180]
    return name or 'inventory.csv'


def clean_scope(value: str) -> str:
    text = str(value).strip()
    path = PurePosixPath(text)
    if (
        not text
        or not path.is_absolute()
        or text == '/'
        or '..' in path.parts
        or any(ch in text for ch in '\x00\r\n')
        or len(text) > 4096
    ):
        raise ReviewRejected(
            'Scope must be a bounded absolute path such as /synthetic/dj-library. '
            'The filesystem root is not allowed. This desk does not guess a folder.'
        )
    return text


def clean_snapshot(value: str) -> date:
    text = str(value).strip()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        raise ReviewRejected('Snapshot date must be YYYY-MM-DD. This desk does not assume today.')
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ReviewRejected(
            'Snapshot date must be a real calendar date. This desk does not assume today.'
        ) from None


def clean_path_column(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > 200 or any(ord(ch) < 32 or ch == '\x7f' for ch in text):
        raise ReviewRejected('Path column name is empty or has control characters.')
    return text


def map_value_error(message: str) -> str:
    known = {
        'duplicate CSV headers': (
            'The CSV header row repeats a column name. Fix the header and import again. '
            'No music path was opened.'
        ),
        'ambiguous/missing path column; specify --path-column': (
            'The path column is ambiguous or missing. Use a header named path, full_path, '
            'filepath, or file_path, or type the column name in Path column. No music path was opened.'
        ),
    }
    if message in known:
        return known[message]
    if message.startswith('path column not found:'):
        column = message.split(':', 1)[1].strip()
        column = ''.join(ch for ch in column if ch >= ' ')[:80]
        return f'The path column "{column}" is not in the header. No music path was opened.'
    if message.startswith('inventory row budget exceeded'):
        return message + '. No candidate groups were produced. No music path was opened.'
    return 'The inventory could not be reviewed. No music path was opened.'


def present_error(error: dict) -> dict[str, object]:
    message = str(error.get('error') or 'Row problem')
    shown: dict[str, object] = {'message': _ROW_MESSAGES.get(message, message)}
    if isinstance(error.get('line'), int):
        shown['line'] = error['line']
    if error.get('fatal'):
        shown['fatal'] = True
    raw_path = error.get('path')
    if raw_path is not None and not error.get('fatal'):
        path = str(raw_path)
        if len(path) > 2000:
            path = path[:2000] + '…'
        shown['path'] = path
    return shown


def _age_note(snapshot: date, analysis_day: date) -> str:
    days = (analysis_day - snapshot).days
    if days > 0:
        return (
            f'{days} days between the snapshot date you entered and the analysis calendar day. '
            'This is not file age and does not show that the files still exist.'
        )
    if days < 0:
        return (
            'The snapshot date is after the analysis calendar day. The date was not changed. '
            'This is not file age.'
        )
    return (
        'The snapshot date matches the analysis calendar day in America/New_York. '
        'That only means the date you typed. It is not file mtime.'
    )


def _file_from_row(row: dict) -> ReviewFile:
    path = str(row['path'])
    pure = PurePosixPath(path)
    signature = json.loads(row['signature'])
    state = str(row['version_state'])
    markers = tuple(signature.get('markers') or [])
    qualifiers = tuple(signature.get('qualifiers') or [])
    return ReviewFile(
        path=path,
        filename=pure.name,
        folder_text=str(pure.parent),
        extension=pure.suffix,
        core=str(row['core']),
        signature=str(row['signature']),
        version_state=state,
        version_state_label=version_state_label(state),
        copy_hint=bool(row['copy_hint']),
        markers=markers,
        qualifiers=qualifiers,
    )


def _comparison_note(files: tuple[ReviewFile, ...]) -> str:
    names_differ = len({item.filename for item in files}) > 1
    extensions_differ = len({item.extension.casefold() for item in files}) > 1
    if names_differ:
        note = (
            'Original filenames in this group are not identical. '
            'They matched on the normalized core and version signature only.'
        )
    else:
        note = (
            'Original filenames in this group match as text. '
            'That still does not prove the audio matches.'
        )
    if extensions_differ:
        note += ' Extensions differ. An extension is not a version marker, and the audio was not compared.'
    return note


def _uncertainty(files: tuple[ReviewFile, ...], markers: tuple[str, ...], qualifiers: tuple[str, ...], state: str) -> str:
    parts = [
        'These imported path strings share a normalized filename core and version signature. '
        'That makes them filename candidates only. It does not prove the audio matches.',
        'Byte hashes were not checked. Audio metadata, tags, waveforms, and fingerprints were not read. '
        'This snapshot does not show whether any of these paths exist on a disk now.',
        'A filename match never authorizes deleting, moving, renaming, or retagging a music file.',
    ]
    if state == 'unlabeled_uncertain':
        parts.append(
            'No structured version suffix was found. Unlabeled is uncertain. '
            'It is not treated as Original.'
        )
    else:
        if markers:
            parts.append(
                'Known version markers in the signature: ' + ', '.join(markers) + '. '
                'Different marker sets stay in different groups. The same known words in a different '
                'order can land in one group when no other qualifier identifies the version. '
                'That is still only a filename candidate.'
            )
        if qualifiers:
            parts.append(
                'Retained qualifier text is part of identity: ' + '; '.join(qualifiers) + '. '
                'A different editor, remix name, date, BPM range, or unknown word would be a different group.'
            )
        else:
            parts.append(
                'No extra qualifier remained after known markers. If an editor, remix name, date, or BPM '
                'range had been written in the filename, it would have stayed in the signature and would '
                'not be in this group.'
            )
    if any(item.copy_hint for item in files):
        parts.append(
            'A trailing parenthetical number such as (2) is only a copy hint. It stays inside the matched '
            'filename core. It was not stripped, and it is not proof the audio matches.'
        )
    return ' '.join(parts)


def _make_group(group_id: str, files: tuple[ReviewFile, ...]) -> CandidateGroup:
    first = files[0]
    haystack = fold('\n'.join([
        first.core,
        *first.markers,
        *first.qualifiers,
        *[piece for item in files for piece in (item.filename, item.path, item.folder_text, item.extension)],
    ]))
    return CandidateGroup(
        group_id=group_id,
        core=first.core,
        signature=first.signature,
        markers=first.markers,
        qualifiers=first.qualifiers,
        version_state=first.version_state,
        version_state_label=first.version_state_label,
        evidence_label=EVIDENCE_LABEL,
        uncertainty=_uncertainty(files, first.markers, first.qualifiers, first.version_state),
        comparison_note=_comparison_note(files),
        files=files,
        haystack=haystack,
    )


def _fatal_result(
    *,
    message: str,
    dataset_label: str,
    source_name: str,
    scope: str,
    snapshot: date,
    path_column: str | None,
    example_data: bool,
    now: datetime,
    errors: list[dict] | None = None,
    input_rows: int = 0,
) -> ReviewResult:
    detail = list(errors or [])
    detail.append({'error': message, 'fatal': True})
    public = tuple(present_error(item) for item in detail[:MAX_ERROR_DETAILS])
    return ReviewResult(
        status='fatal_error',
        example_data=example_data,
        dataset_label=dataset_label,
        source_name=source_name,
        scope=scope,
        snapshot_date=snapshot.isoformat(),
        snapshot_age_days=(now.date() - snapshot).days,
        age_note=_age_note(snapshot, now.date()),
        analysis_display=now.strftime('%Y-%m-%d %H:%M') + ' ' + dj.ANALYSIS_TIMEZONE,
        analysis_generated_at=now.isoformat(),
        path_column=path_column or '',
        input_rows=input_rows,
        audio_paths=0,
        out_of_scope=0,
        unsupported_rows=0,
        repeated_path_rows=0,
        error_count=len(detail),
        error_details_truncated=len(detail) > MAX_ERROR_DETAILS,
        errors=public,
        groups={},
    )


def _blank_inventory(data: bytes) -> bool:
    payload = data[3:] if data.startswith(b'\xef\xbb\xbf') else data
    return not payload.strip()


def analyze_csv_bytes(
    data: bytes,
    *,
    dataset_label: str,
    source_name: str,
    scope: str,
    snapshot_date: str,
    path_column: str | None,
) -> ReviewResult:
    """Group filename candidates from CSV bytes. Does not open the path strings."""
    label = clean_label(dataset_label)
    source = clean_source_name(source_name)
    bounded_scope = clean_scope(scope)
    snapshot = clean_snapshot(snapshot_date)
    column = clean_path_column(path_column)
    if len(data) > MAX_UPLOAD_BYTES:
        raise ReviewRejected(
            f'The inventory is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB local limit.'
        )
    example_data = same_bytes(data, EXAMPLE_BYTES)
    now = datetime.now(ZoneInfo(dj.ANALYSIS_TIMEZONE))
    if _blank_inventory(data):
        return _fatal_result(
            message='The inventory file is empty. No music path was opened.',
            dataset_label=label,
            source_name=source,
            scope=bounded_scope,
            snapshot=snapshot,
            path_column=column,
            example_data=example_data,
            now=now,
        )
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        return _fatal_result(
            message='Inventory text must be UTF-8. No music path was opened.',
            dataset_label=label,
            source_name=source,
            scope=bounded_scope,
            snapshot=snapshot,
            path_column=column,
            example_data=example_data,
            now=now,
        )
    if not text.strip():
        return _fatal_result(
            message='The inventory file is empty. No music path was opened.',
            dataset_label=label,
            source_name=source,
            scope=bounded_scope,
            snapshot=snapshot,
            path_column=column,
            example_data=example_data,
            now=now,
        )
    errors: list[dict] = []
    counts: dict[str, int] = defaultdict(int, input_rows=0, out_of_scope=0, unsupported_rows=0, repeated_path_rows=0)
    try:
        paths = list(dj.read_inventory_stream(io.StringIO(text), column, bounded_scope, errors, counts))
        raw_groups = dj.candidate_groups(paths)
    except UnicodeError:
        return _fatal_result(
            message='Inventory text must be UTF-8. No music path was opened.',
            dataset_label=label,
            source_name=source,
            scope=bounded_scope,
            snapshot=snapshot,
            path_column=column,
            example_data=example_data,
            now=now,
            errors=errors,
            input_rows=counts['input_rows'],
        )
    except csv.Error as exc:
        detail = str(exc).replace('\n', ' ')[:300]
        return _fatal_result(
            message=f'The CSV could not be parsed ({detail}). No music path was opened.',
            dataset_label=label,
            source_name=source,
            scope=bounded_scope,
            snapshot=snapshot,
            path_column=column,
            example_data=example_data,
            now=now,
            errors=errors,
            input_rows=counts['input_rows'],
        )
    except ValueError as exc:
        return _fatal_result(
            message=map_value_error(str(exc)),
            dataset_label=label,
            source_name=source,
            scope=bounded_scope,
            snapshot=snapshot,
            path_column=column,
            example_data=example_data,
            now=now,
            errors=errors,
            input_rows=counts['input_rows'],
        )
    except OSError:
        return _fatal_result(
            message='The inventory text could not be read. No music path was opened.',
            dataset_label=label,
            source_name=source,
            scope=bounded_scope,
            snapshot=snapshot,
            path_column=column,
            example_data=example_data,
            now=now,
            errors=errors,
            input_rows=counts['input_rows'],
        )
    ordered: dict[str, CandidateGroup] = {}
    for group_id, rows in raw_groups.items():
        files = tuple(sorted((_file_from_row(row) for row in rows), key=lambda item: item.path))
        ordered[group_id] = _make_group(group_id, files)
    public_errors = tuple(present_error(item) for item in errors[:MAX_ERROR_DETAILS])
    status = 'partial_review_errors' if errors else 'complete_filename_review'
    return ReviewResult(
        status=status,
        example_data=example_data,
        dataset_label=label,
        source_name=source,
        scope=bounded_scope,
        snapshot_date=snapshot.isoformat(),
        snapshot_age_days=(now.date() - snapshot).days,
        age_note=_age_note(snapshot, now.date()),
        analysis_display=now.strftime('%Y-%m-%d %H:%M') + ' ' + dj.ANALYSIS_TIMEZONE,
        analysis_generated_at=now.isoformat(),
        path_column=column or '',
        input_rows=counts['input_rows'],
        audio_paths=len(paths),
        out_of_scope=counts['out_of_scope'],
        unsupported_rows=counts['unsupported_rows'],
        repeated_path_rows=counts['repeated_path_rows'],
        error_count=len(errors),
        error_details_truncated=len(errors) > MAX_ERROR_DETAILS,
        errors=public_errors,
        groups=ordered,
    )


def analyze_example() -> ReviewResult:
    """Review the packaged synthetic inventory. Ignores any caller-supplied scope."""
    return analyze_csv_bytes(
        EXAMPLE_BYTES,
        dataset_label=EXAMPLE_DATASET_LABEL,
        source_name=EXAMPLE_SOURCE_NAME,
        scope=EXAMPLE_SCOPE,
        snapshot_date=EXAMPLE_SNAPSHOT_DATE,
        path_column=None,
    )


def _status_label(result: ReviewResult) -> str:
    labels = {
        'complete_filename_review': (
            'Filename review finished. Groups below are filename candidates, not confirmed audio duplicates.'
        ),
        'partial_review_errors': (
            'Filename review finished with row problems. Valid rows can be reviewed. '
            'Problems are listed. No music path was opened.'
        ),
        'fatal_error': (
            'This inventory could not be reviewed. No candidate groups were produced. No music path was opened.'
        ),
    }
    try:
        return labels[result.status]
    except KeyError:
        raise RuntimeError('unknown review status') from None


def _counts_display(result: ReviewResult) -> list[dict[str, object]]:
    not_grouped = max(0, result.audio_paths - sum(len(group.files) for group in result.groups.values()))
    rows = [
        ('input_rows', result.input_rows, 'CSV rows read'),
        ('audio_paths', result.audio_paths, 'Audio paths accepted as text'),
        ('candidate_groups', len(result.groups), 'Filename candidate groups'),
        ('candidate_files', sum(len(group.files) for group in result.groups.values()), 'Paths in those groups'),
        ('paths_not_grouped', not_grouped, 'Accepted paths not in a group'),
        ('repeated_path_rows', result.repeated_path_rows, 'Repeated path rows skipped'),
        ('out_of_scope', result.out_of_scope, 'Out of scope or invalid'),
        ('unsupported_rows', result.unsupported_rows, 'Non-audio rows skipped'),
        ('error_count', result.error_count, 'Row problems'),
    ]
    return [{'key': key, 'value': value, 'label': label} for key, value, label in rows]


def _assurances() -> list[dict[str, str]]:
    return [
        {'label': 'Byte evidence', 'value': 'Not checked'},
        {'label': 'Audio evidence', 'value': 'Not checked'},
        {'label': 'Current files', 'value': 'Not verified'},
        {'label': 'Deletion', 'value': 'Not authorized'},
    ]


def _provenance_rows(result: ReviewResult) -> list[dict[str, str]]:
    example = 'Yes — synthetic sample, not your library' if result.example_data else 'No'
    column = result.path_column or 'Chosen from the header (path, full_path, filepath, or file_path)'
    return [
        {'label': 'Dataset', 'value': result.dataset_label},
        {'label': 'Source file', 'value': result.source_name},
        {'label': 'Scope', 'value': result.scope},
        {'label': 'Snapshot date', 'value': result.snapshot_date},
        {'label': 'Snapshot date source', 'value': dj.SNAPSHOT_DATE_PROVENANCE},
        {'label': 'Snapshot age note', 'value': result.age_note},
        {'label': 'Analysis time', 'value': result.analysis_display},
        {'label': 'Analysis timestamp', 'value': result.analysis_generated_at},
        {'label': 'Analysis clock', 'value': dj.ANALYSIS_TIMEZONE},
        {'label': 'Engine', 'value': f'{dj.ENGINE_NAME} {dj.ENGINE_VERSION}'},
        {'label': 'App', 'value': f'{APP_NAME} {APP_VERSION}'},
        {'label': 'Mode', 'value': 'Saved inventory text only'},
        {'label': 'Path column', 'value': column},
        {'label': 'Example data', 'value': example},
    ]


def _facets(result: ReviewResult) -> dict[str, object]:
    markers = sorted({marker for group in result.groups.values() for marker in group.markers})
    states = sorted({group.version_state for group in result.groups.values()})
    return {
        'markers': markers,
        'version_states': [{'value': state, 'label': version_state_label(state)} for state in states],
        'groups_with_copy_hint': sum(1 for group in result.groups.values() if group.any_copy_hint),
    }


def _public_file(item: ReviewFile) -> dict[str, object]:
    return {
        'path': item.path,
        'filename': item.filename,
        'folder_text': item.folder_text,
        'extension': item.extension,
        'copy_hint': item.copy_hint,
        'version_state': item.version_state,
        'version_state_label': item.version_state_label,
    }


def _public_group(group: CandidateGroup, preview: int) -> dict[str, object]:
    return {
        'group_id': group.group_id,
        'evidence_label': group.evidence_label,
        'core': group.core,
        'markers': list(group.markers),
        'qualifiers': list(group.qualifiers),
        'version_state': group.version_state,
        'version_state_label': group.version_state_label,
        'file_count': len(group.files),
        'filenames_differ': len({item.filename for item in group.files}) > 1,
        'extensions_differ': len({item.extension.casefold() for item in group.files}) > 1,
        'copy_hint': group.any_copy_hint,
        'comparison_note': group.comparison_note,
        'uncertainty': group.uncertainty,
        'files': [_public_file(item) for item in group.files[:preview]],
    }


def _as_int(value: object, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReviewRejected('The filter was not valid.')
    return max(low, min(high, value))


def _as_text(value: object, limit: int) -> str:
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > limit:
        raise ReviewRejected('The filter was not valid.')
    return value.strip()


def query_groups(
    result: ReviewResult,
    *,
    q: str = '',
    version_state: str = '',
    marker: str = '',
    copy_hint: str = 'any',
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    preview: int = GROUP_FILE_PREVIEW,
) -> dict[str, object]:
    if copy_hint not in {'any', 'yes', 'no'}:
        raise ReviewRejected('The filter was not valid.')
    folded = fold(q) if q else ''
    marker_key = marker.casefold()
    matched = []
    for group in result.groups.values():
        if folded and folded not in group.haystack:
            continue
        if version_state and group.version_state != version_state:
            continue
        if marker_key and marker_key not in group.markers:
            continue
        if copy_hint == 'yes' and not group.any_copy_hint:
            continue
        if copy_hint == 'no' and group.any_copy_hint:
            continue
        matched.append(group)
    page_count = max(1, math.ceil(len(matched) / page_size)) if matched else 1
    page = min(max(1, page), page_count)
    start = (page - 1) * page_size
    view = matched[start:start + page_size]
    total = len(result.groups)
    return {
        'groups': [_public_group(group, preview) for group in view],
        'page': page,
        'page_size': page_size,
        'page_count': page_count,
        'matched_groups': len(matched),
        'total_groups': total,
        'empty_library_message': (
            'No filename candidate groups in this snapshot. '
            f'{result.audio_paths} audio paths were accepted as text, and none shared both a normalized '
            'core and a version signature with another path. Clean, dirty, radio, extended, remix, and '
            'edit variants stay apart on purpose. This is not a finding that the audio is unique, and it '
            'is not a check that the files exist now.'
        ),
        'filter_empty_message': (
            f'No candidate groups match this filter. There are still {total} groups before the filter. '
            'A missing title often means it did not share a core and version signature with a second path. '
            'It does not mean the audio was checked.'
        ),
    }


def review_payload(result: ReviewResult, **filters: object) -> dict[str, object]:
    page = query_groups(
        result,
        q=_as_text(filters.get('q', ''), 200),
        version_state=_as_text(filters.get('version_state', ''), 80),
        marker=_as_text(filters.get('marker', ''), 80),
        copy_hint=_as_text(filters.get('copy_hint', 'any'), 16) or 'any',
        page=_as_int(filters.get('page'), 1, 1, 100000),
        page_size=_as_int(filters.get('page_size'), DEFAULT_PAGE_SIZE, 1, 50),
    )
    return {
        'accepted': True,
        'status': result.status,
        'status_label': _status_label(result),
        'example_data': result.example_data,
        'form': {
            'dataset_label': result.dataset_label,
            'scope': result.scope,
            'snapshot_date': result.snapshot_date,
            'path_column': result.path_column,
            'source_name': result.source_name,
        },
        'provenance_rows': _provenance_rows(result),
        'counts_display': _counts_display(result),
        'counts_note': (
            'Accepted means the row was inside the scope, had status ok, and used an audio extension. '
            'It does not mean the file was found on disk. Byte evidence and audio evidence were not checked. '
            'Current files were not verified. Nothing here authorizes deletion.'
        ),
        'assurances': _assurances(),
        'errors': list(result.errors),
        'error_count': result.error_count,
        'error_details_truncated': result.error_details_truncated,
        'facets': _facets(result),
        **page,
    }


def query_payload(result: ReviewResult, body: dict) -> dict[str, object]:
    return query_groups(
        result,
        q=_as_text(body.get('q', ''), 200),
        version_state=_as_text(body.get('version_state', ''), 80),
        marker=_as_text(body.get('marker', ''), 80),
        copy_hint=_as_text(body.get('copy_hint', 'any'), 16) or 'any',
        page=_as_int(body.get('page'), 1, 1, 100000),
        page_size=_as_int(body.get('page_size'), DEFAULT_PAGE_SIZE, 1, 50),
    )


def group_payload(result: ReviewResult, body: dict) -> dict[str, object]:
    group_id = _as_text(body.get('group_id', ''), 64)
    group = result.groups.get(group_id)
    if group is None:
        raise ReviewRejected('That candidate group is not in the current review.', status=404)
    offset = _as_int(body.get('offset'), 0, 0, 1000000)
    limit = _as_int(body.get('limit'), GROUP_FILE_PREVIEW, 1, 100)
    chosen = group.files[offset:offset + limit]
    return {
        'group_id': group.group_id,
        'file_count': len(group.files),
        'offset': offset,
        'files': [_public_file(item) for item in chosen],
    }


def _summary_dict(result: ReviewResult) -> dict[str, object]:
    return {
        'evidence': 'filename_candidates',
        'evidence_label': EVIDENCE_LABEL,
        'byte_evidence': 'not_checked',
        'audio_evidence': 'not_checked',
        'current_files_verified': False,
        'deletion_authorized': False,
        'audio_metadata_read': False,
        'audio_content_read': False,
        'live_directory_scan': False,
        'hosted_upload': False,
        'imported_paths_opened': False,
        'action_allowed': 'review_only',
        'paths_are_untrusted_text': True,
        'engine_name': dj.ENGINE_NAME,
        'engine_version': dj.ENGINE_VERSION,
        'app_name': APP_NAME,
        'app_version': APP_VERSION,
        'dataset_label': result.dataset_label,
        'source_name': result.source_name,
        'scope': result.scope,
        'snapshot_date': result.snapshot_date,
        'snapshot_date_provenance': dj.SNAPSHOT_DATE_PROVENANCE,
        'snapshot_age_days': result.snapshot_age_days,
        'snapshot_age_note': result.age_note,
        'analysis_generated_at': result.analysis_generated_at,
        'analysis_timezone': dj.ANALYSIS_TIMEZONE,
        'example_data': result.example_data,
        'status': result.status,
        'status_label': _status_label(result),
        'input_rows': result.input_rows,
        'audio_paths': result.audio_paths,
        'candidate_groups': len(result.groups),
        'candidate_files': sum(len(group.files) for group in result.groups.values()),
        'repeated_path_rows': result.repeated_path_rows,
        'out_of_scope': result.out_of_scope,
        'unsupported_rows': result.unsupported_rows,
        'error_count': result.error_count,
        'error_details_truncated': result.error_details_truncated,
        'mode': 'saved_filename_inventory',
    }


_CANDIDATE_FIELDS = (
    'evidence', 'evidence_label', 'action_allowed', 'byte_evidence', 'audio_evidence',
    'current_files_verified', 'deletion_authorized', 'group_id', 'dataset_label', 'source_name',
    'scope', 'snapshot_date', 'snapshot_date_provenance', 'analysis_generated_at', 'analysis_timezone',
    'engine_name', 'engine_version', 'app_version', 'example_data', 'version_state', 'markers',
    'qualifiers', 'signature', 'copy_hint', 'core', 'original_filename', 'folder_text', 'path',
    'uncertainty',
)


def _csv_bytes(fieldnames: tuple[str, ...], rows: list[dict[str, object]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator='\r\n')
    writer.writeheader()
    for row in rows:
        writer.writerow({key: dj.csv_cell(row.get(key, '')) for key in fieldnames})
    return buffer.getvalue().encode('utf-8-sig')


def _candidate_rows(result: ReviewResult) -> list[dict[str, object]]:
    rows = []
    for group in result.groups.values():
        for item in group.files:
            rows.append({
                'evidence': EVIDENCE_CODE,
                'evidence_label': EVIDENCE_LABEL,
                'action_allowed': ACTION_ALLOWED,
                'byte_evidence': 'not_checked',
                'audio_evidence': 'not_checked',
                'current_files_verified': 'false',
                'deletion_authorized': 'false',
                'group_id': group.group_id,
                'dataset_label': result.dataset_label,
                'source_name': result.source_name,
                'scope': result.scope,
                'snapshot_date': result.snapshot_date,
                'snapshot_date_provenance': dj.SNAPSHOT_DATE_PROVENANCE,
                'analysis_generated_at': result.analysis_generated_at,
                'analysis_timezone': dj.ANALYSIS_TIMEZONE,
                'engine_name': dj.ENGINE_NAME,
                'engine_version': dj.ENGINE_VERSION,
                'app_version': APP_VERSION,
                'example_data': 'yes' if result.example_data else 'no',
                'version_state': group.version_state,
                'markers': ' | '.join(group.markers),
                'qualifiers': ' | '.join(group.qualifiers),
                'signature': group.signature,
                'copy_hint': 'yes' if item.copy_hint else 'no',
                'core': item.core,
                'original_filename': item.filename,
                'folder_text': item.folder_text,
                'path': item.path,
                'uncertainty': group.uncertainty,
            })
    return rows


def _problem_rows(result: ReviewResult) -> list[dict[str, object]]:
    rows = []
    for error in result.errors:
        rows.append({
            'line': '' if 'line' not in error else error['line'],
            'message': error.get('message', ''),
            'path': error.get('path', ''),
        })
    if result.error_details_truncated:
        rows.append({
            'line': '',
            'message': 'Further row problems were counted but not listed.',
            'path': '',
        })
    return rows


def _readme_first(result: ReviewResult) -> str:
    summary = _summary_dict(result)
    return '\n'.join([
        'DJ LIBRARY FILENAME CANDIDATE REVIEW',
        'THIS IS NOT PROOF OF DUPLICATE AUDIO.',
        '',
        'Byte evidence: not checked.',
        'Audio evidence: not checked.',
        'Current files verified: no. The snapshot does not show that a path exists now.',
        'Deletion authorized: no. A matching filename does not authorize delete, move, rename, or retag.',
        'Imported paths were not opened, stat-ed, or resolved.',
        'This report was created because a review export was requested. It does not change music files.',
        'A leading apostrophe in the CSV is a spreadsheet guard, not part of the filename.',
        'Open filename_candidates.csv or row_problems.csv in a spreadsheet.',
        'Do not open errors.json or paths.json in a spreadsheet; they keep exact untrusted text.',
        '',
        'Dataset: ' + dj.csv_cell(result.dataset_label),
        'Source file: ' + dj.csv_cell(result.source_name),
        'Scope: ' + dj.csv_cell(result.scope),
        'Snapshot date: ' + result.snapshot_date,
        'Snapshot date source: ' + dj.SNAPSHOT_DATE_PROVENANCE,
        'Analysis: ' + result.analysis_generated_at + ' ' + dj.ANALYSIS_TIMEZONE,
        'Engine: ' + dj.ENGINE_NAME + ' ' + dj.ENGINE_VERSION,
        'App: ' + APP_NAME + ' ' + APP_VERSION,
        'Example data: ' + ('yes' if result.example_data else 'no'),
        'Status: ' + result.status,
        '',
        json.dumps(summary, ensure_ascii=False, indent=2),
        '',
    ])


def _review_notes(result: ReviewResult) -> str:
    lines = [
        'FILENAME CANDIDATE NOTES',
        'Not confirmed audio duplicates. Bytes not checked. Audio not checked. Current files not verified.',
        'A leading apostrophe is a spreadsheet guard, not part of the filename.',
        '',
        _status_label(result),
        '',
    ]
    if not result.groups:
        lines.append('No filename candidate groups.')
        lines.append('')
    for group in result.groups.values():
        lines.append(f'GROUP {group.group_id}')
        lines.append('Evidence: ' + EVIDENCE_LABEL)
        lines.append('Normalized core: ' + dj.csv_cell(group.core))
        lines.append('Markers: ' + dj.csv_cell(' | '.join(group.markers) or '(none)'))
        lines.append('Qualifiers: ' + dj.csv_cell(' | '.join(group.qualifiers) or '(none)'))
        lines.append('Version: ' + group.version_state_label)
        lines.append(group.comparison_note)
        lines.append(group.uncertainty)
        for item in group.files:
            lines.append(
                '- '
                + dj.csv_cell(item.filename)
                + ' | '
                + dj.csv_cell(item.path)
                + ' | copy_hint='
                + ('yes' if item.copy_hint else 'no')
            )
        lines.append('')
    if result.errors:
        lines.append('ROW PROBLEMS')
        for error in result.errors:
            prefix = f"Line {error['line']}: " if 'line' in error else ''
            path = f" | {dj.csv_cell(error['path'])}" if error.get('path') else ''
            lines.append(prefix + str(error.get('message', '')) + path)
        lines.append('')
    return '\n'.join(lines) + '\n'


def export_report_zip(result: ReviewResult) -> bytes:
    """Build a review report in memory. Does not write music paths or choose an output folder."""
    summary = _summary_dict(result)
    exact_groups = [
        {'group_id': group.group_id, 'paths': [item.path for item in group.files]}
        for group in result.groups.values()
    ]
    exact_errors = []
    for error in result.errors:
        item = {'message': error.get('message', '')}
        if 'line' in error:
            item['line'] = error['line']
        if 'path' in error:
            item['path'] = error['path']
        exact_errors.append(item)
    files = {
        'README_FIRST.txt': _readme_first(result).encode('utf-8'),
        'filename_candidates.csv': _csv_bytes(_CANDIDATE_FIELDS, _candidate_rows(result)),
        'row_problems.csv': _csv_bytes(('line', 'message', 'path'), _problem_rows(result)),
        'review_notes.txt': _review_notes(result).encode('utf-8'),
        'summary.json': (json.dumps(summary, ensure_ascii=False, indent=2) + '\n').encode('utf-8'),
        'errors.json': (json.dumps({
            'untrusted_text': True,
            'spreadsheet_safe': False,
            'note': 'Path text retained for this review. Cells longer than 2000 characters are shortened. Do not open this file in a spreadsheet. Use row_problems.csv there.',
            'truncated': result.error_details_truncated,
            'errors': exact_errors,
        }, ensure_ascii=False, indent=2) + '\n').encode('utf-8'),
        'paths.json': (json.dumps({
            'untrusted_text': True,
            'spreadsheet_safe': False,
            'do_not_open_in_a_spreadsheet': True,
            'imported_paths_opened': False,
            'note': 'Exact path text from the inventory. These paths were not opened.',
            'groups': exact_groups,
        }, ensure_ascii=False, indent=2) + '\n').encode('utf-8'),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(filename=name)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[name])
    return buffer.getvalue()
