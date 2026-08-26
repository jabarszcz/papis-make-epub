"""The library's operations: what ``papis make-epub`` does, importable.

One EPUB per papis document. The book's identity derives from the
bibliographic record (doc_meta, epub_name), its input is a saved
page among the document's files (saved_pages) or the fetched arXiv
rendition (arxiv_builder), and build_epub owns the
build-and-register sequence. cli.py is the command-line
presentation of these operations; the builders take a DocMeta
directly, so building outside a papis library needs only them.
"""

import os
import tempfile
from collections.abc import Callable
from logging import Logger
from pathlib import Path

from papis.api import save_doc
from papis.document import Document, describe

from papis_make_epub.book import BuildOptions, DocMeta, write_epub
from papis_make_epub.logging import get_logger
from papis_make_epub.math_renderers import MathRenderer, make_renderer
from papis_make_epub.saved_page import SavedPage
from papis_make_epub.sources import SOURCES, Source
from papis_make_epub.sources.arxiv import arxiv_id_from_doc, fetch_page
from papis_make_epub.sources.extraction import BuildStats

logger: Logger = get_logger(__name__)

# the arXiv source also serves the fetch path
_ARXIV = next(s for s in SOURCES if s.key == "arxiv")


def author_names(doc: Document) -> list[str]:
    names = []
    for a in doc.get("author_list") or []:
        n = f"{a.get('given') or ''} {a.get('family') or ''}".strip()
        if n:
            names.append(n)
    if not names and doc.get("author"):
        names = [str(doc["author"])]
    return names


def epub_name(doc: Document) -> str:
    for f in doc.get("files") or []:
        if f.lower().endswith(".pdf"):
            return os.path.splitext(str(f))[0] + ".epub"
    return str(doc.get("ref") or doc.get("papis_id") or "document") + ".epub"


def doc_meta(doc: Document) -> DocMeta:
    """The book's identity, derived from the bibliographic record.

    The identifier prefers the DOI, then the arXiv id (in its
    ``arxiv:<id>`` citation form), then papis's own id.
    """
    arxiv_id = arxiv_id_from_doc(doc)
    identifier = (doc.get("doi")
                  or (f"arxiv:{arxiv_id}" if arxiv_id else None)
                  or doc.get("papis_id") or epub_name(doc))
    return DocMeta(title=str(doc.get("title") or describe(doc)),
                   authors=author_names(doc),
                   identifier=str(identifier))


def existing_epub(doc: Document) -> str | None:
    for f in doc.get("files") or []:
        if f.lower().endswith(".epub"):
            return str(f)
    return None


def matching_sources(html_text: str) -> list[Source]:
    """The sources whose detection accepts *html_text*."""
    return [s for s in SOURCES if s.matches(html_text)]


def saved_pages(doc: Document) -> list[tuple[SavedPage, list[Source]]]:
    """Detect saved full-text pages among the document's files.

    Every ``.html``/``.htm`` file is read through the source
    detection; each recognized page yields (page, matching sources).
    The rest are logged and passed over.
    """
    out: list[tuple[SavedPage, list[Source]]] = []
    for path in doc.get_files():
        if not path.lower().endswith((".html", ".htm")):
            continue
        try:
            page = SavedPage.from_file(Path(path))
        except OSError as exc:
            logger.info("Ignoring '%s': %s.", os.path.basename(path), exc)
            continue
        matches = matching_sources(page.text)
        if matches:
            out.append((page, matches))
        else:
            logger.info("Ignoring '%s': no source matches.",
                        os.path.basename(path))
    return out


def renderer_resolver(options: BuildOptions) -> Callable[
        [frozenset[str]], tuple[MathRenderer, MathRenderer]]:
    """Per-math-formats (inline, display) renderers from the
    options' names.

    Renderer construction is the availability gate, and it needs the
    math formats the source supplies; the source varies per document,
    so the returned callable constructs where the formats are known,
    one cached pair per format set (a cache hit reuses the
    instances, mathjax's co-process included; one name for both
    positions yields one instance serving both).
    RendererUnavailableError propagates from the call.
    """
    cache: dict[frozenset[str], tuple[MathRenderer, MathRenderer]] = {}

    def resolve(math_formats: frozenset[str],
                ) -> tuple[MathRenderer, MathRenderer]:
        if math_formats not in cache:
            inline = make_renderer(options.math_inline,
                                   math_formats=math_formats)
            display = (inline
                       if options.math_display == options.math_inline
                       else make_renderer(options.math_display,
                                          math_formats=math_formats))
            cache[math_formats] = (inline, display)
        return cache[math_formats]

    return resolve


def page_builder(page: SavedPage, source: Source, meta: DocMeta,
                 renderers: tuple[MathRenderer, MathRenderer],
                 options: BuildOptions,
                 ) -> Callable[[str], BuildStats]:
    """The build callable for one saved page (see build_epub)."""
    def build(tmp_path: str) -> BuildStats:
        content = source.extract(page, renderers=renderers,
                                 best_effort=options.best_effort)
        content.add_title_page(meta.title, meta.authors)
        write_epub(tmp_path, meta=meta, content=content, options=options)
        return content.stats
    return build


def arxiv_builder(arxiv_id: str, meta: DocMeta,
                  renderers: tuple[MathRenderer, MathRenderer],
                  options: BuildOptions,
                  ) -> Callable[[str], BuildStats]:
    """The build callable for a fetched arXiv rendition; a failed
    fetch raises :class:`papis_make_epub.fetch.FetchError`."""
    def build(tmp_path: str) -> BuildStats:
        # the fetch materializes page and figures in a temp dir, so
        # extraction is the same local run the saved-page path does;
        # the content carries the figures as bytes, so the dir can go
        # before the write
        with tempfile.TemporaryDirectory(
                prefix="papis-make-epub-") as fetched:
            page = fetch_page(arxiv_id, fetched)
            content = _ARXIV.extract(page, renderers=renderers,
                                     best_effort=options.best_effort)
        content.add_title_page(meta.title, meta.authors)
        write_epub(tmp_path, meta=meta, content=content, options=options)
        return content.stats
    return build


def build_epub(doc: Document,
               build: Callable[[str], BuildStats]) -> Path:
    """Build the document's EPUB via *build*, register it, and
    return the written path.

    The EPUB lands in the document's folder under :func:`epub_name`,
    written through a ``.part`` temp name so a failed rebuild cannot
    destroy an EPUB the document already has; on any error the temp
    file is removed and the exception propagates. On success the
    name is appended to the document's ``files`` and the document
    saved.
    """
    folder = doc.get_main_folder()
    if folder is None:
        raise ValueError(f"'{describe(doc)}' has no main folder")

    out_name = epub_name(doc)
    out_path = os.path.join(folder, out_name)
    tmp_path = out_path + ".part"
    try:
        stats = build(tmp_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    os.replace(tmp_path, out_path)

    if out_name not in (doc.get("files") or []):
        doc["files"] = [*(doc.get("files") or []), out_name]
    save_doc(doc)
    logger.info("Built '%s' (%d math, %d rendered as images, "
                "%d fallbacks, %d figures, %d dead links).",
                out_name, stats.math, stats.math_images,
                stats.math_fallback, stats.images, stats.dead_links)
    return Path(out_path)
