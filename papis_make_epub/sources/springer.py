"""Build an EPUB from a saved Springer Nature article page.

Covers link.springer.com and nature.com, which share the platform
markup. The page's MathJax (v2) moves the TeX of each formula into a
script element as it renders it; a save without the page's scripts
keeps only MathJax's MathML (see :func:`_formula`).
"""

import re
from typing import ClassVar

from papis_make_epub.formula import MATHML, TEX, Formula
from papis_make_epub.math_renderers import MathRenderer
from papis_make_epub.saved_page import SavedPage
from papis_make_epub.sources.extraction import (
    Content,
    Extraction,
    Source,
    SourceNotAvailableError,
    fragment_targets,
    outermost,
)
from papis_make_epub.sources.node import Node, Target, target

# platform boilerplate sections dropped from the book (content
# sections carry a data-title and no class; chrome sections carry
# classes or these titles)
_CHROME_TITLES = (
    "About this article", "About this paper", "Author information",
    "Comments", "Copyright information", "Editor information",
    "Publish with us", "Rights and permissions",
)
_CHROME = ", ".join(f"[data-title='{t}']" for t in _CHROME_TITLES)


def _content_sections(body: Node) -> tuple[Node | None, list[Node]]:
    """The abstract section, if any, and the other content sections
    under *body*."""
    sections = []
    abstract = None
    for s in outermost(body.select("section")):
        if s.matches("[class]") or not s.matches("[data-title]"):
            continue
        if s.matches(_CHROME):
            continue
        if s.matches("[data-title='Abstract']"):
            abstract = s
        else:
            sections.append(s)
    return abstract, sections


def _unwrap_pictures(parts: list[Node]) -> None:
    """Replace each ``picture`` with its ``img``: browsers keep the
    variants pointing at the CDN; the img alone is what the sidecar
    resolution works from."""
    for pic in (p for part in parts for p in part.select("picture")):
        img = pic.first("img")
        if img is not None:
            pic.replace_with(img)
        else:
            pic.remove()


def _formula(extraction: Extraction, span: Node) -> Formula:
    """The formula a ``span.mathjax-tex`` holds, in whichever state
    the page was saved.

    Once MathJax has rendered it, the span holds the MathML MathJax
    renders for assistive technology, and, when the save kept the
    page's scripts, the TeX MathJax moved into a script. The MathML
    is preferred over texmath's conversion of the TeX, which fails
    on some of the platform's macros (``\\mathscr``, ``\\bf``).
    """
    math = span.first("span.MJX_Assistive_MathML > math")
    script = span.first('script[type^="math/tex"]')
    if math is None and script is None:
        # unrendered: the TeX in its math delimiters
        return extraction.delimited_formula(span)
    if math is not None:
        # MathJax's TeX-atom classes
        math.strip_attributes("class")
    return extraction.formula(
        span, mathml=math,
        tex=script.text().strip() if script is not None else None,
        display=(script.matches("[type*='mode=display']")
                 if script is not None
                 else span.has("span.MJX_Assistive_MathML_Block")))


def _self_link_target(href: str) -> Target | None:
    """The in-document id a Springer cross-reference href points at.

    The link carries the whole article or chapter path, the target a
    fragment on it (nature.com ``/articles/<id>#Fig2``,
    link.springer.com ``/article/<doi>#ref-CR4``).
    """
    if m := re.match(r"/(?:articles?|chapter)/[^#]*#(.+)$", href):
        return target(m.group(1))
    return None


def _link_targets(a: Node) -> list[Target]:
    """A whole-path cross-reference names its target in a fragment on
    the path (see :func:`_self_link_target`); a fragment link names it
    directly."""
    path_target = _self_link_target(a.attr("href") or "")
    return [path_target] if path_target is not None else fragment_targets(a)


class SpringerSource(Source):
    key = "springer"
    name = "Springer Nature"
    # MathJax's MathML, with the TeX when the save kept the scripts
    # (see _formula)
    math_formats: ClassVar[frozenset[str]] = frozenset({MATHML, TEX})

    def matches(self, html_text: str) -> bool:
        return "c-article-body" in html_text

    def extract(self, page: SavedPage, *,
                renderers: tuple[MathRenderer, MathRenderer],
                best_effort: bool = False) -> Content:
        """Extract the document from saved Springer Nature article HTML."""
        extraction = Extraction(page, renderers, best_effort=best_effort)
        body = extraction.root.first("div.c-article-body")
        if body is None:
            raise SourceNotAvailableError(
                "no div.c-article-body: not a Springer Nature article page")

        abstract, sections = _content_sections(body)
        if not sections:
            raise SourceNotAvailableError(
                "no content sections under c-article-body")

        parts = ([abstract] if abstract is not None else []) + sections
        # u-hide-print marks the platform's screen-only chrome (figure
        # buttons, per-reference link rows, citation downloads); the
        # print rendition omits it, so the book does too. nature.com's
        # figure buttons lack the marker and need their own selector.
        extraction.drop(parts, ".u-hide-print, svg.u-icon, "
                               "div.c-article-section__figure-link")
        _unwrap_pictures(parts)
        extraction.render_math(
            _formula(extraction, sp) for part in parts
            for sp in part.select("span.mathjax-tex"))
        extraction.resolve_links(parts, _link_targets)
        extraction.embed_figures(parts)

        if abstract is not None:
            extraction.add_abstract(abstract)
        # the bibliography and the footnotes are among the platform's
        # sections
        for s in sections:
            if s.matches("[data-title='References']"):
                extraction.add_bibliography(
                    s, s.select("li.c-article-references__item"))
            elif s.matches("[data-title='Notes']"):
                extraction.add_footnotes(
                    s, s.select("li.c-article-footnote--listed__item"))
            else:
                extraction.add_sections([s])

        return extraction.content()
