"""Build an EPUB from a saved ScienceDirect article page.

The page's MathJax (v3) replaces the MathML of its formulas with
rendered output when it runs, so a save must keep MathJax from
running.
"""

from collections.abc import Iterable
from typing import ClassVar

from papis_make_epub.formula import MATHML, Formula
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
from papis_make_epub.sources.node import Node, Target, target

# below this many words the capture is the hydration shell
MIN_ARTICLE_WORDS = 500


def _formula(extraction: Extraction, mml: Node) -> Formula:
    """A Formula from a ``math`` element."""
    # the hydration framework stamps is=true on every MathML element;
    # the attribute is not MathML
    mml.strip_attributes("is")
    wrapper = mml.parent("span.math")
    node = wrapper if wrapper is not None else mml
    # ScienceDirect marks display position structurally: display
    # equations sit in their own block, inline ones inside a <p>
    display = node.parent("p") is None or mml.has("mtable")
    return extraction.formula(node, mathml=mml, display=display)


def _unlink_back_references(parts: Iterable[Node]) -> None:
    """Take the href off the reference entries' back-links.

    They target a citation ``name`` rather than an id and never
    resolve; the elements stay, being the citations' targets.
    """
    for part in parts:
        for a in part.select('a[href^="#bbr"]'):
            a.delete("href")


def _link_targets(a: Node) -> list[Target]:
    """Body citations point at ``#brNNNN``, but the reference entries
    are id'd ``ref-id-brNNNN``: the link is a side-panel opener in the
    app."""
    return [t for f in fragment_targets(a) for t in (f, target(f"ref-id-{f}"))]


class ScienceDirectSource(Source):
    key = "sciencedirect"
    name = "ScienceDirect"
    # MathML; no TeX exists
    math_formats: ClassVar[frozenset[str]] = frozenset({MATHML})

    def matches(self, html_text: str) -> bool:
        # Elsevier's design-system footer class; a marker only its own
        # markup carries (the platform name and article URLs also
        # appear in pages that cite ScienceDirect papers)
        return "els-footer" in html_text

    def extract(self, page: SavedPage, *,
                renderers: tuple[MathRenderer, MathRenderer],
                best_effort: bool = False) -> Content:
        """Extract the document from a capture of ScienceDirect's
        rendered page."""
        extraction = Extraction(page, renderers, best_effort=best_effort)
        root = extraction.root
        body = root.first("#body")
        if body is None or word_count(body) < MIN_ARTICLE_WORDS:
            raise SourceNotAvailableError(
                "no article text: the capture holds ScienceDirect's page "
                "shell, not the rendered article; save the page once it "
                "has rendered (see the README)")

        abstract = root.first("#abstracts")
        bibliography = root.first("section.bibliography")
        sections = outermost(body.select("section"))
        if not sections:
            raise SourceNotAvailableError("no sections under #body")

        parts = [p for p in (abstract, body, bibliography) if p is not None]
        # reader chrome: per-figure download-link pairs, per-reference
        # Scopus/Google Scholar/View PDF rows, and the inline "opens in
        # new window" link icons (svg.icon)
        extraction.drop(parts, "div.ReferenceLinks, ol:has(a.download-link), "
                               "svg.icon")
        extraction.render_math(
            _formula(extraction, mml) for part in parts
            for mml in part.select("span.math > math"))

        _unlink_back_references(parts)
        extraction.resolve_links(parts, _link_targets)
        extraction.embed_figures(parts)

        if abstract is not None:
            extraction.add_abstract(abstract)
        extraction.add_sections(sections)
        if bibliography is not None:
            extraction.add_sections([bibliography], "bibliography")

        return extraction.content()
