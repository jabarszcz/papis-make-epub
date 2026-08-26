"""EPUB assembly: the writer side, uniform across sources.

A source's :class:`~papis_make_epub.sources.extraction.Content` becomes
the book through :func:`write_epub`: metadata, media, the single
content document with its navigation, character normalization, the
stylesheet, and symbol-font coverage.
"""

import importlib.resources
import io
import logging
import mimetypes
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import matplotlib
from ebooklib import epub
from fontTools import subset
from fontTools.ttLib import TTFont

from papis_make_epub.logging import get_logger
from papis_make_epub.sources.extraction import Content, Part, TocEntry

logger: logging.Logger = get_logger(__name__)


@dataclass(frozen=True)
class DocMeta:
    """The document identity that goes into the EPUB.

    The caller projects it from its own record (the cli from the
    papis document): title and authors become the book metadata (and
    the title page, through ``Content.add_title_page``), the
    identifier its ``dc:identifier``.
    """

    title: str
    authors: Sequence[str]
    identifier: str


@dataclass(frozen=True)
class BuildOptions:
    """The user's build choices, one bundle through the whole chain."""

    # renderers by name; the pass-through is the default: every
    # other renderer trades away something (text flattens layout,
    # images fix the font and size), and the trade is the user's to
    # choose
    math_inline: str = "mathml"
    math_display: str = "mathml"
    # added after the bundled stylesheet and the fallback-font rules,
    # so its rules win on conflicts
    css: bytes | None = None
    normalize_chars: bool = True
    best_effort: bool = False


CSS = (importlib.resources.files("papis_make_epub") / "style.css").read_bytes()


def _new_book(meta: DocMeta, language: str | None) -> epub.EpubBook:
    book = epub.EpubBook()
    book.set_identifier(meta.identifier)
    book.set_title(meta.title)
    if language is not None:
        book.set_language(language)
    for a in meta.authors:
        # a bare dc:creator, no id: add_author would id each creator to
        # hang role or file-as refinements, which we do not add, and
        # its default id collides across authors
        book.add_metadata("DC", "creator", a)
    return book


def _add_media(book: epub.EpubBook,
               media: Iterable[tuple[str, bytes]]) -> None:
    """Add (file name, bytes) media items; the type follows the name."""
    for fn, data in media:
        mt = mimetypes.guess_type(fn)[0] or "application/octet-stream"
        book.add_item(epub.EpubItem(file_name=fn, media_type=mt,
                                    content=data))


# CJK/fullwidth codepoints found in place of the standard math
# characters in some of the sampled saved ACM pages (mostly inside
# their MathML; the few prose occurrences were all math notation).
# The standard forms are what text faces and the embedded fallback
# fonts cover (U+2329/232A are themselves deprecated in favor of
# U+27E8/27E9); these surfaced in font-coverage analysis as the
# uncovered residue after the fallback-font chain, wrong codepoints
# rather than missing glyphs. Applied to the whole document so math
# notation typeset as prose is covered too; that assumes the document
# has no legitimate CJK text, and --no-char-normalization turns the
# table off.
CHAR_NORMALIZE = str.maketrans({
    "〚": "⟦",  # white square bracket -> math white bracket
    "〛": "⟧",
    "〈": "⟨",  # deprecated angle bracket -> math angle bracket
    "〉": "⟩",
    "〈": "⟨",  # CJK angle bracket -> math angle bracket
    "〉": "⟩",
    "・": "·",  # katakana middle dot -> middle dot
    "￢": "¬",  # fullwidth not sign -> not sign
})


def _assemble_document(css: epub.EpubItem, language: str | None,
                       title: str, parts: Iterable[Part], *,
                       normalize_chars: bool = True) -> epub.EpubHtml:
    """One content document from the ordered parts, linked to *css*;
    the caller adds it to the book.

    The whole article is a single XHTML document; navigation comes
    from the table of contents pointing at fragments (_build_toc). A
    typed part is wrapped in a ``section`` carrying its ``epub:type``
    (titlepage, abstract, bibliography, appendix...) and an id
    landmarks can target. A repeated type (several appendices) keeps
    ``part-<type>`` on the first, so the landmark target stays
    stable, and suffixes the rest.
    """
    pieces = []
    type_counts: dict[str, int] = {}
    for p in parts:
        if not p.epub_type:
            pieces.append(p.html)
            continue
        n = type_counts.get(p.epub_type, 0)
        type_counts[p.epub_type] = n + 1
        pid = f"part-{p.epub_type}" if n == 0 else f"part-{p.epub_type}-{n}"
        pieces.append(
            f'<section epub:type="{p.epub_type}" id="{pid}">{p.html}</section>')
    inner = "".join(pieces)
    if normalize_chars:
        inner = inner.translate(CHAR_NORMALIZE)
    doc = epub.EpubHtml(file_name="content.xhtml", title=title,
                        lang=language)
    doc.content = (
        '<html xmlns:epub="http://www.idpf.org/2007/ops">'
        "<head><link rel='stylesheet' href='style.css'/></head>"
        f"<body>{inner}</body></html>")
    if "<math" in inner:  # EPUB3 manifest property for MathML content
        doc.properties.append("mathml")
    if ".svg" in inner:
        doc.properties.append("svg")
    doc.add_item(css)
    return doc


def _build_toc(book: epub.EpubBook, doc: epub.EpubHtml,
               parts: Sequence[Part]) -> None:
    """TOC and landmarks from the parts' declared entries.

    Landmarks: the body starts at the first entry; a
    bibliography-typed part is named when present.
    """
    fname = doc.file_name

    def link(e: TocEntry) -> Any:
        lnk = epub.Link(f"{fname}#{e.id}", e.title, e.id)
        # a childless entry stays a plain link: (link, []) confuses
        # some readers
        return (lnk, [link(c) for c in e.children]) if e.children else lnk

    entries = [e for p in parts for e in p.entries]
    book.toc = [link(e) for e in entries]

    guide = []
    if entries:
        guide.append({"type": "bodymatter", "title": "Start",
                      "href": f"{fname}#{entries[0].id}"})
    if any(p.epub_type == "bibliography" for p in parts):
        guide.append({"type": "bibliography", "title": "References",
                      "href": f"{fname}#part-bibliography"})
    book.guide = guide


# fallback chain: STIX first (a serif math face that harmonizes with
# the body text and covers the math alphanumerics), DejaVu Sans for
# the symbols STIX lacks (dingbats, space variants)
_FALLBACK_FONTS = [("STIXGeneral.ttf", "stix-fallback", "fonts/stix.ttf"),
                   ("DejaVuSans.ttf", "dejavu-fallback", "fonts/dejavu.ttf")]


def _embed_symbol_font(book: epub.EpubBook, css_item: epub.EpubItem,
                       doc: epub.EpubHtml) -> bool:
    """Embed fallback fonts covering the document's uncommon characters.

    The build drops the source's stylesheets and webfonts, so glyph
    coverage falls to the device, whose fonts often lack math glyphs
    (a Kobo shipped no U+2200). Readers fall back per missing glyph
    through the font-family list, so appending embedded faces fixes
    tofu without changing the text face. Each font in the chain is
    subset to the needed characters its cmap covers and the previous
    fonts do not.
    """
    chars = {c for c in doc.content if not c.isascii()}
    if not chars:
        return False

    ttf_dir = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")

    remaining = chars
    families = []
    for src_name, family, file_name in _FALLBACK_FONTS:
        if not remaining:
            break
        src = os.path.join(ttf_dir, src_name)
        take = remaining & _font_cmap(src)
        if not take:
            continue
        data = _subset_font(src, take)
        book.add_item(epub.EpubItem(file_name=file_name,
                                    media_type="font/ttf", content=data))
        css_item.content += (
            f'\n@font-face {{ font-family: "{family}";'
            f" src: url({file_name}); }}\n").encode()
        families.append(family)
        remaining = remaining - take
    if not families:
        return False

    if remaining:
        logger.warning(
            "embedded symbol fonts do not cover %d character(s): %s; "
            "they will render only if the device's own fonts have them",
            len(remaining),
            " ".join(f"{c!r}(U+{ord(c):04X})" for c in sorted(remaining)[:20]))
    chain = ", ".join(f'"{f}"' for f in families)
    css_item.content += f"body {{ font-family: serif, {chain}; }}\n".encode()
    return True


def _font_cmap(src: str) -> set[str]:
    """The characters *src* covers."""
    with TTFont(src) as font:
        return {chr(cp) for cp in font.getBestCmap()}


def _subset_font(src: str, chars: set[str]) -> bytes:
    """Subset *src* to *chars*.

    Coverage is the caller's concern (via _font_cmap): *chars* is
    expected to contain only characters the font has.
    """
    opts = subset.Options()
    buf = io.BytesIO()
    # the subsetter logs its steps at DEBUG and INFO, and a dropped
    # table at WARNING, under the host application's logging; its
    # logger is quieted for the call alone and left as it was
    ft_logger = logging.getLogger("fontTools")
    level = ft_logger.level
    ft_logger.setLevel(logging.ERROR)
    try:
        with subset.load_font(src, opts) as font:
            sub = subset.Subsetter(opts)
            sub.populate(text="".join(chars))
            sub.subset(font)
            font.save(buf)
    finally:
        ft_logger.setLevel(level)
    return buf.getvalue()


def _write_book(book: epub.EpubBook, doc: epub.EpubHtml,
                out_path: str) -> None:
    book.add_item(epub.EpubNcx())
    # the nav document feeds the reader's navigation UI; out of the
    # spine, so the book opens on the article itself
    book.add_item(epub.EpubNav())
    book.spine = [doc]
    epub.write_epub(out_path, book)


def write_epub(out_path: str, *, meta: DocMeta, content: Content,
               options: BuildOptions) -> None:
    """Write *content* as an EPUB at *out_path*.

    The whole write side, uniform across sources: book metadata,
    media, the single content document, navigation, and symbol-font
    coverage for the document's uncommon characters.
    """
    book = _new_book(meta, content.language)
    _add_media(book, content.media)
    css_item = epub.EpubItem(
        file_name="style.css", media_type="text/css", content=CSS)
    book.add_item(css_item)

    doc = _assemble_document(css_item, content.language, meta.title,
                             content.parts,
                             normalize_chars=options.normalize_chars)
    book.add_item(doc)
    _build_toc(book, doc, content.parts)
    _embed_symbol_font(book, css_item, doc)
    if options.css is not None:
        # last, so its rules win on conflicts
        css_item.content += b"\n" + options.css
    _write_book(book, doc, out_path)
