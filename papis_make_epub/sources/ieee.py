"""Build an EPUB from a saved IEEE Xplore article page.

Xplore keeps the References in a fold: a save holds the bibliography
only if the fold was open. The TeX of the formulas lives in script
elements (MathJax v2's; the page carries no MathML), which a save
must keep.
"""

from collections.abc import Sequence
from logging import Logger
from typing import ClassVar

from bs4 import Tag

from papis_make_epub.formula import TEX, Formula
from papis_make_epub.logging import get_logger
from papis_make_epub.math_renderers import MathRenderer
from papis_make_epub.saved_page import SavedPage
from papis_make_epub.sources.extraction import (
    Content,
    Extraction,
    Source,
    SourceNotAvailableError,
    fragment_targets,
    outermost,
    word_count,
)

logger: Logger = get_logger(__name__)

# below this many words the capture is the page shell
MIN_ARTICLE_WORDS = 500


def _link_targets(a: Tag) -> list[str]:
    """Xplore's cross-references (citations, equation, figure and
    section refs) are ``javascript:void`` links naming their target
    in ``anchor``; a reference entry's id carries an ``-all-ref``
    suffix the citation's anchor lacks."""
    anchor = a.get("anchor")
    if not anchor:
        return fragment_targets(a)
    return [str(anchor), f"{anchor}-all-ref"]


def _strip_cruft(part: Tag) -> None:
    """Drop Xplore's SPA markup that is not valid XHTML.

    The cross-reference links carry ``ref-type``, ``anchor`` (read
    by the link resolution, which runs first) and ``data-range``,
    plus an id keyed by reference-and-section that repeats when a
    reference is cited twice in a section; nothing targets those
    ids, so the attributes go. ``_ngcontent`` marks Angular
    components, ``xplmathjax`` flags math containers, and ``<fig>``
    is Xplore's caption wrapper.
    """
    for el in (part, *part.find_all(True)):
        for attr in list(el.attrs):
            if (attr in ("ref-type", "anchor", "data-range", "xplmathjax")
                    or attr.startswith("_ng")):
                del el[attr]
        if str(el.get("id", "")).startswith("context_ref"):
            del el["id"]
    for fig in part.find_all("fig"):
        fig.unwrap()


def _figures(extraction: Extraction, parts: Sequence[Tag]) -> None:
    """Xplore's figures as HTML figures: ``div.figure`` holds the image
    and its ``div.figcaption``, beside the zoom button's container and
    a "Show All" row, which are reader chrome."""
    extraction.drop(parts, "div.figure .zoom-container, div.figure p.links")
    for fig in (f for part in parts for f in part.select("div.figure")):
        fig.name = "figure"
        for caption in fig.select("div.figcaption"):
            caption.name = "figcaption"


class IeeeSource(Source):
    key = "ieee"
    name = "IEEE Xplore"
    # TeX only; no MathML exists
    math_formats: ClassVar[frozenset[str]] = frozenset({TEX})

    def matches(self, html_text: str) -> bool:
        # Xplore's page-script global; a marker only its own markup
        # carries (an ieeexplore.ieee.org URL also appears in pages
        # that cite Xplore papers)
        return "xplGlobal" in html_text

    def extract(self, page: SavedPage, *,
                renderers: tuple[MathRenderer, MathRenderer],
                best_effort: bool = False) -> Content:
        """Extract the document from a capture of Xplore's rendered
        full text."""
        extraction = Extraction(page, renderers, best_effort=best_effort)
        soup = extraction.soup
        article = soup.select_one("#article")
        if article is None or word_count(article) < MIN_ARTICLE_WORDS:
            raise SourceNotAvailableError(
                "no article text: the capture holds Xplore's page shell, "
                "not the rendered article; save the rendered page with "
                "its script elements (see the README)")

        abstract = soup.select_one(".abstract-text")
        # the References fold: its toggle button and, once opened, its
        # panel share the id; a capture made with the fold closed
        # carries the button alone
        references = soup.select_one("div#references")
        if references is None and soup.select_one("button#references"):
            logger.warning(
                "the References fold was closed when the page was "
                "captured: the book gets no bibliography and its citations "
                "stay as text; open it before capturing")
        sections = outermost(article.select("div.section"))
        if not sections:
            raise SourceNotAvailableError("no div.section under #article")

        parts = ([abstract] if abstract is not None else []) + sections
        # span.formula is Xplore's per-equation helper UI (a View
        # Source link, a support icon, a hidden TeX duplicate),
        # preserved by fuller captures
        extraction.drop(parts, "span.formula, .MathJax, .MathJax_Preview")
        if references is not None:
            # the panel's export header, the per-entry link rows (Show
            # in Context, View Article, PDF, Google Scholar, CrossRef),
            # the citation-map button and the export selection (Select
            # All, a checkbox per entry: the panel's hide-mobile
            # elements) are reader chrome
            extraction.drop([references],
                            ".reference-header-container, "
                            ".ref-links-container, xpl-citation-map-button, "
                            ".hide-mobile")
        formulas = []
        for scr in (s for part in parts
                    for s in part.select('script[type^="math/tex"]')):
            wrapper = scr.find_parent("tex-math")
            formulas.append(Formula(
                node=wrapper if wrapper is not None else scr,
                tex=scr.get_text().strip(),
                display="mode=display" in str(scr.get("type"))))
        extraction.render_math(formulas)

        # the references sit outside #article; a collapsed accordion
        # leaves them empty, and the citations then become text
        nodes = parts + ([references] if references is not None else [])
        extraction.resolve_links(nodes, _link_targets)
        for node in nodes:
            _strip_cruft(node)
        _figures(extraction, parts)

        extraction.embed_figures(parts)

        if abstract is not None:
            # the abstract is bare text in its div; a paragraph gives
            # it a block of its own
            paragraph = soup.new_tag("p")
            paragraph.extend(list(abstract.children))
            abstract.append(paragraph)
            extraction.add_abstract(abstract)
        extraction.add_sections(sections)
        if references is not None:
            # the fold's heading is its toggle button, outside the panel;
            # the part gets one of its own for the TOC
            heading = soup.new_tag("h2")
            heading.string = "References"
            references.insert(0, heading)
            extraction.add_sections([references], "bibliography")

        return extraction.content()
