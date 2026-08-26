"""Build an EPUB from a saved Springer Nature article page.

Covers link.springer.com and nature.com, which share the platform
markup. The math is TeX in ``span.mathjax-tex``, which the page's
MathJax (v2) renders.
"""

import re
from typing import ClassVar

from bs4 import Tag

from papis_make_epub.formula import TEX, Formula
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

# platform boilerplate sections dropped from the book (content
# sections carry a data-title and no class; chrome sections carry
# classes or these titles)
_CHROME_TITLES = {
    "About this article", "About this paper", "Author information",
    "Comments", "Copyright information", "Editor information",
    "Publish with us", "Rights and permissions",
}


def _formula(span: Tag) -> Formula:
    """A Formula from a ``span.mathjax-tex``: ``\\(..\\)`` is inline,
    ``$$..$$`` and ``\\[..\\]`` are display."""
    raw = span.get_text().strip()
    display = False
    tex = raw
    for opening, closing, disp in (("\\(", "\\)", False), ("$$", "$$", True),
                                   ("\\[", "\\]", True)):
        if raw.startswith(opening) and raw.endswith(closing):
            tex = raw[len(opening):-len(closing)].strip()
            display = disp
            break
    return Formula(node=span, tex=tex, display=display)


def _self_link_target(href: str) -> str | None:
    """The in-document id a Springer cross-reference href points at.

    The link carries the whole article or chapter path. Journal
    figures, tables, and references are a fragment on that path
    (nature.com ``/articles/<id>#Fig2``, link.springer.com
    ``/article/<doi>#ref-CR4``). Book chapters instead number figures
    and tables in the path itself (``/chapter/<doi>/figures/2``), whose
    target id is ``Fig2``.
    """
    if m := re.match(r"/(?:articles?|chapter)/[^#]*#(.+)$", href):
        return m.group(1)
    if m := re.match(r"/chapter/[^#]*/(figures|tables)/(\d+)$", href):
        return ("Fig" if m.group(1) == "figures" else "Tab") + m.group(2)
    return None


def _link_targets(a: Tag) -> list[str]:
    """A whole-path cross-reference names its target in the path (see
    :func:`_self_link_target`); a fragment link names it directly."""
    target = _self_link_target(str(a.get("href", "")))
    return [target] if target is not None else fragment_targets(a)


class SpringerSource(Source):
    key = "springer"
    name = "Springer Nature"
    # TeX only; no MathML exists
    math_formats: ClassVar[frozenset[str]] = frozenset({TEX})

    def matches(self, html_text: str) -> bool:
        return "c-article-body" in html_text

    def extract(self, page: SavedPage, *,
                renderers: tuple[MathRenderer, MathRenderer],
                best_effort: bool = False) -> Content:
        """Extract the document from saved Springer Nature article HTML."""
        extraction = Extraction(page, renderers, best_effort=best_effort)
        body = extraction.soup.select_one("div.c-article-body")
        if body is None:
            raise SourceNotAvailableError(
                "no div.c-article-body: not a Springer Nature article page")

        sections = []
        abstract = None
        for s in outermost(body.find_all("section")):
            stitle = s.get("data-title")
            if s.get("class") or stitle is None or stitle in _CHROME_TITLES:
                continue
            if stitle == "Abstract":
                abstract = s
            else:
                sections.append(s)
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
        # browsers keep the <picture> variants pointing at the CDN; the
        # img alone is what the sidecar resolution works from
        for pic in (p for part in parts for p in part.find_all("picture")):
            img = pic.find("img")
            pic.replace_with(img if img is not None else "")
        extraction.render_math(
            _formula(sp) for part in parts
            for sp in part.select("span.mathjax-tex"))
        extraction.resolve_links(parts, _link_targets)
        extraction.embed_figures(parts)

        if abstract is not None:
            extraction.add_abstract(abstract)
        # the bibliography is one of the platform's sections
        extraction.add_sections(
            sections,
            lambda s: ("bibliography" if s.get("data-title") == "References"
                       else None))

        return extraction.content()
