"""Build an EPUB from a saved IEEE Xplore article page.

Xplore keeps the References and the Footnotes in folds of one
accordion, at most one open at a time: a save holds the bibliography
or the footnotes only if its fold was open. The TeX of the formulas
lives in script elements, which a save must keep (see
:func:`_formulas`).
"""

import re
from collections.abc import Sequence
from logging import Logger
from typing import ClassVar

from papis_make_epub.formula import TEX, Formula
from papis_make_epub.logging import get_logger
from papis_make_epub.math_renderers import MathRenderer
from papis_make_epub.saved_page import SavedPage
from papis_make_epub.sources.extraction import (
    Content,
    Extraction,
    Source,
    SourceNotAvailableError,
    outermost,
    word_count,
)
from papis_make_epub.sources.node import Node, Target, target

logger: Logger = get_logger(__name__)

# below this many words the capture is the page shell
MIN_ARTICLE_WORDS = 500


def _formulas(extraction: Extraction, parts: Sequence[Node]) -> list[Formula]:
    """The formulas under *parts*, each with its TeX, whether MathJax
    (v2) had typeset it when the page was captured or not; the page
    carries no MathML."""
    formulas = []
    for part in parts:
        # typeset: the TeX sits in a MathJax script, inside the
        # tex-math wrapper for an inline formula and beside it for a
        # display one (the wrapper then holds MathJax output alone)
        for script in part.select('script[type^="math/tex"]'):
            wrapper = script.parent("tex-math")
            formulas.append(extraction.formula(
                wrapper if wrapper is not None else script,
                tex=script.text().strip(),
                display=script.matches("[type*='mode=display']")))
        # untypeset: the tex-math holds the TeX as its only content,
        # delimited by \$ (the ieee-footnotes reduction's panel, caught
        # before MathJax typeset it)
        for tex_math in part.select("tex-math:not(:has(*)):not(:empty)"):
            tex = tex_math.text().strip()
            if not tex:
                continue
            if m := re.fullmatch(r"\\?\$(.*?)\\?\$", tex, re.S):
                tex = m.group(1).strip()
            formulas.append(extraction.formula(
                tex_math, tex=tex,
                display=tex_math.parent("disp-formula") is not None))
    return formulas


def _link_targets(a: Node) -> list[Target]:
    """Xplore's cross-references (citations, footnote marks, equation,
    figure and section refs) are ``javascript:void`` links naming
    their target in ``anchor``; a reference entry's id carries an
    ``-all-ref`` suffix the citation's anchor lacks, and a footnote's
    a ``footnotes-desktop-`` prefix the mark's anchor lacks."""
    anchor = a.attr("anchor")
    if not anchor:
        return []
    return [target(anchor), target(f"{anchor}-all-ref"),
            target(f"footnotes-desktop-{anchor}")]


def _strip_cruft(part: Node) -> None:
    """Drop Xplore's SPA markup that is not valid XHTML.

    The cross-reference links carry ``ref-type``, ``anchor`` (read
    by the link resolution, which runs first) and ``data-range``,
    plus an id keyed by reference-and-section that repeats when a
    reference is cited twice in a section; nothing targets those
    ids, so the attributes go. ``_ngcontent`` marks Angular
    components, ``xplmathjax``, ``xplfulltextdomhandler`` and
    ``context-events`` are Angular directives (the last two on the
    footnotes panel), and ``<fig>`` is Xplore's caption wrapper.
    """
    part.strip_attributes("ref-type", "anchor", "data-range", "xplmathjax",
                          "xplfulltextdomhandler", "context-events", r"_ng.*")
    for el in part.select("[id^='context_ref']"):
        el.delete("id")
    for fig in part.select("fig"):
        fig.unwrap()


def _figures(extraction: Extraction, parts: Sequence[Node]) -> None:
    """Xplore's figures as HTML figures: ``div.figure`` holds the image
    and its ``div.figcaption``, beside the zoom button's container and
    a "Show All" row, which are reader chrome."""
    extraction.drop(parts, "div.figure .zoom-container, div.figure p.links")
    for fig in (f for part in parts for f in part.select("div.figure")):
        fig.rename("figure")
        for caption in fig.select("div.figcaption"):
            caption.rename("figcaption")


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
        root = extraction.root
        article = root.first("#article")
        if article is None or word_count(article) < MIN_ARTICLE_WORDS:
            raise SourceNotAvailableError(
                "no article text: the capture holds Xplore's page shell, "
                "not the rendered article; save the rendered page with "
                "its script elements (see the README)")

        abstract = root.first(".abstract-text")
        # the References and Footnotes folds: a fold's toggle button
        # and, once opened, its panel share the id; a capture made with
        # the fold closed carries the button alone. The folds are one
        # accordion, so a capture holds one of them open.
        references = root.first("div#references")
        if references is None and root.has("button#references"):
            logger.warning(
                "the References fold was closed when the page was "
                "captured: the book gets no bibliography and its citations "
                "stay as text; open it before capturing")
        footnotes_section = root.first("div#footnotes")
        if (footnotes_section is None and root.has("button#footnotes")
                and article.has("a.footnote-link")):
            logger.warning(
                "the Footnotes fold was closed when the page was "
                "captured: the book gets no footnotes and their marks stay "
                "as text")
        sections = outermost(article.select("div.section"))
        if not sections:
            raise SourceNotAvailableError("no div.section under #article")

        parts = ([abstract] if abstract is not None else []) + sections
        parts += [footnotes_section] if footnotes_section is not None else []
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
        extraction.render_math(_formulas(extraction, parts))

        # the references sit outside #article; a collapsed accordion
        # leaves them empty, and the citations then become text
        nodes = parts + ([references] if references is not None else [])
        extraction.resolve_links(nodes, _link_targets)
        for node in nodes:
            _strip_cruft(node)
        _figures(extraction, parts)

        extraction.embed_figures(parts)

        if abstract is not None:
            extraction.add_abstract(abstract)
        extraction.add_sections(sections)
        if references is not None:
            # the fold's heading is its toggle button, outside the panel;
            # the part gets one of its own for the TOC
            references.insert(0, extraction.new("h2", text="References"))
            extraction.add_bibliography(
                references, references.select("div.reference-container"))
        if footnotes_section is not None:
            # the same for the Footnotes fold
            footnotes_section.insert(0, extraction.new("h2", text="Footnotes"))
            extraction.add_footnotes(
                footnotes_section,
                footnotes_section.select("div[id^='footnotes-desktop-fn']"))

        return extraction.content()
