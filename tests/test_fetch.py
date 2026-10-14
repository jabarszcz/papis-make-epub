"""The fetch route against a local server.

The committed arXiv capture is what the route saved from arXiv, the
page and its figures byte for byte, so a server handing them back
lets the real client code run offline: the request spacing, the
page validation, the figure download. The server's answers then
drive the policy the route documents: a refusal for the moment is
retried, a 406 is reported at once, a missing rendition falls to
ar5iv. The observations behind the 406 rule are in the fetch
module, at ``REFUSED``.
"""

import http.server
import logging
import threading
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from conftest import FIXTURES

from papis_make_epub import fetch
from papis_make_epub.fetch import FetchError
from papis_make_epub.sources import arxiv
from papis_make_epub.sources.arxiv import fetch_html, fetch_page
from papis_make_epub.sources.extraction import SourceNotAvailableError

CAPTURE = FIXTURES / "library" / "arxiv"
ARXIV_ID = "2312.00752"
PAGE_PATH = f"/html/{ARXIV_ID}"
AR5IV_PAGE_PATH = f"/ar5iv/html/{ARXIV_ID}"


class _State:
    """The server's script and log: statuses to answer per path
    before serving the file, and every request's path and agent."""

    def __init__(self) -> None:
        self.statuses: dict[str, list[int]] = {}
        self.requests: list[tuple[str, str | None]] = []


class _Server(http.server.ThreadingHTTPServer):
    state: _State


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        assert isinstance(self.server, _Server)
        state = self.server.state
        state.requests.append((self.path, self.headers.get("User-Agent")))
        queued = state.statuses.get(self.path)
        status = queued.pop(0) if queued else HTTPStatus.OK
        body = self._body() if status == HTTPStatus.OK else None
        if body is None:
            self.send_error(HTTPStatus.NOT_FOUND if status == HTTPStatus.OK
                            else status)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes | None:
        # the page under either host's /html/, the figures by their
        # URL-relative path, which the sidecar keeps
        if "/html/" not in self.path:
            return None
        rel = self.path.split("/html/", 1)[1]
        if rel == ARXIV_ID:
            return (CAPTURE / "arxiv.html").read_bytes()
        file = CAPTURE / "arxiv_files" / rel
        return file.read_bytes() if file.is_file() else None


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[_State]:
    httpd = _Server(("127.0.0.1", 0), _Handler)
    httpd.state = _State()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setattr(arxiv, "ARXIV_HTML_BASE", base + "/html/")
    monkeypatch.setattr(arxiv, "AR5IV_HTML_BASE", base + "/ar5iv/html/")
    # no waiting in the test; the retries stay observable in the log
    monkeypatch.setattr(fetch, "PAGE_INTERVAL", 0.0)
    monkeypatch.setattr(fetch, "ASSET_INTERVAL", 0.0)
    monkeypatch.setattr(fetch, "RETRY_WAITS", (0,))
    try:
        yield httpd.state
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_fetch_page_replays_the_capture(server: _State,
                                        tmp_path: Path) -> None:
    fetch_page(ARXIV_ID, str(tmp_path))

    expected = (CAPTURE / "arxiv.html").read_text(encoding="utf-8")
    assert (tmp_path / "page.html").read_text(encoding="utf-8") == expected
    committed = {p.relative_to(CAPTURE / "arxiv_files"): p.read_bytes()
                 for p in (CAPTURE / "arxiv_files").rglob("*") if p.is_file()}
    saved = {p.relative_to(tmp_path / "page_files"): p.read_bytes()
             for p in (tmp_path / "page_files").rglob("*") if p.is_file()}
    assert saved == committed
    assert server.requests[0] == (PAGE_PATH, fetch.USER_AGENT)
    assert {agent for _, agent in server.requests} == {fetch.USER_AGENT}


def test_missing_rendition_falls_to_ar5iv(server: _State) -> None:
    server.statuses[PAGE_PATH] = [404]
    _text, url = fetch_html(ARXIV_ID)
    assert url.endswith(AR5IV_PAGE_PATH)
    assert [path for path, _ in server.requests] == [PAGE_PATH,
                                                     AR5IV_PAGE_PATH]


def test_no_rendition_anywhere(server: _State) -> None:
    server.statuses[PAGE_PATH] = [404]
    server.statuses[AR5IV_PAGE_PATH] = [404]
    with pytest.raises(SourceNotAvailableError):
        fetch_html(ARXIV_ID)


def test_refusal_for_the_moment_is_retried(server: _State,
                                           caplog: Any) -> None:
    caplog.set_level(logging.WARNING)
    server.statuses[PAGE_PATH] = [429]
    fetch_html(ARXIV_ID)
    assert [path for path, _ in server.requests] == [PAGE_PATH, PAGE_PATH]
    assert any("retrying in 0 s (1 of 1)" in r.message for r in caplog.records)


def test_406_is_reported_without_retry(server: _State) -> None:
    server.statuses[PAGE_PATH] = [406]
    with pytest.raises(FetchError) as caught:
        fetch_html(ARXIV_ID)
    assert caught.value.status == HTTPStatus.NOT_ACCEPTABLE
    assert "refuses this client" in str(caught.value)
    assert [path for path, _ in server.requests] == [PAGE_PATH]


@pytest.mark.parametrize(("value", "urls"), [
    ("a.png", ["a.png"]),
    ("a.png 1x, b.png 2x", ["a.png", "b.png"]),
    ("a.png, b.png", ["a.png", "b.png"]),
    (" , a.png 100w ", ["a.png"]),
    # a data: URL holds commas; it runs to the whitespace before its
    # descriptor
    ("data:image/webp;base64,AAA= 685w, https://x/y.png 2x",
     ["data:image/webp;base64,AAA=", "https://x/y.png"]),
    ("", []),
])
def test_srcset_urls(value: str, urls: list[str]) -> None:
    assert fetch.srcset_urls(value) == urls
