"""What the platform modules share: the source interface, the content
a source produces, and the extraction that produces it.

A source turns a saved page into :class:`Content`, the article in a
publisher-independent form: its parts as markup carrying their own
navigation, the media the markup references, the build counts, and
the language. It works through an :class:`Extraction`, which holds
the page and carries the stages every platform needs (math
rendering, figure embedding, heading indexing, part assembly) while
the source supplies what varies by platform (where the article is,
what is chrome, how formulas are delivered, which links to repair).
The source reaches the page through :class:`~papis_make_epub.sources.node.Node`
queries, which record the rules it applies. The writer
(:mod:`papis_make_epub.book`) packages the content into the EPUB and
imports the content types from here; the platform modules import
nothing from the writer.
"""

import base64
import binascii
import html
import io
import mimetypes
import os
import re
import urllib.parse
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence, Set as AbstractSet
from dataclasses import dataclass, field
from logging import Logger
from typing import ClassVar

from bs4 import BeautifulSoup, Tag
from PIL import Image

from papis_make_epub.errors import BuildError
from papis_make_epub.formula import Formula
from papis_make_epub.logging import get_logger
from papis_make_epub.math_renderers import (
    MathRenderer,
    RenderContext,
    apply_math,
    is_math_img,
)
from papis_make_epub.saved_page import SavedPage
from papis_make_epub.sources.node import (
    Node,
    Rule,
    Rules,
    Target,
    internal_module,
    tag_of,
    target,
)

logger: Logger = get_logger(__name__)

# the generic stages call no queries for the platforms, so a query
# from here is recorded at the platform module's call site
internal_module(__file__)


class SourceNotAvailableError(BuildError):
    """A document has no usable full-text HTML source."""


class FigureError(BuildError):
    """A saved page's figure could not be embedded."""


@dataclass
class BuildStats:
    """A build's summary counts: the sanity check and the checksum.

    Logging these stats serves as a quick sanity check (e.g. 0 math
    where math is expected), and the stats also give a simple
    fingerprint for testing.
    """

    # formulas in the document
    math: int = 0
    # formulas that degraded down the best-effort ladder
    math_fallback: int = 0
    # media files produced by the image renderers (0 for the
    # pass-through and text renderers; multi-line renders count
    # per image)
    math_images: int = 0
    # figures embedded, math images excluded
    images: int = 0
    # cross-references with no target in the document, left as text
    # or as placeholder anchors (a capture that left out part of the
    # page, such as a collapsed references list, is the usual cause)
    dead_links: int = 0


@dataclass
class TocEntry:
    """One TOC entry: a title pointing at a fragment id in the document."""

    title: str
    id: str
    children: list["TocEntry"] = field(default_factory=list)


@dataclass
class Part:
    """One document part: content and its own navigation.

    The source declares the part's TOC as TocEntry trees; every entry
    id must exist in the part's markup. A typed part is wrapped in a
    section carrying ``epub:type`` and the id ``part-<type>``, which
    entries and landmarks can target.
    """

    html: str
    epub_type: str | None = None
    entries: list[TocEntry] = field(default_factory=list)


@dataclass
class Content:
    """A source's whole output: the article as data, ready to write.

    *media* holds the referenced images (figures and rendered math)
    as (file name, bytes); the parts' markup already points at those
    names. A part typed ``titlepage`` is the article's own front
    matter; a source whose page carries none leaves the title page
    to the caller, through :meth:`add_title_page`. *rules* holds the
    matches of the source's rules, per rule (see
    :mod:`papis_make_epub.sources.node`).
    """

    parts: list[Part]
    media: list[tuple[str, bytes]]
    stats: BuildStats
    language: str | None
    rules: dict[Rule, int] = field(default_factory=dict)

    def add_title_page(self, title: str, authors: Iterable[str]) -> None:
        """Prepend a title page of *title* and *authors*, unless a part
        typed ``titlepage`` is already there."""
        if any(p.epub_type == "titlepage" for p in self.parts):
            return
        markup = f"<h1>{html.escape(title)}</h1>"
        if authors:
            markup += f"<p>{html.escape(', '.join(authors))}</p>"
        self.parts.insert(0, Part(markup, "titlepage"))


class Source(ABC):
    """A source platform: detection and the content extraction.

    ``matches`` accepts only markup its own platform emits, so at
    most one source matches a page; ``key`` is the ``--from``
    vocabulary. ``math_formats`` names the math formats the source
    supplies, MathML, TeX or both: a renderer consuming a format the
    source lacks is available only through the texmath binary, which
    converts between the two (the gate is
    :func:`papis_make_epub.math_renderers.make_renderer`). A platform
    module subclasses this and the package init lists the instance;
    a subclass missing either method fails there, at import.
    """

    key: ClassVar[str]
    name: ClassVar[str]
    math_formats: ClassVar[frozenset[str]]

    @abstractmethod
    def matches(self, html_text: str) -> bool:
        """Whether *html_text* is this platform's full-text markup."""

    @abstractmethod
    def extract(self, page: SavedPage, *,
                renderers: tuple[MathRenderer, MathRenderer],
                best_effort: bool = False) -> Content:
        """The page's content, its math rendered by *renderers*
        (inline, display).

        With *best_effort*, a formula the selected renderer cannot
        render falls down the renderer ladder (see
        :func:`papis_make_epub.math_renderers.apply_math`).
        """


# elements that are never article content: code, stylesheet blocks,
# navigation, embedded pages and form controls
NON_CONTENT = ("script, noscript, template, style, nav, iframe, form, "
               "button, input, select, textarea")

# WAI-ARIA's widget roles: they describe the interaction of a web
# application's controls and panels, which the book does not have
# (a tab panel without tabs is announced as one). The document
# structure and landmark roles, and the digital-publishing roles the
# platforms put on citations, footnotes and bibliographies, stay.
WIDGET_ROLES = frozenset({
    "button", "checkbox", "combobox", "dialog", "alertdialog", "grid",
    "gridcell", "listbox", "menu", "menubar", "menuitem",
    "menuitemcheckbox", "menuitemradio", "option", "progressbar",
    "radio", "radiogroup", "scrollbar", "searchbox", "slider",
    "spinbutton", "switch", "tab", "tablist", "tabpanel", "textbox",
    "toolbar", "tooltip", "tree", "treegrid", "treeitem",
})
# the widget states and properties, and HTML's focus order
WIDGET_ATTRIBUTES = ("aria-checked", "aria-controls", "aria-expanded",
                     "aria-haspopup", "aria-modal", "aria-multiselectable",
                     "aria-pressed", "aria-selected", "aria-sort",
                     "tabindex")

# the ARIA attributes whose value is a list of element ids; a token
# naming an id outside the content (a dropped tab label, a control
# panel) is a dangling reference the EPUB checker rejects
ARIA_ID_REFERENCES = ("aria-activedescendant", "aria-describedby",
                      "aria-details", "aria-errormessage", "aria-flowto",
                      "aria-labelledby", "aria-owns")

# Inline svg in XHTML needs its namespace declared, and its camelCase
# names spelled as SVG defines them; the HTML parser lowercases every
# name. The HTML standard's parser restores them on insertion ("adjust
# SVG attributes" and the SVG element name table of the "in foreign
# content" insertion mode, https://html.spec.whatwg.org/multipage/
# parsing.html, read 2026-10-05); the same tables restore them here,
# after the fact.
SVG_NS = "http://www.w3.org/2000/svg"
XHTML_NS = "http://www.w3.org/1999/xhtml"
SVG_ELEMENT_NAMES = {n.lower(): n for n in (
    "altGlyph", "altGlyphDef", "altGlyphItem", "animateColor",
    "animateMotion", "animateTransform", "clipPath", "feBlend",
    "feColorMatrix", "feComponentTransfer", "feComposite",
    "feConvolveMatrix", "feDiffuseLighting", "feDisplacementMap",
    "feDistantLight", "feDropShadow", "feFlood", "feFuncA", "feFuncB",
    "feFuncG", "feFuncR", "feGaussianBlur", "feImage", "feMerge",
    "feMergeNode", "feMorphology", "feOffset", "fePointLight",
    "feSpecularLighting", "feSpotLight", "feTile", "feTurbulence",
    "foreignObject", "glyphRef", "linearGradient", "radialGradient",
    "textPath",
)}
SVG_ATTRIBUTE_NAMES = {n.lower(): n for n in (
    "attributeName", "attributeType", "baseFrequency", "baseProfile",
    "calcMode", "clipPathUnits", "diffuseConstant", "edgeMode",
    "filterUnits", "glyphRef", "gradientTransform", "gradientUnits",
    "kernelMatrix", "kernelUnitLength", "keyPoints", "keySplines",
    "keyTimes", "lengthAdjust", "limitingConeAngle", "markerHeight",
    "markerUnits", "markerWidth", "maskContentUnits", "maskUnits",
    "numOctaves", "pathLength", "patternContentUnits",
    "patternTransform", "patternUnits", "pointsAtX", "pointsAtY",
    "pointsAtZ", "preserveAlpha", "preserveAspectRatio",
    "primitiveUnits", "refX", "refY", "repeatCount", "repeatDur",
    "requiredExtensions", "requiredFeatures", "specularConstant",
    "specularExponent", "spreadMethod", "startOffset", "stdDeviation",
    "stitchTiles", "surfaceScale", "systemLanguage", "tableValues",
    "targetX", "targetY", "textLength", "viewBox", "viewTarget",
    "xChannelSelector", "yChannelSelector", "zoomAndPan",
)}


def fragment_targets(a: Node) -> list[Target]:
    """The id a fragment link names; nothing for any other link.

    The default of :meth:`Extraction.resolve_links`; a platform's own
    function adds the forms its cross-references take.
    """
    return [target(f) for f in _fragment(tag_of(a))]


def _fragment(a: Tag) -> list[str]:
    href = str(a.get("href", ""))
    return [href[1:]] if href.startswith("#") else []


class Extraction:
    """One extraction of a saved page: the page, and the stages that
    turn the article's nodes into :class:`Content`.

    The source locates the article from :attr:`root`, the page's
    root node, and drives the stages on its nodes: the nodes' own
    ``drop`` for the platform's chrome, :meth:`render_math` for its
    formulas, :meth:`resolve_links` and :meth:`embed_figures` over
    every node that becomes a part, then the ``add_*`` methods in
    reading order, and :meth:`content` to close. The stages rewrite
    the page in place; the parts are recorded as nodes, and
    :meth:`content` runs the generic rules on them (what is never
    article content, widget markup, inline svg, ARIA references) and
    serializes them.
    """

    def __init__(self, page: SavedPage,
                 renderers: tuple[MathRenderer, MathRenderer], *,
                 best_effort: bool = False) -> None:
        self.page = page
        self._soup = BeautifulSoup(page.text, "lxml")
        self._rules = Rules()
        self.root = Node(self._soup, self._rules)
        self.stats: BuildStats = BuildStats()
        # the parts in reading order, as nodes until content()
        self._parts: list[tuple[Tag, str | None, list[TocEntry]]] = []
        # what _type_references types at content(), as recorded by
        # _refer: (referenced elements, the type and role of a link
        # into one)
        self._referenced: list[tuple[list[Tag], str, str]] = []
        self._inline, self._display = renderers
        self._best_effort = best_effort
        self._math_media: list[tuple[str, bytes]] = []
        self._ctx = RenderContext(soup=self._soup, math_media=self._math_media)
        self._figures: list[tuple[str, bytes]] = []
        # figures already embedded, by their source URL, and the media
        # names taken; both span every embed_figures call
        self._figure_names: dict[str, str] = {}
        self._used_names: set[str] = set()
        self._ids: IdGenerator | None = None

    def new(self, name: str, attrs: dict[str, str] | None = None,
            text: str | None = None) -> Node:
        """A new element, detached until placed."""
        tag = self._soup.new_tag(name, attrs=attrs or {})
        if text is not None:
            tag.string = text
        return Node(tag, self._rules)

    def place(self, node: Node) -> None:
        """Put a detached node at the end of the page's body, where
        id minting and the stages see it."""
        (self._soup.body or self._soup).append(tag_of(node))

    def mint_id(self, base: str) -> str:
        """An id unique in the page (see :class:`IdGenerator`)."""
        if self._ids is None:
            self._ids = IdGenerator(self._soup)
        return self._ids.mint(base)

    def drop(self, nodes: Sequence[Node], selector: str) -> None:
        """Remove every element under *nodes* that *selector* matches
        (see :meth:`Node.drop <papis_make_epub.sources.node.Node.drop>`)."""
        for node in nodes:
            node.drop(selector)

    def formula(self, node: Node, *, display: bool, mathml: Node | None = None,
                tex: str | None = None) -> Formula:
        """The formula at *node*, carrying the formats the source has,
        for :meth:`render_math`."""
        return Formula(node=tag_of(node), display=display,
                       mathml=tag_of(mathml) if mathml is not None else None,
                       tex=tex)

    def render_math(self, formulas: Iterable[Formula]) -> None:
        """Replace each formula's node with its rendering; count the
        formulas rendered and degraded.

        Formula delivery differs per platform: a source collects the
        Formula objects from its own markup and, where the formulas
        sit in script elements, drops the remaining scripts after
        this call, since dropping them before would take the
        formulas with them.
        """
        n, degraded = apply_math(formulas, self._inline, self._display,
                                 self._ctx, best_effort=self._best_effort)
        self.stats.math += n
        self.stats.math_fallback += degraded

    def resolve_links(self, part_nodes: Sequence[Node],
                      targets: Callable[[Node], list[Target]] = fragment_targets,
                      ) -> None:
        """Point every cross-reference under *nodes* at its target in
        the document, and turn the ones without a target into text.

        *targets* names the ids an ``a`` element means to point at,
        in preference order, each recorded as a rule (see
        :func:`~papis_make_epub.sources.node.target`), and nothing for one that is not a
        cross-reference (an external URL, a mailto, a bare anchor),
        which stays as it is; the default reads fragment links, and
        a platform's function adds the forms its own links take (a
        prefixed id, a site path, an attribute on an element with
        no href). A cross-reference becomes a fragment link to the
        first candidate found under *nodes*. One with no candidate
        found is unwrapped, its text kept; when the element is
        itself a link target, only its href goes, and the element
        stays as the placeholder anchor the HTML standard provides
        for a link without a destination.
        """
        nodes = [tag_of(n) for n in part_nodes]
        ids = ids_under(nodes)
        unresolved = []
        for node in nodes:
            for a in node.find_all("a"):
                candidates = targets(Node(a, self._rules))
                if not candidates:
                    continue
                hit = next((c for c in candidates if c in ids), None)
                for c in candidates:
                    self._rules.record("target", "", int(c is hit), c.site)
                if hit is None:
                    unresolved.append(a)
                else:
                    a["href"] = "#" + hit
        targeted = {str(a["href"])[1:] for node in nodes
                    for a in node.find_all("a", href=True)
                    if str(a["href"]).startswith("#")}
        for a in unresolved:
            if a.get("id") in targeted:
                del a["href"]
            else:
                a.unwrap()
        self.stats.dead_links += len(unresolved)
        if unresolved:
            logger.warning(
                "%d cross-reference(s) have no target in the document and "
                "stay as text; their targets may sit in a part of the page "
                "the capture left out (a collapsed references list, for "
                "instance)", len(unresolved))

    def embed_figures(self, part_nodes: Iterable[Node]) -> None:
        """Rewrite the figures under *nodes* to point at embedded media;
        collect that media.

        Figures are ``img src`` and image ``object data`` embeds,
        math images excluded. Image bytes come from ``data:`` URIs
        (captures that inline resources) or from the page's files,
        located by :meth:`papis_make_epub.saved_page.SavedPage.resolve`.
        An unresolvable figure raises FigureError naming it. Every
        node that will be serialized must be passed, before it is.
        """
        figure_attrs = (("img", "src"), ("object", "data"))
        for node in (tag_of(n) for n in part_nodes):
            for el, attr in [(el, attr) for tag, attr in figure_attrs
                             for el in node.find_all(tag)]:
                if el.name == "img" and is_math_img(el):
                    continue
                if (el.name == "object"
                        and not str(el.get("type", "")).startswith("image/")):
                    continue
                src = el.get(attr)
                if not isinstance(src, str) or not src:
                    continue
                if src in self._figure_names:
                    el[attr] = self._figure_names[src]
                    continue
                if src.startswith("data:"):
                    decoded = _data_uri_bytes(src)
                    if decoded is None:
                        cause = (
                            ": the image had not loaded when the page was "
                            "saved; scroll through the whole page before "
                            "saving it" if src.strip() == "data:," else "")
                        raise FigureError(
                            "figure carries no image data in the saved "
                            f"page (src is {src[:40]!r}){cause}")
                    raw, base = decoded
                else:
                    local = self.page.resolve(src)
                    if local is None:
                        base = os.path.basename(src.split("?")[0])
                        raise FigureError(
                            f"figure not in saved page: {base[:80]}")
                    raw = local.read_bytes()
                    base = local.name
                data, base = core_image(raw, base)
                fn = unique_name("images/" + base, self._used_names)
                self._figures.append((fn, data))
                self._figure_names[src] = fn
                el[attr] = fn
        self.stats.images = len(self._figures)

    def add_part(self, node: Node, epub_type: str | None = None,
                 entries: list[TocEntry] | None = None) -> None:
        """Record the node as the next part; a node with no text is
        dropped with a warning (a part that had not rendered when the
        page was saved is the usual cause)."""
        tag = tag_of(node)
        if not tag.get_text(strip=True):
            logger.warning("empty %s part dropped; the page may have been "
                           "saved before it rendered",
                           epub_type or "untyped")
            return
        self._parts.append((tag, epub_type, entries or []))

    def add_abstract(self, node: Node) -> None:
        """Record the node as the abstract part, with its TOC entry."""
        self.add_part(node, "abstract", [TocEntry("Abstract", "part-abstract")])

    def add_sections(self, nodes: Sequence[Node],
                     epub_type: str | None = None) -> None:
        """Index the nodes' headings, then record one part per node,
        typed *epub_type*."""
        for node, entries in zip(nodes, self._index_headings(
                [tag_of(n) for n in nodes]), strict=True):
            self.add_part(node, epub_type, entries)

    def _index_headings(self, nodes: Sequence[Tag]) -> list[list[TocEntry]]:
        """Give every h2/h3 heading under the nodes an id; return each
        node's TocEntry tree.

        For platforms that title sections with h2 headings and
        subsections with h3: h3 entries nest under the preceding h2.
        A heading without an id gets a generated one, unique across
        the whole document (see :class:`IdGenerator`).
        """
        if not nodes:
            return []
        ids = IdGenerator(nodes[0])
        result = []
        for node in nodes:
            entries: list[TocEntry] = []
            children: list[TocEntry] | None = None
            for h in node.find_all(["h2", "h3"]):
                if not h.get("id"):
                    h["id"] = ids.mint("toc")
                entry = TocEntry(h.get_text(" ", strip=True) or "…",
                                 str(h["id"]))
                if h.name == "h2":
                    entries.append(entry)
                    children = entry.children
                elif children is not None:
                    children.append(entry)
                else:
                    entries.append(entry)
            result.append(entries)
        return result

    def add_footnotes(self, section: Node, footnotes: Sequence[Node]) -> None:
        """Record *section* as the footnotes part (its headings
        indexed, as :meth:`add_sections` does), *footnotes* being the
        footnotes it holds, each wrapped in place in an ``aside``
        typed ``footnote`` (with the DPUB-ARIA role). The links into
        a footnote become note references (``noteref``) and each
        footnote gets its back link, by :meth:`_type_references` at
        :meth:`content`. A reading system that implements pop-up
        notes shows each footnote at its mark, and may hide the back
        link there; one that shows the footnotes in place has the
        back link.

        A footnote is the element to show for its mark: it holds the
        id the mark's link targets. The links are the page's own,
        repaired by :meth:`resolve_links`; a platform whose marks are
        no links makes them before this.
        """
        asides = []
        for footnote in (tag_of(f) for f in footnotes):
            aside = self._soup.new_tag("aside")
            aside["epub:type"] = "footnote"
            aside["role"] = "doc-footnote"
            # the role sits on the wrapper now
            if footnote.get("role") == "doc-footnote":
                del footnote["role"]
            if footnote.name == "li":
                # a list holds list items alone: the aside goes inside
                # the item, around its content, and takes the item's
                # id, the one the marks target
                aside.extend(list(footnote.children))
                if footnote.get("id"):
                    aside["id"] = footnote["id"]
                    del footnote["id"]
                footnote.append(aside)
            else:
                footnote.wrap(aside)
            asides.append(aside)
        self._refer(asides, "noteref", "doc-noteref")
        self.add_sections([section], "footnotes")

    def add_bibliography(self, section: Node, entries: Sequence[Node]) -> None:
        """Record *section* as the bibliography part (its headings
        indexed, as :meth:`add_sections` does), *entries* being the
        reference entries it holds. Every link from the parts into an entry
        becomes a bibliographic reference (``biblioref``, with the
        DPUB-ARIA role), and each cited entry gets a back link
        (``backlink``) to its first citation, by
        :meth:`_type_references` at :meth:`content`. The entries
        themselves carry no type: the vocabularies deprecated the
        entry-level term.
        """
        self._refer([tag_of(e) for e in entries], "biblioref", "doc-biblioref")
        self.add_sections([section], "bibliography")

    def _refer(self, elements: Sequence[Tag], ref_type: str,
               ref_role: str) -> None:
        """Record *elements* for :meth:`_type_references`: the links
        into them get *ref_type* and *ref_role*."""
        self._referenced.append((list(elements), ref_type, ref_role))

    def content(self) -> Content:
        """The content: the recorded parts, serialized once four
        generic rules have run on them (:func:`drop_non_content`,
        :func:`drop_widget_markup`, :func:`conform_svg`,
        :func:`resolve_aria_references`) and the references are
        typed. The extraction's work on the soup ends here."""
        nodes = [node for node, _, _ in self._parts]
        for node in nodes:
            drop_non_content(node)
            drop_widget_markup(node)
            conform_svg(node)
        ids = ids_under(nodes)
        for node in nodes:
            resolve_aria_references(node, ids)
        self._type_references(nodes, ids)
        parts = [Part(str(node), epub_type, entries)
                 for node, epub_type, entries in self._parts]
        self.stats.math_images = len(self._math_media)
        return Content(parts=parts,
                       media=self._math_media + self._figures,
                       stats=self.stats,
                       language=page_language(self.page.text),
                       rules=dict(self._rules.matches))

    def _type_references(self, nodes: Sequence[Tag], ids: set[str]) -> None:
        """Type the links into the elements recorded by :meth:`_refer`
        (from :meth:`add_footnotes`, :meth:`add_bibliography`), and
        append to each referenced element a back link to its first
        referrer. A referrer with no id gets one, recorded in *ids*.

        It runs at :meth:`content`, over every part at once: a
        reference can come from any part, a part added later included
        (a footnote citing an entry), and the first referrer and a
        new id's uniqueness are defined over the whole document."""
        for elements, ref_type, ref_role in self._referenced:
            # the element each id belongs to
            owner = {i: el for el in elements
                     for i in ({str(el["id"])} if el.get("id") else set())
                     | ids_under([el])}
            # each element's referrers, in document order
            referrers: dict[int, list[str]] = {}
            for node in nodes:
                for a in node.find_all("a", href=True):
                    target = _fragment(a)
                    # a back link made by an earlier pass is no referrer
                    if (not target or target[0] not in owner
                            or a.get("epub:type") == "backlink"):
                        continue
                    el = owner[target[0]]
                    if a in el.descendants:
                        continue
                    a["epub:type"] = ref_type
                    a["role"] = ref_role
                    if not a.get("id"):
                        a["id"] = unique_name(f"{ref_type}-{target[0]}", ids)
                    referrers.setdefault(id(el), []).append(str(a["id"]))
            for el in elements:
                if id(el) not in referrers:
                    continue
                back = self._soup.new_tag("a", href="#" + referrers[id(el)][0])
                back["epub:type"] = "backlink"
                back["role"] = "doc-backlink"
                back["aria-label"] = "Return to text"
                back.string = "↩"
                el.append(back)


def drop_non_content(node: Tag) -> None:
    """Remove from under *node* the elements that are never article
    content (:data:`NON_CONTENT`), and unwrap the links the book
    cannot follow: javascript links, and links relative to the
    platform's site (a browser save's ``/chapter/…``), which the book
    does not carry. Fragment links stay."""
    for el in node.select(NON_CONTENT):
        el.decompose()
    for a in node.select("a[href]"):
        href = str(a["href"])
        if href.startswith("javascript:") or not (
                href.startswith("#") or urllib.parse.urlsplit(href).scheme):
            a.unwrap()


def page_words(html: str) -> int:
    """The words of a page's content, :data:`NON_CONTENT` left out."""
    doc = BeautifulSoup(html, "lxml")
    for el in doc.select(NON_CONTENT):
        el.decompose()
    return len(doc.get_text().split())


def drop_widget_markup(node: Tag) -> None:
    """Remove from *node* and its descendants the attributes that
    describe a web application's interaction: the widget roles and
    the widget states (:data:`WIDGET_ROLES`,
    :data:`WIDGET_ATTRIBUTES`)."""
    for el in [node, *node.find_all(True)]:
        if el.get("role") in WIDGET_ROLES:
            del el["role"]
        for attr in WIDGET_ATTRIBUTES:
            if el.has_attr(attr):
                del el[attr]


def conform_svg(node: Tag) -> None:
    """Make the inline svg under *node* valid in XHTML: declare the
    SVG namespace on each svg root, restore the camelCase element and
    attribute names the
    HTML parser lowercased (:data:`SVG_ELEMENT_NAMES`,
    :data:`SVG_ATTRIBUTE_NAMES`), and put the content of each
    ``foreignObject`` back in the XHTML namespace."""
    for svg in node.find_all("svg"):
        if svg.find_parent("svg"):
            continue
        svg["xmlns"] = SVG_NS
        for el in (svg, *svg.find_all(True)):
            el.name = SVG_ELEMENT_NAMES.get(el.name, el.name)
        for el in (svg, *svg.find_all(True)):
            if el.find_parent("foreignObject"):
                continue
            for attr in list(el.attrs):
                proper = SVG_ATTRIBUTE_NAMES.get(attr, attr)
                if proper != attr:
                    el.attrs[proper] = el.attrs.pop(attr)
        for fo in svg.find_all("foreignObject"):
            for child in fo.find_all(True, recursive=False):
                child["xmlns"] = XHTML_NS


def resolve_aria_references(node: Tag, ids: AbstractSet[str]) -> None:
    """Remove from the ARIA id references under *node* every token
    naming an id outside *ids*, and the attribute when none is left
    (:data:`ARIA_ID_REFERENCES`)."""
    for attr in ARIA_ID_REFERENCES:
        for el in [node, *node.find_all(True, attrs={attr: True})]:
            if not el.get(attr):
                continue
            kept = [t for t in str(el[attr]).split() if t in ids]
            if kept:
                el[attr] = " ".join(kept)
            else:
                del el[attr]


def word_count(node: Node) -> int:
    """The words under *node*, for telling an article from a page
    shell or a placeholder."""
    return len(node.text().split())


def outermost(nodes: Sequence[Node]) -> list[Node]:
    """The nodes that have no ancestor among *nodes*.

    A platform's section selector matches nested sections too; the
    parts are the top-level ones.
    """
    found = {id(tag_of(n)) for n in nodes}
    return [n for n in nodes
            if not any(id(p) in found for p in tag_of(n).parents)]


def ids_under(nodes: Iterable[Tag]) -> set[str]:
    """Every element id under *nodes*: the targets a fragment link
    can resolve to once the parts are serialized together."""
    return {str(t["id"]) for node in nodes for t in node.find_all(id=True)}


# image media types EPUB readers must support
CORE_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/svg+xml"}


def core_image(data: bytes, base: str) -> tuple[bytes, str]:
    """Return (data, file name) with non-core types converted.

    Non-core formats (e.g. webp) are converted to PNG via Pillow;
    readers need not display them, so a failed conversion raises
    FigureError.
    """
    mt = mimetypes.guess_type(base)[0] or "image/jpeg"
    if mt in CORE_IMAGE_TYPES:
        return data, base
    try:
        buf = io.BytesIO()
        Image.open(io.BytesIO(data)).save(buf, format="PNG")
    except Exception as exc:
        raise FigureError(
            f"cannot convert non-core image {base!r} ({mt}) to PNG: "
            f"{exc}") from exc
    return buf.getvalue(), os.path.splitext(base)[0] + ".png"


def _data_uri_bytes(src: str) -> tuple[bytes, str] | None:
    """Decode a ``data:`` URI into (bytes, file name with extension)."""
    m = re.match(r"data:([\w/+.-]+)(;base64)?,(.*)", src, re.S)
    if m is None:
        return None
    mime, b64, payload = m.groups()
    try:
        data = (base64.b64decode(payload) if b64
                else urllib.parse.unquote_to_bytes(payload))
    except (ValueError, binascii.Error):
        return None
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
           "image/svg+xml": "svg", "image/webp": "webp"}.get(mime, "bin")
    return data, f"figure.{ext}"


def unique_name(name: str, used: set[str]) -> str:
    """Return *name*, counter-suffixed if already in *used*; records it.

    Figures are named by source basename; two distinct sources sharing
    a basename must not produce duplicate zip entries.
    """
    candidate = name
    stem, ext = os.path.splitext(name)
    n = 2
    while candidate in used:
        candidate = f"{stem}-{n}{ext}"
        n += 1
    used.add(candidate)
    return candidate


def page_language(html_text: str) -> str | None:
    """The page's own language declaration.

    The ``lang`` attribute sits on the ``html`` start tag, within
    the first bytes, so a bounded prefix suffices to parse it.
    """
    root = BeautifulSoup(html_text[:4096], "lxml").find("html")
    if isinstance(root, Tag):
        lang = root.get("lang")
        if lang:
            return str(lang)
    return None


class IdGenerator:
    """Mints ids unique within a node's document.

    Built from a node, it seeds itself with the ids already in that
    node's document (native ids, and any minted earlier on the same
    tree), so a generated id never collides. ``mint`` derives the id
    from a caller-supplied base (a slug, or a bare tag like ``toc``),
    counter-suffixed on collision, and records it as taken.
    """

    def __init__(self, node: Tag) -> None:
        root = node
        while root.parent is not None:
            root = root.parent
        self._taken = {str(t["id"]) for t in root.find_all(id=True)}

    def mint(self, base: str) -> str:
        candidate = base
        n = 2
        while candidate in self._taken:
            candidate = f"{base}-{n}"
            n += 1
        self._taken.add(candidate)
        return candidate
