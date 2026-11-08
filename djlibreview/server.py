"""Loopback HTTP desk. It accepts a CSV from the local browser and does not open imported paths."""

from __future__ import annotations

import argparse
import html
import json
import re
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import dj_find_dupes_v2 as dj

from djlibreview import APP_VERSION
from djlibreview.review import (
    MAX_UPLOAD_BYTES,
    ReviewRejected,
    analyze_csv_bytes,
    analyze_example,
    export_report_zip,
    group_payload,
    query_payload,
    review_payload,
)

COOKIE_NAME = 'djlib_session'
DEFAULT_PORT = 8741
JSON_BODY_MAX = 32 * 1024
MAX_FORM_PARTS = 8
MAX_TEXT_FIELD = 16 * 1024
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
GET_ROUTES = frozenset({'/', '/app.css', '/app.js', '/favicon.svg', '/api/review', '/api/health'})
POST_ROUTES = frozenset({'/api/review', '/api/demo', '/api/query', '/api/group', '/api/export'})
_DENY = 'The local page did not authorize this request.'

_STATIC_ROOT = Path(__file__).resolve().parent / 'static'
_INDEX_TEMPLATE = (_STATIC_ROOT / 'index.html').read_text(encoding='utf-8')
_ASSETS = {
    '/app.css': ('text/css; charset=utf-8', (_STATIC_ROOT / 'app.css').read_bytes()),
    '/app.js': ('text/javascript; charset=utf-8', (_STATIC_ROOT / 'app.js').read_bytes()),
    '/favicon.svg': ('image/svg+xml', (_STATIC_ROOT / 'favicon.svg').read_bytes()),
}


def tokens_match(expected: str, presented: str) -> bool:
    if not isinstance(presented, str) or len(presented) != len(expected):
        return False
    return secrets.compare_digest(expected, presented)


def read_cookie(header: str) -> str:
    if not header or len(header) > 4096:
        return ''
    for part in header.split(';'):
        name, sep, value = part.strip().partition('=')
        if name == COOKIE_NAME and sep:
            return value
    return ''


def parse_multipart(content_type: str, body: bytes) -> dict[str, list[dict[str, object]]]:
    match = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', content_type, re.IGNORECASE)
    if not match:
        raise ReviewRejected('The upload could not be read. Choose the CSV again.')
    boundary = (match.group(1) or match.group(2) or '').strip()
    if not boundary or len(boundary) > 200 or any(ch in boundary for ch in '\r\n'):
        raise ReviewRejected('The upload could not be read. Choose the CSV again.')
    try:
        delimiter = b'--' + boundary.encode('ascii')
    except UnicodeEncodeError:
        raise ReviewRejected('The upload could not be read. Choose the CSV again.') from None
    if delimiter not in body:
        raise ReviewRejected('The upload could not be read. Choose the CSV again.')
    fields: dict[str, list[dict[str, object]]] = {}
    count = 0
    for part in body.split(delimiter)[1:]:
        if part.startswith(b'--'):
            break
        if part.startswith(b'\r\n'):
            part = part[2:]
        elif part.startswith(b'\n'):
            part = part[1:]
        if part.endswith(b'\r\n'):
            part = part[:-2]
        elif part.endswith(b'\n'):
            part = part[:-1]
        header_blob, sep, data = part.partition(b'\r\n\r\n')
        if not sep:
            header_blob, sep, data = part.partition(b'\n\n')
        if not sep:
            raise ReviewRejected('The upload could not be read. Choose the CSV again.')
        try:
            header_text = header_blob.decode('utf-8')
        except UnicodeError:
            raise ReviewRejected('The upload could not be read. Choose the CSV again.') from None
        disposition = ''
        for line in header_text.splitlines():
            if line.lower().startswith('content-disposition:'):
                disposition = line.split(':', 1)[1].strip()
        name_match = re.search(r'name="([^"]*)"', disposition) or re.search(r'name=([^;\s]+)', disposition)
        if not name_match:
            continue
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        count += 1
        if count > MAX_FORM_PARTS:
            raise ReviewRejected('The form had too many fields.')
        fields.setdefault(name_match.group(1), []).append({
            'filename': filename_match.group(1) if filename_match else '',
            'data': data,
        })
    return fields


def parse_json_object(body: bytes) -> dict:
    if not body:
        return {}
    try:
        data = json.loads(body.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError):
        raise ReviewRejected('The request body was not valid JSON.') from None
    if not isinstance(data, dict):
        raise ReviewRejected('The request body was not valid JSON.')
    return data


def field_text(fields: dict[str, list[dict[str, object]]], name: str) -> str:
    items = fields.get(name, [])
    if not items:
        return ''
    if len(items) != 1:
        raise ReviewRejected('The form had a repeated field.')
    raw = items[0]['data']
    if not isinstance(raw, bytes):
        raise ReviewRejected('The form could not be read.')
    if len(raw) > MAX_TEXT_FIELD:
        raise ReviewRejected('A form field was too long.')
    try:
        return raw.decode('utf-8')
    except UnicodeError:
        raise ReviewRejected('A form field was not UTF-8.') from None


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler) -> None:
        super().__init__(server_address, handler)
        self.sessions: dict[str, dict[str, object]] = {}
        self.session_order: list[str] = []
        self.lock = threading.Lock()

    def new_session(self) -> tuple[str, str]:
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[sid] = {'csrf': csrf, 'review': None, 'created': time.time()}
            self.session_order.append(sid)
            while len(self.session_order) > 20:
                old = self.session_order.pop(0)
                self.sessions.pop(old, None)
        return sid, csrf

    def csrf_for(self, sid: str) -> str | None:
        with self.lock:
            session = self.sessions.get(sid)
            if session is None:
                return None
            csrf = session.get('csrf')
            return csrf if isinstance(csrf, str) else None

    def csrf_ok(self, sid: str, presented: str) -> bool:
        csrf = self.csrf_for(sid)
        return bool(csrf) and tokens_match(csrf, presented)

    def get_review(self, sid: str):
        with self.lock:
            session = self.sessions.get(sid)
            if session is None:
                return None
            return session.get('review')

    def set_review(self, sid: str, review) -> None:
        with self.lock:
            session = self.sessions.get(sid)
            if session is not None:
                session['review'] = review


class Handler(BaseHTTPRequestHandler):
    server_version = 'DJLibraryReview'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args) -> None:
        return

    def send_error(self, code, message=None, explain=None) -> None:
        body = b'Request not accepted.\n'
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def handle_one_request(self) -> None:
        try:
            self.request.settimeout(60)
        except OSError:
            pass
        super().handle_one_request()

    def do_OPTIONS(self) -> None:
        self.close_connection = True
        self._send(403, b'This desk does not answer cross-origin requests.\n', 'text/plain; charset=utf-8')

    def do_GET(self) -> None:
        self._dispatch(False)

    def do_POST(self) -> None:
        self._dispatch(True)

    def _dispatch(self, is_post: bool) -> None:
        try:
            path = urlsplit(self.path).path
            if self._allowed_origin() is None:
                self._deny()
                return
            if is_post:
                if path not in POST_ROUTES:
                    self._send(404, b'Not found\n', 'text/plain; charset=utf-8')
                    return
                sid = self._authorized_sid()
                if sid is None:
                    self._deny()
                    return
                self._post(path, sid)
                return
            if path not in GET_ROUTES:
                self._send(404, b'Not found\n', 'text/plain; charset=utf-8')
                return
            self._get(path)
        except ReviewRejected as exc:
            self._send_json(exc.status, {'ok': False, 'error': exc.message})
        except Exception:
            traceback.print_exc()
            self._send_json(500, {
                'ok': False,
                'error': 'The review process hit an unexpected error and did not open any music path.',
            })

    def _get(self, path: str) -> None:
        if path == '/':
            sid = read_cookie(self.headers.get('Cookie', ''))
            csrf = self.server.csrf_for(sid) if sid else None
            if csrf is None:
                sid, csrf = self.server.new_session()
            self._send_html(self._page(csrf), sid)
            return
        if path == '/api/health':
            port = self.server.server_address[1]
            self._send_json(200, {
                'ok': True,
                'service': 'djlibreview',
                'version': APP_VERSION,
                'engine': dj.ENGINE_NAME,
                'engine_version': dj.ENGINE_VERSION,
                'bind': '127.0.0.1',
                'port': port,
                'saved_inventory_only': True,
                'live_directory_scan': False,
                'byte_hash': False,
                'audio_read': False,
                'hosted_upload': False,
                'receives_csv_on_loopback': True,
            })
            return
        if path == '/api/review':
            sid = read_cookie(self.headers.get('Cookie', ''))
            review = self.server.get_review(sid) if sid else None
            if review is None:
                self._send_json(200, {'accepted': False})
                return
            self._send_json(200, review_payload(review))
            return
        ctype, body = _ASSETS[path]
        self._send(200, body, ctype)

    def _post(self, path: str, sid: str) -> None:
        if path == '/api/demo':
            self._read_body(4096)
            result = analyze_example()
            self.server.set_review(sid, result)
            self._send_json(200, review_payload(result))
            return
        if path == '/api/review':
            body = self._read_body(MAX_UPLOAD_BYTES)
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type.lower():
                raise ReviewRejected('Choose a CSV inventory from this page.')
            fields = parse_multipart(content_type, body)
            files = fields.get('inventory', [])
            if len(files) != 1:
                raise ReviewRejected('Choose one CSV inventory, or load the synthetic example.')
            upload = files[0]
            data = upload['data']
            if not isinstance(data, bytes) or not data:
                raise ReviewRejected('Choose a CSV inventory, or load the synthetic example.')
            result = analyze_csv_bytes(
                data,
                dataset_label=field_text(fields, 'dataset_label'),
                source_name=str(upload.get('filename') or 'inventory.csv'),
                scope=field_text(fields, 'scope'),
                snapshot_date=field_text(fields, 'snapshot_date'),
                path_column=field_text(fields, 'path_column'),
            )
            self.server.set_review(sid, result)
            self._send_json(200, review_payload(result))
            return
        if path == '/api/query':
            review = self._require_review(sid)
            body = parse_json_object(self._read_body(JSON_BODY_MAX))
            self._send_json(200, query_payload(review, body))
            return
        if path == '/api/group':
            review = self._require_review(sid)
            body = parse_json_object(self._read_body(JSON_BODY_MAX))
            self._send_json(200, group_payload(review, body))
            return
        if path == '/api/export':
            review = self._require_review(sid)
            self._read_body(JSON_BODY_MAX)
            payload = export_report_zip(review)
            self._send(200, payload, 'application/zip', extra=(
                ('Content-Disposition', 'attachment; filename="filename-candidate-review.zip"'),
            ))
            return
        self._send(404, b'Not found\n', 'text/plain; charset=utf-8')

    def _require_review(self, sid: str):
        review = self.server.get_review(sid)
        if review is None:
            raise ReviewRejected('Import a saved inventory before continuing.', status=409)
        return review

    def _read_body(self, limit: int) -> bytes:
        if self.headers.get('Transfer-Encoding'):
            raise ReviewRejected('Send the request with a Content-Length.')
        raw_length = self.headers.get('Content-Length', '0')
        try:
            length = int(raw_length)
        except ValueError:
            raise ReviewRejected('Content-Length was not valid.') from None
        if length < 0:
            raise ReviewRejected('Content-Length was not valid.')
        if length > limit:
            self.close_connection = True
            if limit >= MAX_UPLOAD_BYTES:
                message = f'The inventory is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB local limit.'
            else:
                message = 'The request was too large.'
            raise ReviewRejected(message, status=413)
        if length == 0:
            return b''
        body = self.rfile.read(length)
        if len(body) != length:
            raise ReviewRejected('The upload ended before the declared length.')
        return body

    def _allowed_origin(self) -> str | None:
        port = self.server.server_address[1]
        host = self.headers.get('Host', '')
        if host == f'127.0.0.1:{port}':
            return f'http://127.0.0.1:{port}'
        if host == f'localhost:{port}':
            return f'http://localhost:{port}'
        return None

    def _authorized_sid(self) -> str | None:
        expected = self._allowed_origin()
        origin = self.headers.get('Origin')
        if not expected or not origin or origin != expected:
            return None
        site = self.headers.get('Sec-Fetch-Site')
        if site not in (None, 'same-origin', 'none'):
            return None
        sid = read_cookie(self.headers.get('Cookie', ''))
        token = self.headers.get('X-CSRF-Token', '')
        if not self.server.csrf_ok(sid, token):
            return None
        return sid

    def _page(self, csrf: str) -> bytes:
        origin = self._allowed_origin() or ''
        page = _INDEX_TEMPLATE
        replacements = {
            '{{CSRF_TOKEN}}': html.escape(csrf, quote=True),
            '{{BIND_ORIGIN}}': html.escape(origin, quote=True),
            '{{APP_VERSION}}': html.escape(APP_VERSION, quote=True),
            '{{ENGINE_NAME}}': html.escape(dj.ENGINE_NAME, quote=True),
            '{{ENGINE_VERSION}}': html.escape(dj.ENGINE_VERSION, quote=True),
            '{{MAX_UPLOAD_MB}}': str(MAX_UPLOAD_BYTES // (1024 * 1024)),
        }
        for token, value in replacements.items():
            page = page.replace(token, value)
        return page.encode('utf-8')

    def _send_html(self, body: bytes, sid: str) -> None:
        self._send(200, body, 'text/html; charset=utf-8', extra=(
            ('Set-Cookie', f'{COOKIE_NAME}={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200'),
        ))

    def _send_json(self, status: int, payload: dict) -> None:
        body = (json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8')
        self._send(status, body, 'application/json; charset=utf-8')

    def _deny(self) -> None:
        self._send_json(403, {'ok': False, 'error': _DENY})

    def _send(self, status: int, body: bytes, content_type: str, extra: tuple[tuple[str, str], ...] = ()) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', CSP)
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        sys.stderr.write('Python 3.11 or newer is required. This process was not started.\n')
        return 1
    parser = argparse.ArgumentParser(description='Local DJ filename-candidate review desk.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    if args.host != '127.0.0.1':
        sys.stderr.write('Refusing to bind anything except 127.0.0.1.\n')
        return 2
    if not 0 <= args.port <= 65535:
        sys.stderr.write('Port must be between 0 and 65535.\n')
        return 2
    try:
        httpd = ReviewServer((args.host, args.port), Handler)
    except OSError as exc:
        sys.stderr.write(f'Could not listen on 127.0.0.1:{args.port} ({exc}).\n')
        return 1
    port = httpd.server_address[1]
    sys.stdout.write(
        f'DJ Library Review listening on http://127.0.0.1:{port}\n'
        'Saved CSV inventories only. Imported paths are not opened. Ctrl+C stops the process.\n'
    )
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write('\nStopped.\n')
    finally:
        httpd.server_close()
    return 0
