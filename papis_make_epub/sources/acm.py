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
        """Extract the document from saved ACM full-text HTML."""
        extraction = Extraction(page, renderers, best_effort=best_effort)
        root = extraction.root
        # the author's abstract has the id "abstract" on some pages,
        # "summary-abstract" on others
        abstract = root.first("div#abstracts section#abstract",
                              "div#abstracts section#summary-abstract")
        body = root.first("section#bodymatter")
        back = root.first("#backmatter section#bibliography")
        footnotes_section = root.first("#backmatter #footnotes")
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

        parts = [p for p in (abstract, body, back, footnotes_section)
                 if p is not None]
        # div.external-links is the per-reference and per-footnote link
        # row; its return link (Go to Citation, Return to text) goes,
        # replaced by the made back links, and a footnote's row is then
        # empty
        backmatter = [p for p in (back, footnotes_section) if p is not None]
        extraction.drop(backmatter, "div.to-citation__wrapper")
        extraction.drop(backmatter, "div.external-links:not(:has(a))")
        for part in parts:
            part.strip_attributes(r"data-.*")
            # role="paragraph" is WAI-ARIA 1.2, which EPUB 3.3
            # predates; the div renders the same without it
            for el in part.select("[role='paragraph']"):
                el.delete("role")
        extraction.render_math(
            extraction.formula(
                m, mathml=m,
                display=m.matches("[display='block'], [mode='display']"))
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
            extraction.add_bibliography(back, back.select("div.biblioentry"))
        if footnotes_section is not None:
            # one doc-footnote wrapper per footnote
            extraction.add_footnotes(
                footnotes_section,
                footnotes_section.select("[role='doc-footnote']"))

        return extraction.content()
