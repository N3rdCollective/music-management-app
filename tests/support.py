"""Shared helpers for synthetic review-desk tests."""

from __future__ import annotations

import http.client
import json
import re
import threading
import time
from pathlib import Path

from djlibreview.server import COOKIE_NAME, Handler, ReviewServer

ROOT = Path(__file__).resolve().parents[1]


class Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> dict:
        return json.loads(self.body.decode('utf-8'))

    def text(self) -> str:
        return self.body.decode('utf-8')

    def header(self, name: str) -> str | None:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None


class LocalDesk:
    """A loopback review process on an ephemeral port."""

    def __init__(self) -> None:
        self.httpd = ReviewServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]
        self.origin = f'http://127.0.0.1:{self.port}'
        self.cookie = ''
        self.csrf = ''

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        include_cookie: bool = True,
        include_csrf: bool = False,
    ) -> Response:
        hdrs = {'Host': f'127.0.0.1:{self.port}'}
        if headers:
            hdrs.update(headers)
        if include_cookie and self.cookie:
            hdrs['Cookie'] = self.cookie
        if include_csrf:
            hdrs['X-CSRF-Token'] = self.csrf
            hdrs.setdefault('Origin', self.origin)
            hdrs.setdefault('Sec-Fetch-Site', 'same-origin')
        last: Exception | None = None
        for _ in range(40):
            try:
                conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
                conn.request(method, path, body=body, headers=hdrs)
                resp = conn.getresponse()
                raw = resp.read()
                found = {key: value for key, value in resp.getheaders()}
                status = resp.status
                conn.close()
                return Response(status, found, raw)
            except (ConnectionRefusedError, http.client.RemoteDisconnected, TimeoutError, OSError) as exc:
                last = exc
                time.sleep(0.05)
        raise AssertionError(last)

    def open_page(self) -> Response:
        response = self.request('GET', '/', include_cookie=False)
        set_cookie = response.header('Set-Cookie') or ''
        match = re.search(rf'{COOKIE_NAME}=([^;]+)', set_cookie)
        token = re.search(r'name="csrf-token" content="([^"]+)"', response.text())
        if match is None or token is None:
            raise AssertionError('page did not establish a session')
        self.cookie = f'{COOKIE_NAME}={match.group(1)}'
        self.csrf = token.group(1)
        return response


def multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes]]) -> tuple[str, bytes]:
    boundary = '----djlibreviewtest'
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    for name, (filename, data) in files.items():
        chunks.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\nContent-Type: text/csv\r\n\r\n'
            ).encode()
            + data
            + b'\r\n'
        )
    chunks.append(f'--{boundary}--\r\n'.encode())
    return f'multipart/form-data; boundary={boundary}', b''.join(chunks)
