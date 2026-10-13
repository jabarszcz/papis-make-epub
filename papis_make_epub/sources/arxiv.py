"""Build an EPUB from arXiv's LaTeXML-generated HTML.

The page is the "HTML (experimental)" rendition arXiv generates from
the submitted LaTeX sources; for older papers, the fetch falls back
to ar5iv's.
"""

import re
from collections.abc import Mapping
from http import HTTPStatus
from pathlib import Path
from typing import Any, ClassVar

from papis_make_epub.fetch import FetchError, polite_get
from papis_make_epub.formula import MATHML, TEX, Formula
from papis_make_epub.math_renderers import MathRenderer
from papis_make_epub.saved_page import SavedPage
from papis_make_epub.sources.extraction import (
    Content,
    Extraction,
    Source,
    SourceNotAvailableError,
    TocEntry,
    outermost,
    page_words,
)
from papis_make_epub.sources.node import Node

ARXIV_HTML_BASE = "https://arxiv.org/html/"
# fallback for papers predating arXiv's native HTML (same LaTeXML output)
AR5IV_HTML_BASE = "https://ar5iv.labs.arxiv.org/html/"
# below this many words a conversion is considered silently truncated
MIN_WORDS = 1500

# LaTeXML's article element
ARTICLE = "article"
# the LaTeXML sections that become the document's top-level parts
PARTS = "section.ltx_section, section.ltx_appendix, section.ltx_bibliography"


def arxiv_id_from_doc(doc: Mapping[str, Any]) -> str | None:
    """Extract an arXiv identifier from a papis document's metadata."""
    eprint = doc.get("eprint")
    if eprint and doc.get("eprinttype", "arxiv") == "arxiv":
        return str(eprint)

    for key in ("url", "doc_url"):
        # old-style identifiers contain a slash (math/0211159)
        m = re.search(r"arxiv\.org/(?:abs|pdf|html)/([^\s?#]+?)/?(?:\.pdf)?$",
                      str(doc.get(key) or ""))
        if m:
            return m.group(1)

    return None


def _validate_html(text: str, url: str) -> None:
    if "ltx_" not in text:
        raise SourceNotAvailableError(f"{url} is not LaTeXML output")
    # ar5iv serves failed conversions with HTTP 200 and a marker
    if "had a Fatal error" in text:
        raise SourceNotAvailableError(f"{url}: conversion marked fatal")
    words = page_words(text)
    if words < MIN_WORDS:
        raise SourceNotAvailableError(
            f"{url}: conversion truncated ({words} words)")


def fetch_html(arxiv_id: str) -> tuple[str, str]:
    """Fetch LaTeXML HTML for *arxiv_id*, trying arXiv then ar5iv.

    Returns the HTML and the page URL it came from (the base for
    resolving relative asset links). A page that is not there (404)
    or not a usable conversion means no rendition at that host and
    the next is tried; any other failed request (the server refusing
    the client, a network failure) propagates as FetchError, since
    the rendition may well exist.
    """
    errors = []
    for base in (ARXIV_HTML_BASE, AR5IV_HTML_BASE):
        url = base + arxiv_id
        try:
            text = polite_get(url, page=True).decode("utf-8", "replace")
            _validate_html(text, url)
            return text, url
        except SourceNotAvailableError as exc:
            errors.append(str(exc))
        except FetchError as exc:
            if exc.status != HTTPStatus.NOT_FOUND:
                raise
            errors.append(str(exc))
    raise SourceNotAvailableError("; ".join(errors))


def fetch_page(arxiv_id: str, dest_dir: str,
               name: str = "page.html") -> SavedPage:
    """Fetch the LaTeXML page and its figures into *dest_dir*.

    The returned page is file-backed: the html at *dest_dir*/*name*,
    the figures in its sidecar dir (see
    :meth:`papis_make_epub.saved_page.SavedPage.save`).
    """
    html, page_url = fetch_html(arxiv_id)
    page = SavedPage(html)
    try:
        # the figures of the article alone
        page.save(Path(dest_dir) / name, page_url, scope=(ARTICLE,))
    except LookupError:
        raise SourceNotAvailableError("no <article> in HTML") from None
    return page


def _drop_conversion_errors(extraction: Extraction, article: Node) -> None:
    """Drop what LaTeXML emits for macros it could not expand.

    A missing-label span keeps only the text after its colon.
    ``ltx_ERROR`` marks an unexpanded macro (e.g. an unfilled
    ``\\Copyright`` in a template), and the marker goes.
    """
    for s in article.select(".ltx_missing_label"):
        s.replace_with(s.text().rsplit(":", 1)[-1])
    extraction.drop([article], ".ltx_ERROR")


def _restore_list_markers(extraction: Extraction, article: Node) -> None:
    """Restore the native list markers.

    LaTeXML suppresses them and injects its own bullet or number as a
    span, which renders on a line of its own.
    """
    extraction.drop([article], "li.ltx_item span.ltx_tag")
    for li in article.select("li.ltx_item[style]"):
        style = re.sub(r"list-style-type\s*:\s*none;?", "",
                       li.attr("style") or "").strip()
        if style:
            li.set("style", style)
        else:
            li.delete("style")


# the LaTeXML note roles read as footnotes: \footnote, and the title
# block's \thanks and \footnotetext
FOOTNOTE_ROLES = ("ltx_role_footnote", "ltx_role_thanks",
                  "ltx_role_footnotetext")


def _collect_footnotes(extraction: Extraction,
                       article: Node) -> tuple[Node, list[Node]]:
    """Move the article's footnotes into a footnotes section after it;
    the section and the footnotes.

    LaTeXML leaves each note inline at its mark, one span holding the
    mark and, beside it, the note text, which repeats the mark; no
    link joins them. The mark stays, as a link to the footnote; the
    note text moves, under LaTeXML's number label (or the repeated
    mark, for the title-block notes, which have no number) and
    without the ``thanks:``/``footnotetext:`` type label. A footnote
    without an id gets one. A ``\\footnotetext`` stands apart from any
    mark, so its place is left empty; an empty ``\\footnotemark`` (a
    repeat of another note's mark) stays as its plain mark.
    """
    section = extraction.new("section")
    section.append(extraction.new("h2", text="Footnotes"))
    footnotes = []
    for span in article.select(", ".join(f"span.ltx_note.{r}"
                                         for r in FOOTNOTE_ROLES)):
        marks = span.children("sup.ltx_note_mark")
        content = span.first(".ltx_note_content")
        if content is None:
            continue
        for el in content.select(".ltx_note_type"):
            el.remove()
        if content.has(".ltx_tag_note"):
            for el in content.select("sup.ltx_note_mark"):
                el.remove()
        footnote_id = span.attr("id") or extraction.mint_id("footnote")
        footnote = extraction.new("div", {"class": "ltx_note_content",
                                          "id": footnote_id})
        footnote.adopt_children(content)
        footnotes.append(footnote)
        section.append(footnote)
        if span.matches(".ltx_role_footnotetext") or not marks:
            span.remove()
            continue
        link = extraction.new("a", {"href": "#" + footnote_id})
        link.append(marks[0])
        span.replace_with(link)
    for span in article.select("span.ltx_note.ltx_role_footnotemark"):
        marks = span.children("sup.ltx_note_mark")
        if marks:
            span.replace_with(marks[0])
        else:
            span.remove()
    extraction.place(section)
    return section, footnotes


def _front_matter(article: Node) -> tuple[Node, list[TocEntry]]:
    """The article's front matter, with the abstract's TOC entry: a
    copy of the article with the part sections removed."""
    front = article.copy()
    for s in front.select(PARTS):
        # a section nested in one already removed went with it
        if not s.is_removed:
            s.remove()
    abstract = front.first(".ltx_abstract")
    abstract_id = abstract.attr("id") if abstract is not None else None
    entries = [TocEntry("Abstract", abstract_id)] if abstract_id else []
    return front, entries


class ArxivSource(Source):
    key = "arxiv"
    name = "arXiv LaTeXML"
    # presentation MathML with TeX annotations
    math_formats: ClassVar[frozenset[str]] = frozenset({MATHML, TEX})

    def matches(self, html_text: str) -> bool:
        return "ltx_page_main" in html_text

    def extract(self, page: SavedPage, *,
                renderers: tuple[MathRenderer, MathRenderer],
                best_effort: bool = False) -> Content:
        """Extract the document from arXiv's LaTeXML HTML.

        Figures resolve from the page's files like the other
        saved-page sources; for a fetch, :func:`fetch_page`
        materializes the page first. The page's own front matter
        (title block, affiliations, abstract) is the title page.
        """
        extraction = Extraction(page, renderers, best_effort=best_effort)
        article = extraction.root.first(ARTICLE)
        if article is None:
            raise SourceNotAvailableError("no <article> in HTML")

        # a[download] is LaTeXML's per-listing "download" icon, a
        # data:text/plain link to the code that the listing also shows
        # inline; the data: URL is invalid in an EPUB href
        extraction.drop([article], "a[download]")

        def formula(m: Node) -> Formula:
            # only declared-TeX annotations are read as TeX: the MathML
            # spec allows any payload under any encoding label
            ann = m.first("annotation[encoding='application/x-tex']")
            # an empty annotation is no annotation
            tex = (ann.text(strip=True) or None) if ann is not None else None
            return extraction.formula(m, mathml=m, tex=tex,
                                      display=m.matches("[display='block']"))

        extraction.render_math(formula(m) for m in article.select("math"))
        _drop_conversion_errors(extraction, article)
        footnotes_section, footnotes = _collect_footnotes(extraction, article)
        extraction.resolve_links([article, footnotes_section])
        _restore_list_markers(extraction, article)
        extraction.embed_figures([article, footnotes_section])

        nodes = outermost(article.select(PARTS))

        front, front_entries = _front_matter(article)
        extraction.add_part(front, "titlepage", front_entries)
        for node in nodes:
            if node.matches(".ltx_bibliography"):
                extraction.add_bibliography(node, node.select("li.ltx_bibitem"))
            elif node.matches(".ltx_appendix"):
                extraction.add_sections([node], "appendix")
            else:
                extraction.add_sections([node])
        if footnotes:
            extraction.add_footnotes(footnotes_section, footnotes)

        return extraction.content()
