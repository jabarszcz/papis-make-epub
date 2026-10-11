"""Build an EPUB from a saved ACM Digital Library full-text page
(``/doi/full/``).
"""

from typing import ClassVar

from papis_make_epub.formula import MATHML
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

# below this many words a bodymatter is the "Formats available"
# placeholder of a paper without full-text HTML
MIN_BODY_WORDS = 500


class AcmSource(Source):
    key = "acm"
    name = "ACM"
    # ACM MathML carries no TeX
    math_formats: ClassVar[frozenset[str]] = frozenset({MATHML})

    def matches(self, html_text: str) -> bool:
        # capture tools may serialize the attribute unquoted
        return (('id="bodymatter"' in html_text
                 or "id=bodymatter" in html_text)
                and "dl.acm.org" in html_text)

    def extract(self, page: SavedPage, *,
                renderers: tuple[MathRenderer, MathRenderer],
                best_effort: bool = False) -> Content:
        """Extract the document from saved ACM full-text HTML.

        Display math is detected as ``display="block"`` or
        mtable-bearing (ACM typesets inference rules as tables inside
        inline math).
        """
        extraction = Extraction(page, renderers, best_effort=best_effort)
        root = extraction.root
        # tight scoping: the abstracts div also holds tab chrome and an
        # AI-summary widget; the backmatter also holds ACM_widgets
        # (Cited By, Recommendations)
        abstract = root.first("div#abstracts section#abstract",
                              "div#abstracts section#summary-abstract",
                              "div#abstracts")
        body = root.first("section#bodymatter")
        back = root.first("#backmatter section#bibliography", "#backmatter")
        # the footnotes sit in a sibling container of the bibliography,
        # so the bibliography selection above misses them
        footnotes = root.first("#backmatter #footnotes")
        if body is None:
            raise SourceNotAvailableError(
                "no #bodymatter: not ACM full-text HTML")
        # papers without full-text HTML still have a bodymatter, holding
        # only a "Formats available: PDF/eReader" placeholder
        if word_count(body) < MIN_BODY_WORDS:
            raise SourceNotAvailableError(
                "page has no full-text HTML (bodymatter is a formats "
                "placeholder); only PDF/eReader renditions exist for "
                "this paper")

        parts = [p for p in (abstract, body, back, footnotes)
                 if p is not None]
        # div.external-links is the per-reference link row (Google
        # Scholar, Digital Library, Crossref): reader chrome
        extraction.drop(parts, "div.external-links")
        for part in parts:
            part.strip_attributes(r"data-.*")
            # role="paragraph" is WAI-ARIA 1.2, which EPUB 3.3
            # predates; the div renders the same without it
            for el in part.select("[role='paragraph']"):
                el.delete("role")
        extraction.render_math(
            extraction.formula(m, mathml=m,
                               display=(m.matches("[display='block']")
                                        or m.has("mtable")))
            for part in parts for m in part.select("math"))
        # the citations and footnote marks are fragment links to ids
        # the served page carries on every entry and note
        extraction.resolve_links(parts)

        extraction.embed_figures(parts)

        if abstract is not None:
            extraction.add_abstract(abstract)

        sections = outermost(body.select("section[id^='sec-']"))
        if not sections:
            raise SourceNotAvailableError(
                "no sec-N sections under #bodymatter")
        extraction.add_sections(sections)
        if back is not None:
            extraction.add_sections([back], "bibliography")

        # the footnote content the markers link to, kept after the
        # bibliography so the #fn targets resolve
        if footnotes is not None:
            extraction.add_part(footnotes)

        return extraction.content()
