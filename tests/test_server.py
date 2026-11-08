"""Loopback desk: import, review, export, and reject cross-origin writes."""

from __future__ import annotations

import io
import json
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import dj_find_dupes_v2 as dj

from djlibreview import server as server_module
from djlibreview.review import EXAMPLE_SCOPE
from tests.support import LocalDesk, multipart

ROOT = Path(__file__).resolve().parents[1]


class DeskHttp(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = LocalDesk()
        self.addCleanup(self.desk.close)

    def test_page_is_local_and_states_its_limits(self) -> None:
        page = self.desk.open_page()
        self.assertEqual(page.status, 200)
        text = page.text()
        self.assertIn(self.desk.origin, text)
        self.assertNotIn('{{', text)
        self.assertIn('not confirmed audio duplicates', text)
        self.assertIn('Byte evidence: not checked', text)
        self.assertIn('Audio evidence: not checked', text)
        self.assertIn('not verified', text)
        self.assertIn('does not authorize deletion', text)
        self.assertIsNone(page.header('Access-Control-Allow-Origin'))
        self.assertIn("default-src 'none'", page.header('Content-Security-Policy') or '')
        cookie = page.header('Set-Cookie') or ''
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertNotIn('Secure', cookie)
        health = self.desk.request('GET', '/api/health')
        body = health.json()
        self.assertFalse(body['live_directory_scan'])
        self.assertFalse(body['byte_hash'])
        self.assertFalse(body['audio_read'])
        self.assertFalse(body['hosted_upload'])
        self.assertTrue(body['receives_csv_on_loopback'])
        self.assertEqual(body['bind'], '127.0.0.1')
        self.assertEqual(self.desk.httpd.server_address[0], '127.0.0.1')

    def test_tokens_differ_and_reuse_requires_the_page(self) -> None:
        first = self.desk.request('GET', '/', include_cookie=False).text()
        second = self.desk.request('GET', '/', include_cookie=False).text()
        token_a = first.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
        token_b = second.split('name="csrf-token" content="', 1)[1].split('"', 1)[0]
        self.assertNotEqual(token_a, token_b)
        self.desk.open_page()
        again = self.desk.request('GET', '/')
        self.assertIn(self.desk.csrf, again.text())

    def test_rejects_bad_host_origin_and_missing_csrf(self) -> None:
        denied = self.desk.request('GET', '/api/health', headers={'Host': 'evil.example'})
        self.assertEqual(denied.status, 403)
        self.assertIsNone(denied.header('Access-Control-Allow-Origin'))
        self.assertNotIn('<html', denied.text().lower())
        self.desk.open_page()
        cross = self.desk.request(
            'POST', '/api/demo', body=b'{}', include_csrf=True,
            headers={'Origin': 'http://evil.example', 'Content-Type': 'application/json'},
        )
        self.assertEqual(cross.status, 403)
        missing = self.desk.request(
            'POST', '/api/demo', body=b'{}', include_csrf=False,
            headers={'Origin': self.desk.origin, 'Content-Type': 'application/json'},
        )
        self.assertEqual(missing.status, 403)
        options = self.desk.request('OPTIONS', '/api/review', headers={'Origin': 'http://evil.example'})
        self.assertEqual(options.status, 403)
        self.assertIsNone(options.header('Access-Control-Allow-Origin'))

    def test_example_review_search_and_export(self) -> None:
        self.desk.open_page()
        with patch.object(dj, 'enumerate_local', side_effect=AssertionError('scan')), \
                patch.object(dj, 'hash_selected', side_effect=AssertionError('hash')), \
                patch.object(dj, 'safe_local_path', side_effect=AssertionError('live path')), \
                patch('os.walk', side_effect=AssertionError('walk')):
            loaded = self.desk.request(
                'POST', '/api/demo', body=b'{"scope":"/etc"}', include_csrf=True,
                headers={'Content-Type': 'application/json'},
            )
        self.assertEqual(loaded.status, 200)
        data = loaded.json()
        self.assertTrue(data['example_data'])
        self.assertEqual(data['form']['scope'], EXAMPLE_SCOPE)
        self.assertEqual(data['form']['snapshot_date'], '2026-03-01')
        self.assertNotEqual(data['form']['scope'], '/etc')
        self.assertEqual(data['total_groups'], 4)
        self.assertEqual(data['status'], 'complete_filename_review')
        self.assertTrue(all(item['value'] in {'Not checked', 'Not verified', 'Not authorized'} for item in data['assurances']))
        dirty = self.desk.request(
            'POST', '/api/query', body=json.dumps({'q': 'Dirty'}).encode(), include_csrf=True,
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(dirty.json()['matched_groups'], 0)
        self.assertEqual(dirty.json()['total_groups'], 4)
        found = self.desk.request(
            'POST', '/api/query', body=json.dumps({'q': 'Beyoncé'}).encode(), include_csrf=True,
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(found.json()['matched_groups'], 1)
        intro = self.desk.request(
            'POST', '/api/query', body=json.dumps({'marker': 'intro'}).encode(), include_csrf=True,
            headers={'Content-Type': 'application/json'},
        ).json()
        self.assertEqual(intro['matched_groups'], 1)
        names = ''.join(item['filename'] for item in intro['groups'][0]['files'])
        self.assertIn('[Intro Clean]', names)
        exported = self.desk.request(
            'POST', '/api/export', body=b'{}', include_csrf=True,
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(exported.status, 200)
        self.assertIn('filename-candidate-review.zip', exported.header('Content-Disposition') or '')
        archive = zipfile.ZipFile(io.BytesIO(exported.body))
        summary = json.loads(archive.read('summary.json'))
        self.assertEqual(summary['byte_evidence'], 'not_checked')
        self.assertTrue(summary['example_data'])
        self.assertIn('Nova - Midnight (Clean Intro).mp3', archive.read('filename_candidates.csv').decode('utf-8-sig'))
        home = self.desk.request('GET', '/')
        self.assertNotIn('Nova - Midnight', home.text())
        self.assertNotIn('<script>alert', home.text())

    def test_upload_reports_row_problems_without_reflecting_into_html(self) -> None:
        self.desk.open_page()
        payload = (ROOT / 'djlibreview/examples/row_problems.csv').read_bytes()
        content_type, body = multipart(
            {
                'dataset_label': '<script>alert(1)</script>',
                'scope': EXAMPLE_SCOPE,
                'snapshot_date': '2026-04-02',
                'path_column': '',
            },
            {'inventory': ('row_problems.csv', payload)},
        )
        with patch.object(dj, 'safe_local_path', side_effect=AssertionError('opened path')):
            uploaded = self.desk.request(
                'POST', '/api/review', body=body, include_csrf=True,
                headers={'Content-Type': content_type},
            )
        self.assertEqual(uploaded.status, 200)
        data = uploaded.json()
        self.assertEqual(data['status'], 'partial_review_errors')
        self.assertEqual(data['error_count'], 6)
        self.assertEqual(data['total_groups'], 3)
        self.assertFalse(data['example_data'])
        encoded = json.dumps(data)
        self.assertIn('<script>alert(1)</script>', encoded)
        page = self.desk.request('GET', '/')
        self.assertNotIn('<script>alert(1)</script>', page.text())
        self.assertNotIn('=1+1.mp3', page.text())

    def test_bad_headers_replace_review_and_validation_does_not(self) -> None:
        self.desk.open_page()
        self.desk.request('POST', '/api/demo', body=b'{}', include_csrf=True, headers={'Content-Type': 'application/json'})
        content_type, body = multipart(
            {'dataset_label': 'Nope', 'scope': 'relative', 'snapshot_date': '2026-04-02', 'path_column': ''},
            {'inventory': ('x.csv', b'path\n/synthetic/dj-library/a/A.mp3\n')},
        )
        rejected = self.desk.request(
            'POST', '/api/review', body=body, include_csrf=True, headers={'Content-Type': content_type},
        )
        self.assertEqual(rejected.status, 400)
        still = self.desk.request('GET', '/api/review').json()
        self.assertTrue(still['example_data'])
        self.assertEqual(still['total_groups'], 4)
        fatal_type, fatal_body = multipart(
            {'dataset_label': 'Bad headers', 'scope': EXAMPLE_SCOPE, 'snapshot_date': '2026-04-02', 'path_column': ''},
            {'inventory': ('bad.csv', (ROOT / 'djlibreview/examples/bad_headers.csv').read_bytes())},
        )
        fatal = self.desk.request(
            'POST', '/api/review', body=fatal_body, include_csrf=True, headers={'Content-Type': fatal_type},
        )
        self.assertEqual(fatal.status, 200)
        self.assertEqual(fatal.json()['status'], 'fatal_error')
        self.assertEqual(fatal.json()['total_groups'], 0)
        self.assertIn('ambiguous or missing', fatal.json()['errors'][-1]['message'])

    def test_oversized_upload_is_rejected(self) -> None:
        self.desk.open_page()
        with patch.object(server_module, 'MAX_UPLOAD_BYTES', 128):
            response = self.desk.request(
                'POST', '/api/review', body=None, include_csrf=True,
                headers={
                    'Content-Type': 'multipart/form-data; boundary=x',
                    'Content-Length': '256',
                },
            )
        self.assertEqual(response.status, 413)
        self.assertIn('local limit', response.json()['error'])

    def test_routes_are_saved_inventory_only(self) -> None:
        self.assertEqual(server_module.GET_ROUTES, frozenset({
            '/', '/app.css', '/app.js', '/favicon.svg', '/api/review', '/api/health',
        }))
        self.assertEqual(server_module.POST_ROUTES, frozenset({
            '/api/review', '/api/demo', '/api/query', '/api/group', '/api/export',
        }))
        for route in server_module.GET_ROUTES | server_module.POST_ROUTES:
            lowered = route.lower()
            self.assertNotIn('hash', lowered)
            self.assertNotIn('directory', lowered)
            self.assertNotIn('delete', lowered)
        self.assertEqual(server_module.main(['--host', '0.0.0.0', '--port', '9']), 2)

    def test_pagination_and_group_text(self) -> None:
        self.desk.open_page()
        rows = ['path']
        for index in range(15):
            rows.append(f'{EXAMPLE_SCOPE}/left/{index}/Song {index} (Clean).mp3')
            rows.append(f'{EXAMPLE_SCOPE}/right/{index}/Song {index} (Clean).mp3')
        content_type, body = multipart(
            {
                'dataset_label': 'Pages',
                'scope': EXAMPLE_SCOPE,
                'snapshot_date': '2026-05-01',
                'path_column': '',
            },
            {'inventory': ('pages.csv', ('\n'.join(rows) + '\n').encode())},
        )
        uploaded = self.desk.request(
            'POST', '/api/review', body=body, include_csrf=True, headers={'Content-Type': content_type},
        )
        first = uploaded.json()
        self.assertEqual(first['total_groups'], 15)
        self.assertEqual(first['page'], 1)
        self.assertGreater(first['page_count'], 1)
        second = self.desk.request(
            'POST', '/api/query',
            body=json.dumps({'page': 2, 'page_size': first['page_size']}).encode(),
            include_csrf=True,
            headers={'Content-Type': 'application/json'},
        ).json()
        self.assertEqual(second['page'], 2)
        self.assertNotEqual(first['groups'][0]['group_id'], second['groups'][0]['group_id'])
        detail = self.desk.request(
            'POST', '/api/group',
            body=json.dumps({'group_id': first['groups'][0]['group_id'], 'offset': 0, 'limit': 20}).encode(),
            include_csrf=True,
            headers={'Content-Type': 'application/json'},
        ).json()
        self.assertEqual(detail['file_count'], 2)
        self.assertEqual(len(detail['files']), 2)


if __name__ == '__main__':
    unittest.main()
