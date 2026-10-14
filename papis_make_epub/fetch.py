"""Polite fetching and a page's embedded-media vocabulary.

An identifying User-Agent rides every request; requests are spaced,
pages more widely than a page's own assets; a request the server
refuses for the moment (429, 503) is retried up to three times, with
growing waits; a 406 is reported at once (see ``REFUSED``).

The media vocabulary is the HTML standard's: the URL-carrying
attributes of the elements that embed media (img, source, object,
embed, audio, video, track, and input's image button), per the
standard's attributes index,
https://html.spec.whatwg.org/multipage/indices.html (verified
2026-08-28). script and iframe are outside the vocabulary: code and
nested documents are not media.
"""

import time
import urllib.error
import urllib.parse
import urllib.request
from logging import Logger

from bs4 import Tag

from papis_make_epub import __version__
from papis_make_epub.logging import get_logger

logger: Logger = get_logger(__name__)

USER_AGENT = f"papis-make-epub/{__version__}"
# minimum spacing between requests, seconds: after a page, and after
# one of its assets
PAGE_INTERVAL = 3.0
ASSET_INTERVAL = 1.0
# the statuses that mean "not now", and the waits before each retry
RETRY_STATUSES = (429, 503)
RETRY_WAITS = (15, 60, 180)
# 406, what arXiv's edge firewall answers a blocked request with; in
# October 2026 it outlasted retries for minutes to a day, whatever
# User-Agent string this client sent, while curl was served
REFUSED = 406
_next_request = 0.0

MEDIA_URL_ATTRS = (
    ("img", "src"),
    ("source", "src"),
    ("object", "data"),
    ("embed", "src"),
    ("audio", "src"),
    ("video", "src"),
    ("video", "poster"),
    ("track", "src"),
    ("input", "src"),
)
# srcset attributes carry a comma-separated candidate list
MEDIA_SRCSET_ATTRS = (
    ("img", "srcset"),
    ("source", "srcset"),
)


class FetchError(Exception):
    """A request failed: the message names the URL and the HTTP status
    (``status``), or the network failure (status None)."""

    def __init__(self, url: str, status: int | None, reason: str) -> None:
        self.status = status
        where = f"HTTP {status} {reason}" if status else reason
        hint = ("; the server refuses this client on this URL (other "
                "clients, such as a browser or curl, may be served)"
                if status == REFUSED else "")
        super().__init__(f"{where} for {url}{hint}")


def _space() -> None:
    wait = _next_request - time.monotonic()
    if wait > 0:
        time.sleep(wait)


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data: bytes = resp.read()
            return data
    except urllib.error.HTTPError as exc:
        raise FetchError(url, exc.code, exc.reason) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FetchError(url, None, str(getattr(exc, "reason", exc))) from exc


def polite_get(url: str, *, page: bool = False) -> bytes:
    """GET with an identifying User-Agent, spaced from the previous
    request and retried while the server refuses for the moment.
    A request that fails for good raises FetchError with its status."""
    global _next_request  # noqa: PLW0603
    if page:
        logger.info("Fetching '%s'.", url)
    for attempt, retry_wait in enumerate((*RETRY_WAITS, None)):
        _space()
        try:
            return _get(url)
        except FetchError as exc:
            if exc.status not in RETRY_STATUSES or retry_wait is None:
                raise
            logger.warning("%s; retrying in %d s (%d of %d)", exc, retry_wait,
                           attempt + 1, len(RETRY_WAITS))
            time.sleep(retry_wait)
        finally:
            _next_request = time.monotonic() + (PAGE_INTERVAL if page
                                                else ASSET_INTERVAL)
    raise AssertionError("unreachable: the last attempt raises or returns")


def resolve_base(soup: Tag, page_url: str) -> str:
    """The URL that the page's relative asset links resolve against.

    A ``<base>`` element overrides the page URL (some pages carry
    bare srcs like ``x1.png`` that only the ``<base>`` can place).
    """
    base_el = soup.find("base")
    if isinstance(base_el, Tag) and base_el.get("href"):
        return urllib.parse.urljoin(page_url, str(base_el["href"]))
    return page_url


def _split_url(rest: str) -> tuple[str, str]:
    """*rest*'s leading non-whitespace run, and what follows it."""
    parts = rest.split(None, 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def srcset_urls(value: str) -> list[str]:
    """The URLs of a srcset candidate list, split as the HTML parsing
    algorithm splits it: a URL runs to the next whitespace (a
    ``data:`` URL holds commas), its trailing commas dropped, and a
    candidate's descriptors run to the next comma."""
    urls = []
    rest = value
    while rest := rest.lstrip(" \t\n\r\f,"):
        url, rest = _split_url(rest)
        if url.endswith(","):
            urls.append(url.rstrip(","))
            continue
        urls.append(url)
        _, _, rest = rest.partition(",")
    return urls


def media_urls(root: Tag) -> list[str]:
    """The embedded-media URLs under *root*."""
    urls: list[str] = []
    for tag, attr in MEDIA_URL_ATTRS:
        for el in root.find_all(tag):
            if el.get(attr):
                urls.append(str(el[attr]))
    for tag, attr in MEDIA_SRCSET_ATTRS:
        for el in root.find_all(tag):
            if el.get(attr):
                urls.extend(srcset_urls(str(el[attr])))
    return urls
