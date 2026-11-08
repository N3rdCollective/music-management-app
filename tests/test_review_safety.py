"""Safety contracts: saved text in, no music paths opened, no deletion authority."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import dj_find_dupes_v2 as dj

from djlibreview.review import (
    MAX_UPLOAD_BYTES,
    ReviewRejected,
    analyze_csv_bytes,
    export_report_zip,
    review_payload,
)

SCOPE = '/synthetic/dj-library'


def inventory(rows: list[str], header: str = 'path') -> bytes:
    lines = [header, *rows]
    return ('\n'.join(lines) + '\n').encode('utf-8')


def review(data: bytes, **kwargs):
    params = {
        'dataset_label': 'Synthetic',
        'source_name': 'memory.csv',
        'scope': SCOPE,
        'snapshot_date': '2026-03-01',
        'path_column': None,
    }
    params.update(kwargs)
    return analyze_csv_bytes(data, **params)


class AnalyzeSafety(unittest.TestCase):
    def test_imported_paths_are_not_opened(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            target = folder / 'A - T (Clean).mp3'
            target.write_bytes(b'fixture-do-not-read')
            before = target.read_bytes()
            other = str(folder / 'b' / 'A - T (Clean).mp3')
            text = 'path\n' + str(target) + '\n' + other + '\n'
            datetime.now(ZoneInfo('America/New_York'))
            with patch('builtins.open', side_effect=AssertionError('open')), \
                    patch('os.walk', side_effect=AssertionError('walk')), \
                    patch('os.listdir', side_effect=AssertionError('listdir')), \
                    patch.object(dj, 'safe_local_path', side_effect=AssertionError('stat path')), \
                    patch.object(dj, 'enumerate_local', side_effect=AssertionError('scan')), \
                    patch.object(dj, 'hash_selected', side_effect=AssertionError('hash')), \
                    patch.object(dj, 'write_reports', side_effect=AssertionError('write report')):
                result = review(text.encode(), scope=str(folder), dataset_label='Temp text')
            self.assertEqual(target.read_bytes(), before)
        self.assertEqual(result.audio_paths, 2)
        self.assertEqual(len(result.groups), 1)
        payload = review_payload(result)
        self.assertEqual(payload['assurances'][0]['value'], 'Not checked')
        self.assertEqual(payload['assurances'][1]['value'], 'Not checked')
        self.assertEqual(payload['assurances'][2]['value'], 'Not verified')
        self.assertEqual(payload['assurances'][3]['value'], 'Not authorized')
        self.assertIn('does not mean the file was found on disk', payload['counts_note'])

    def test_pair_groups_without_touching_bytes(self) -> None:
        data = inventory([
            f'{SCOPE}/a/A - T (Clean).mp3',
            f'{SCOPE}/b/A - T (Clean).mp3',
            f'{SCOPE}/c/A - T (Dirty).mp3',
        ])
        with patch('socket.socket', side_effect=AssertionError('network')), \
                patch.object(dj, 'enumerate_local', side_effect=AssertionError('scan')), \
                patch.object(dj, 'hash_selected', side_effect=AssertionError('hash')):
            result = review(data)
        self.assertEqual(len(result.groups), 1)
        grouped = [item.path for group in result.groups.values() for item in group.files]
        self.assertFalse(any('Dirty' in path for path in grouped))
        archive = zipfile.ZipFile(io.BytesIO(export_report_zip(result)))
        summary = json.loads(archive.read('summary.json'))
        self.assertEqual(summary['byte_evidence'], 'not_checked')
        self.assertEqual(summary['audio_evidence'], 'not_checked')
        self.assertFalse(summary['current_files_verified'])
        self.assertFalse(summary['deletion_authorized'])
        self.assertFalse(summary['audio_content_read'])
        self.assertFalse(summary['imported_paths_opened'])
        names = set(archive.namelist())
        self.assertEqual(names, {
            'README_FIRST.txt', 'filename_candidates.csv', 'row_problems.csv',
            'review_notes.txt', 'summary.json', 'errors.json', 'paths.json',
        })
        for info in archive.infolist():
            self.assertNotIn('..', info.filename)
            self.assertFalse(info.filename.startswith('/'))

    def test_formula_and_html_text_is_export_safe(self) -> None:
        label = '=HYPERLINK("http://evil.test","click")'
        data = inventory([
            f'{SCOPE}/a/+plus (Clean).mp3',
            f'{SCOPE}/b/+plus (Clean).mp3',
            f'{SCOPE}/a/<script>alert(1)</script> (Clean).mp3',
            f'{SCOPE}/b/<script>alert(1)</script> (Clean).mp3',
        ])
        result = review(data, dataset_label=label, source_name='=bad.csv')
        archive = zipfile.ZipFile(io.BytesIO(export_report_zip(result)))
        text = archive.read('filename_candidates.csv').decode('utf-8-sig')
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertTrue(rows)
        self.assertTrue(all(row['dataset_label'].startswith("'=") for row in rows))
        self.assertTrue(all(row['byte_evidence'] == 'not_checked' for row in rows))
        self.assertTrue(all(row['deletion_authorized'] == 'false' for row in rows))
        plus = next(row for row in rows if 'plus' in row['original_filename'])
        self.assertTrue(plus['original_filename'].startswith("'"))
        self.assertTrue(plus['core'].startswith("'"))
        script_rows = [row for row in rows if 'script' in row['path']]
        self.assertEqual(len(script_rows), 2)
        exact = json.loads(archive.read('paths.json'))
        flat = [path for group in exact['groups'] for path in group['paths']]
        self.assertTrue(any(path.endswith('<script>alert(1)</script> (Clean).mp3') for path in flat))
        self.assertNotIn("'<script>", ''.join(flat))
        readme = archive.read('README_FIRST.txt').decode('utf-8').lower()
        self.assertIn('do not open', readme)
        self.assertIn('spreadsheet', readme)

    def test_malformed_unicode_and_column_selection(self) -> None:
        bom = '\ufeffpath\n' + f'{SCOPE}/a/東京 (Clean).mp3\n' + f'{SCOPE}/b/東京 (Clean).mp3\n'
        result = review(bom.encode('utf-8'))
        self.assertEqual(len(result.groups), 1)
        self.assertTrue(any('東京' in item.filename for group in result.groups.values() for item in group.files))
        custom = 'custom\n' + f'{SCOPE}/a/A - T (Clean).mp3\n' + f'{SCOPE}/b/A - T (Clean).mp3\n'
        chosen = review(custom.encode(), path_column='custom')
        self.assertEqual(len(chosen.groups), 1)
        broken = review(b'path\n\xff\n')
        self.assertEqual(broken.status, 'fatal_error')
        self.assertIn('UTF-8', broken.errors[-1]['message'])
        empty = review(b'')
        self.assertEqual(empty.status, 'fatal_error')
        self.assertIn('empty', empty.errors[-1]['message'])
        headers = review('path,full_path\n'.encode())
        self.assertEqual(headers.status, 'fatal_error')
        self.assertIn('ambiguous or missing', headers.errors[-1]['message'])
        duplicate = review('path,path\n'.encode())
        self.assertEqual(duplicate.status, 'fatal_error')
        self.assertIn('repeats a column', duplicate.errors[-1]['message'])
        newline = 'path\n"/synthetic/dj-library/a/line\nbreak.mp3"\n'.encode()
        skipped = review(newline)
        self.assertEqual(skipped.audio_paths, 0)
        self.assertEqual(skipped.error_count, 1)
        nul = f'path\n{SCOPE}/a/bad\x00.mp3\n'.encode()
        nulled = review(nul)
        self.assertEqual(nulled.audio_paths, 0)
        self.assertEqual(nulled.errors[0]['message'].startswith('This row is empty'), True)
        problems = (Path(__file__).resolve().parents[1] / 'djlibreview/examples/row_problems.csv').read_bytes()
        messy = review(problems)
        self.assertEqual(messy.status, 'partial_review_errors')
        self.assertEqual(messy.error_count, 6)
        self.assertEqual(messy.out_of_scope, 4)
        self.assertEqual(len(messy.groups), 3)
        self.assertTrue(any('=1+1.mp3' == error.get('path') for error in messy.errors))
        archived = zipfile.ZipFile(io.BytesIO(export_report_zip(messy)))
        problem_csv = archived.read('row_problems.csv').decode('utf-8-sig')
        self.assertIn("'=1+1.mp3", problem_csv)
        exact_errors = json.loads(archived.read('errors.json'))
        self.assertFalse(exact_errors['spreadsheet_safe'])
        self.assertTrue(any(error.get('path') == '=1+1.mp3' for error in exact_errors['errors']))

    def test_scope_and_snapshot_are_required(self) -> None:
        data = inventory([f'{SCOPE}/a/A - T.mp3'])
        with self.assertRaises(ReviewRejected):
            review(data, scope='/')
        with self.assertRaises(ReviewRejected):
            review(data, scope='relative/music')
        with self.assertRaises(ReviewRejected):
            review(data, scope=f'{SCOPE}/../etc')
        with self.assertRaises(ReviewRejected):
            review(data, snapshot_date='')
        with self.assertRaises(ReviewRejected):
            review(data, snapshot_date='today')
        with self.assertRaises(ReviewRejected):
            review(data, snapshot_date='2026-02-31')
        with self.assertRaises(ReviewRejected):
            review(data, dataset_label='   ')
        self.assertGreaterEqual(MAX_UPLOAD_BYTES, 32 * 1024 * 1024)
        self.assertLessEqual(MAX_UPLOAD_BYTES, 64 * 1024 * 1024)

    def test_row_budget_stops_without_partial_groups(self) -> None:
        rows = [f'{SCOPE}/a/{index}.mp3' for index in range(5)]
        with patch.object(dj, 'MAX_INVENTORY_ROWS', 3):
            result = review(inventory(rows))
        self.assertEqual(result.status, 'fatal_error')
        self.assertEqual(result.audio_paths, 0)
        self.assertEqual(len(result.groups), 0)
        self.assertIn('row budget exceeded', result.errors[-1]['message'])

    def test_preview_does_not_drop_export_rows(self) -> None:
        rows = [f'{SCOPE}/copy-{index}/A - T (Clean).mp3' for index in range(10)]
        result = review(inventory(rows))
        self.assertEqual(len(result.groups), 1)
        payload = review_payload(result)
        self.assertEqual(payload['groups'][0]['file_count'], 10)
        self.assertEqual(len(payload['groups'][0]['files']), 8)
        archived = zipfile.ZipFile(io.BytesIO(export_report_zip(result)))
        table = list(csv.DictReader(io.StringIO(archived.read('filename_candidates.csv').decode('utf-8-sig'))))
        self.assertEqual(len(table), 10)

    def test_app_sources_do_not_call_live_helpers(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for relative in ('djlibreview/review.py', 'djlibreview/server.py', 'djlibreview/static/app.js', 'djlibreview/static/index.html'):
            text = (root / relative).read_text(encoding='utf-8')
            for banned in ('enumerate_local', 'hash_selected', 'safe_local_path', 'os.walk', 'file://', 'innerHTML', 'insertAdjacentHTML'):
                self.assertNotIn(banned, text, relative)
        for relative in ('djlibreview/review.py', 'djlibreview/server.py'):
            text = (root / relative).read_text(encoding='utf-8')
            self.assertNotIn('import socket', text)
            self.assertNotIn('urllib.request', text)
            self.assertNotIn('hashlib', text)
        script = (root / 'djlibreview/static/app.js').read_text(encoding='utf-8')
        self.assertNotIn('document.write', script)
        self.assertNotIn('eval(', script)
        page = (root / 'djlibreview/static/index.html').read_text(encoding='utf-8')
        self.assertIn('not confirmed audio duplicates', page)
        self.assertIn('Byte evidence: not checked', page)
        self.assertIn('does not authorize deletion', page)
        stylesheet = (root / 'djlibreview/static/app.css').read_text(encoding='utf-8')
        self.assertNotIn('http://', stylesheet)
        self.assertNotIn('https://', stylesheet)
        self.assertNotIn('@import', stylesheet)


class EngineCliGuards(unittest.TestCase):
    def test_missing_mount_table_stays_unsupported(self) -> None:
        real_exists = Path.exists

        def fake_exists(self):
            if str(self) == '/proc/self/mountinfo':
                return False
            return real_exists(self)

        with patch.object(Path, 'exists', fake_exists):
            with self.assertRaisesRegex(ValueError, 'use saved CSV'):
                dj.safe_local_path('/tmp/djlib-scope-not-home/a.mp3', '/tmp/djlib-scope-not-home')

    def test_inventory_cli_does_not_check_bytes_or_authorize_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / 'library'
            source.mkdir()
            index = base / 'index.csv'
            index.write_text('path\n' + str(source / 'A - T (Clean).mp3') + '\n', encoding='utf-8')
            output = base / 'report'
            with patch('sys.stdout', new=io.StringIO()):
                code = dj.main([
                    '--inventory', str(index), '--scope', str(source),
                    '--snapshot-date', '2026-03-01', '--output', str(output),
                ])
            summary = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
        self.assertEqual(code, 0)
        self.assertEqual(summary['byte_evidence'], 'not_checked')
        self.assertEqual(summary['audio_evidence'], 'not_checked')
        self.assertFalse(summary['deletion_authorized'])
        self.assertFalse(summary['audio_content_read'])
        self.assertEqual(summary['engine_version'], '2.1.0')

    def test_opt_in_hash_still_does_not_authorize_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / 'library'
            left = source / 'a'
            right = source / 'b'
            left.mkdir(parents=True)
            right.mkdir()
            (left / 'A - T (Clean).mp3').write_bytes(b'fixture')
            (right / 'A - T (Clean).mp3').write_bytes(b'fixture')
            groups = dj.candidate_groups([
                str(left / 'A - T (Clean).mp3'),
                str(right / 'A - T (Clean).mp3'),
            ])
            group_id = next(iter(groups))
            output = base / 'report'
            with patch('sys.stdout', new=io.StringIO()):
                dj.main([
                    '--directory', str(source), '--scope', str(source),
                    '--snapshot-date', '2026-03-01', '--output', str(output),
                    '--hash-group', group_id, '--allow-root', str(source),
                ])
            summary = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
        self.assertEqual(summary['byte_evidence'], 'sha256_opt_in')
        self.assertEqual(summary['audio_evidence'], 'not_checked')
        self.assertFalse(summary['deletion_authorized'])
        self.assertTrue(summary['audio_content_read'])
        self.assertGreater(summary['files_opened'], 0)


if __name__ == '__main__':
    unittest.main()
