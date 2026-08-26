"""Build an EPUB from a saved ACM Digital Library full-text page
(``/doi/full/``).
"""

import re
from typing import ClassVar

from papis_make_epub.formula import MATHML, Formula
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
        soup = extraction.soup
        # tight scoping: the abstracts div also holds tab chrome and an
        # AI-summary widget; the backmatter also holds ACM_widgets
        # (Cited By, Recommendations)
        abstract = (soup.select_one("div#abstracts section#abstract")
                    or soup.select_one("div#abstracts "
                                       "section#summary-abstract")
                    or soup.select_one("div#abstracts"))
        body = soup.select_one("section#bodymatter")
        back = (soup.select_one("#backmatter section#bibliography")
                or soup.select_one("#backmatter"))
        # the footnotes sit in a sibling container of the bibliography,
        # so the bibliography selection above misses them
        footnotes = soup.select_one("#backmatter #footnotes")
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
        for el in (el for part in parts for el in part.find_all(True)):
            for attr in [a for a in el.attrs if a.startswith("data-")]:
                del el[attr]
            # role="paragraph" is WAI-ARIA 1.2, which EPUB 3.3
            # predates; the div renders the same without it
            if el.get("role") == "paragraph":
                del el["role"]
        extraction.render_math(
            Formula(node=m, mathml=m,
                    display=(m.get("display") == "block"
                             or m.find("mtable") is not None))
            for part in parts for m in part.find_all("math"))
        # the citations and footnote marks are fragment links to ids
        # the served page carries on every entry and note
        extraction.resolve_links(parts)

        extraction.embed_figures(parts)

        if abstract is not None:
            extraction.add_abstract(abstract)

        sections = outermost(
            body.find_all("section", id=re.compile(r"^sec-\d+$")))
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
