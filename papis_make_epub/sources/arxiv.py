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

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

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

ARXIV_HTML_BASE = "https://arxiv.org/html/"
# fallback for papers predating arXiv's native HTML (same LaTeXML output)
AR5IV_HTML_BASE = "https://ar5iv.labs.arxiv.org/html/"
# below this many words a conversion is considered silently truncated
MIN_WORDS = 1500

# LaTeXML section classes that become the document's top-level parts
PART_CLASSES = {"ltx_section", "ltx_appendix", "ltx_bibliography"}


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
    soup = BeautifulSoup(html, "lxml")
    article = soup.find("article") or soup.find(class_="ltx_page_main")
    if not isinstance(article, Tag):
        raise SourceNotAvailableError("no <article> in HTML")
    page = SavedPage(html)
    page.save(Path(dest_dir) / name, page_url, root=article)
    return page


def _classes(tag: Tag) -> set[str]:
    return set(tag.attrs.get("class") or [])


def _drop_conversion_errors(extraction: Extraction, article: Tag) -> None:
    """Drop what LaTeXML emits for macros it could not expand.

    A missing-label span keeps only the text after its colon.
    ``ltx_ERROR`` marks an unexpanded macro (e.g. an unfilled
    ``\\Copyright`` in a template): the marker goes, then the
    paragraphs its removal left empty.
    """
    for s in article.select(".ltx_missing_label"):
        s.replace_with(NavigableString(s.get_text().rsplit(":", 1)[-1]))
    extraction.drop([article], ".ltx_ERROR")
    for p in article.select("div.ltx_para, p.ltx_p"):
        if (not p.get_text(strip=True)
                and not p.find(["img", "object", "figure", "table"])):
            p.decompose()


def _restore_list_markers(extraction: Extraction, article: Tag) -> None:
    """Restore the native list markers.

    LaTeXML suppresses them and injects its own bullet or number as a
    span, which renders on a line of its own.
    """
    extraction.drop([article], "li.ltx_item span.ltx_tag")
    for li in article.select("li.ltx_item"):
        if li.get("style"):
            style = re.sub(r"list-style-type\s*:\s*none;?", "",
                           str(li["style"])).strip()
            if style:
                li["style"] = style
            else:
                del li["style"]


def _part_role(section: Tag) -> str | None:
    """The ``epub:type`` of a top-level LaTeXML section, if typed."""
    classes = _classes(section)
    if "ltx_bibliography" in classes:
        return "bibliography"
    if "ltx_appendix" in classes:
        return "appendix"
    return None


def _front_matter(article: Tag) -> tuple[Tag, list[TocEntry]]:
    """The article's front matter, with the abstract's TOC entry: a
    copy of the article with the part sections removed."""
    front = BeautifulSoup(str(article), "lxml").find("article")
    if not isinstance(front, Tag):  # str(article) round-trips a Tag
        raise SourceNotAvailableError("article lost in front-matter copy")
    for s in front.find_all("section"):
        # a section nested in one already removed went with it, and
        # a decomposed tag has no attributes left to read
        if not s.decomposed and PART_CLASSES & _classes(s):
            s.decompose()
    abstract = front.find(class_="ltx_abstract")
    entries = ([TocEntry("Abstract", str(abstract["id"]))]
               if isinstance(abstract, Tag) and abstract.get("id") else [])
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
        soup = extraction.soup
        article = soup.find("article") or soup.find(class_="ltx_page_main")
        if not isinstance(article, Tag):
            raise SourceNotAvailableError("no <article> in HTML")

        # a[download] is LaTeXML's per-listing "download" icon, a
        # data:text/plain link to the code that the listing also shows
        # inline; the data: URL is invalid in an EPUB href
        extraction.drop([article], "a[download]")

        def formula(m: Tag) -> Formula:
            # only declared-TeX annotations are read as TeX: the MathML
            # spec allows any payload under any encoding label
            ann = m.find("annotation", encoding="application/x-tex")
            # an empty annotation is no annotation
            tex = (ann.get_text(strip=True) or None) if ann else None
            return Formula(node=m, mathml=m, tex=tex,
                           display=m.get("display") == "block")

        extraction.render_math(formula(m) for m in article.find_all("math"))
        _drop_conversion_errors(extraction, article)
        extraction.resolve_links([article])
        _restore_list_markers(extraction, article)
        extraction.embed_figures([article])

        nodes = outermost([s for s in article.find_all("section")
                           if PART_CLASSES & _classes(s)])

        front, front_entries = _front_matter(article)
        extraction.add_part(front, "titlepage", front_entries)
        extraction.add_sections(nodes, _part_role)

        return extraction.content()
