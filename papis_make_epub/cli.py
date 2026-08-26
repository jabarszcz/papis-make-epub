"""The ``papis make-epub`` command: build EPUB renditions for documents.

For each matching document, a saved full-text page among its files
builds the EPUB through the source detected from the page; without
one, the arXiv rendition named by the document's metadata is fetched.
The EPUB lands in the document's folder, is appended to its
``files``, and the document saved; documents without a resolvable
source, or already carrying an EPUB, are reported and skipped. With a
saved page as FILE, the one matching document is built from it
instead, through the detected source (or the one named by
``--from``).
"""

from collections import Counter
from collections.abc import Callable
from logging import Logger
from pathlib import Path
from typing import Literal

import click
import papis.cli
import papis.logging
from papis.document import Document, describe

from papis_make_epub.api import (
    arxiv_builder,
    build_epub,
    doc_meta,
    existing_epub,
    matching_sources,
    page_builder,
    renderer_resolver,
    saved_pages,
)
from papis_make_epub.book import BuildOptions
from papis_make_epub.errors import BuildError
from papis_make_epub.fetch import FetchError
from papis_make_epub.logging import get_logger
from papis_make_epub.math_renderers import RENDERERS, MathRenderer
from papis_make_epub.saved_page import SavedPage
from papis_make_epub.sources import SOURCES, Source
from papis_make_epub.sources.arxiv import ArxivSource, arxiv_id_from_doc
from papis_make_epub.sources.extraction import SourceNotAvailableError

logger: Logger = get_logger(__name__)


_MATH_HELP = "Renderers: " + "; ".join(
    f"'{name}' = {cls.help}" for name, cls in RENDERERS.items()) + "."


@click.command("make-epub")
@click.help_option("--help", "-h")
@papis.cli.query_argument()
# a locally saved page to build from
@click.argument("file", required=False,
                type=click.Path(exists=True, dir_okay=False),
                metavar="[FILE]")
@papis.cli.all_option()
@papis.cli.bool_flag("--force",
                     help="Rebuild even if the document already has an EPUB.")
@click.option("--from", "source_key",
              type=click.Choice(tuple(s.key for s in SOURCES)),
              default=None,
              help="Which source reads FILE, overriding the detection.")
@click.option("--math", "math_both",
              type=click.Choice(tuple(RENDERERS)), default=None,
              help="Renderer for both inline and display math; "
                   "--math-inline and --math-display override it. "
                   + _MATH_HELP)
@click.option("--math-inline", "math_inline",
              type=click.Choice(tuple(RENDERERS)), default=None,
              help="Renderer for inline math (default mathml).")
@click.option("--math-display", "math_display",
              type=click.Choice(tuple(RENDERERS)), default=None,
              help="Renderer for display math (default mathml).")
@papis.cli.bool_flag("--best-effort",
                     help="Keep building when the selected renderer cannot "
                          "render a formula: rendering falls back down the "
                          "renderer ladder, at worst keeping the formula as "
                          "the source supplied it.")
@papis.cli.bool_flag("--no-char-normalization",
                     help="Keep the source's nonstandard math codepoints "
                          "(skip the fullwidth/CJK-to-standard character "
                          "normalization applied to the document text).")
@click.option("--css", "css_file",
              type=click.Path(exists=True, dir_okay=False), default=None,
              help="Stylesheet added after the bundled style.css; its "
                   "rules win on conflicts.")
def cli(query: str, source_key: str | None,  # noqa: PLR0913, PLR0917
        file: str | None, _all: bool, force: bool,
        math_both: str | None,
        math_inline: str | None, math_display: str | None,
        best_effort: bool, no_char_normalization: bool,
        css_file: str | None) -> None:
    """Build EPUB renditions of papis documents.

    Each document builds from the saved full-text page detected among
    its own files; without one, the arXiv rendition named by its
    metadata is fetched. A page passed as FILE overrides both:

        papis make-epub <query> saved-page.html

    The query must then match exactly one document; ``--from`` picks
    the source when the detection guesses wrong.
    """
    # the flag defaults mirror BuildOptions, where the pass-through
    # choice is motivated
    math_inline = math_inline or math_both or "mathml"
    math_display = math_display or math_both or "mathml"

    css = None
    if css_file is not None:
        with open(css_file, "rb") as cf:
            css = cf.read()

    if source_key is not None and file is None:
        logger.error("--from %s names the source of a saved page; "
                     "give the file too.", source_key)
        raise SystemExit(1)

    options = BuildOptions(
        math_inline=math_inline, math_display=math_display, css=css,
        normalize_chars=not no_char_normalization,
        best_effort=best_effort)
    renderers_for = renderer_resolver(options)

    documents = papis.cli.handle_doc_folder_query_all_sort(
        query, (), None, False, _all)
    if not documents:
        logger.warning("No documents retrieved.")
        return

    if file is not None:
        if len(documents) != 1:
            logger.error("A saved-page build needs the query to match "
                         "exactly one document, got %d.", len(documents))
            raise SystemExit(1)
        doc = documents[0]
        if (existing := existing_epub(doc)) and not force:
            logger.info("Skipping '%s': already has '%s' (use --force).",
                        describe(doc), existing)
            return
        page = SavedPage.from_file(Path(file))
        source = _file_source(page, file, source_key)
        logger.info("Building from %s.", source.name)
        _build_file(doc, page, source, renderers_for, options)
        return

    counts = Counter(_build_document(doc, renderers_for, options, force)
                     for doc in documents)
    logger.info("Done: %d built, %d skipped, %d failed.",
                counts["built"], counts["skipped"], counts["failed"])
    if counts["failed"]:
        raise SystemExit(1)


_Resolver = Callable[[frozenset[str]], tuple[MathRenderer, MathRenderer]]
# a document's fate in the library loop, tallied for the summary line
_Outcome = Literal["built", "skipped", "failed"]


def _file_source(page: SavedPage, file: str, source_key: str | None) -> Source:
    """The source reading FILE: ``--from``'s, or the one the detection
    yields; no match or several is a usage error."""
    if source_key is not None:
        return next(s for s in SOURCES if s.key == source_key)
    matches = matching_sources(page.text)
    if len(matches) > 1:
        logger.error("'%s' matches several sources (%s); --from "
                     "picks one.", file,
                     ", ".join(s.key for s in matches))
        raise SystemExit(1)
    if not matches:
        logger.error("'%s' is not recognized full-text HTML; "
                     "supported saved pages: %s (--from picks one "
                     "explicitly).", file,
                     ", ".join(s.name for s in SOURCES))
        raise SystemExit(1)
    return matches[0]


def _build_file(doc: Document, page: SavedPage, source: Source,
                renderers_for: _Resolver, options: BuildOptions) -> None:
    """Build *doc* from the page the user named; any failure exits."""
    try:
        renderers = renderers_for(source.math_formats)
        build_epub(doc, page_builder(
            page, source, doc_meta(doc), renderers, options))
    except BuildError as exc:
        # the user named this file; not building it is a failure
        logger.error("%s", exc)
        raise SystemExit(1) from exc
    except Exception as exc:
        logger.error("Failed on '%s'.", describe(doc), exc_info=exc)
        raise SystemExit(1) from exc


def _build_document(doc: Document, renderers_for: _Resolver,
                    options: BuildOptions, force: bool) -> _Outcome:
    """Build one library document from its saved page or its arXiv
    rendition, logging the outcome."""
    name = describe(doc)

    existing = existing_epub(doc)
    if existing and not force:
        logger.info("Skipping '%s': already has '%s' (use --force).",
                    name, existing)
        return "skipped"

    pages = saved_pages(doc)
    if len(pages) > 1:
        logger.error("Cannot build '%s': several saved pages among "
                     "its files (%s); pass one as FILE.", name,
                     ", ".join(p.path.name if p.path else ""
                               for p, _ in pages))
        return "failed"
    if pages:
        page, matches = pages[0]
        return _build_from_page(doc, page, matches, renderers_for, options)
    return _build_from_arxiv(doc, renderers_for, options)


def _build_from_page(doc: Document, page: SavedPage, matches: list[Source],
                     renderers_for: _Resolver,
                     options: BuildOptions) -> _Outcome:
    name = describe(doc)
    if len(matches) > 1:
        logger.error("Cannot build '%s': '%s' matches several "
                     "sources (%s); pass it as FILE with --from.",
                     name, page.path.name if page.path else "",
                     ", ".join(s.key for s in matches))
        return "failed"
    source = matches[0]
    logger.info("Building '%s' from its saved %s page.", name, source.name)
    try:
        renderers = renderers_for(source.math_formats)
        build_epub(doc, page_builder(
            page, source, doc_meta(doc), renderers, options))
    except BuildError as exc:
        # the library carries this page, so not building it is a
        # failure, and no fetch is attempted in its place
        logger.error("Cannot build '%s': %s", name, exc)
        return "failed"
    except Exception as exc:
        logger.error("Failed on '%s'.", name, exc_info=exc)
        return "failed"
    return "built"


def _build_from_arxiv(doc: Document, renderers_for: _Resolver,
                      options: BuildOptions) -> _Outcome:
    name = describe(doc)
    arxiv_id = arxiv_id_from_doc(doc)
    if arxiv_id is None:
        logger.info("Skipping '%s': no arXiv source in metadata.", name)
        return "skipped"

    try:
        renderers = renderers_for(ArxivSource.math_formats)
        build_epub(doc, arxiv_builder(
            arxiv_id, doc_meta(doc), renderers, options))
    except SourceNotAvailableError as exc:
        # expected for part of the library (papers with no HTML
        # rendition), but worth the user's attention
        logger.warning("Skipping '%s': %s", name, exc)
        return "skipped"
    except FetchError as exc:
        # the rendition may exist: the server refused a request or the
        # network failed; a page saved from a browser builds unfetched
        logger.error("Cannot build '%s': %s. Save the rendition page from "
                     "a browser, add it to the document's files, and build "
                     "again.", name, exc)
        return "failed"
    except BuildError as exc:
        logger.error("Cannot build '%s': %s", name, exc)
        return "failed"
    except Exception as exc:
        logger.error("Failed on '%s'.", name, exc_info=exc)
        return "failed"
    return "built"


def main() -> None:
    """Entry point for the standalone ``papis-make-epub`` script, where papis
    itself has not configured logging."""
    papis.logging.setup("INFO")
    cli()


if __name__ == "__main__":
    main()
