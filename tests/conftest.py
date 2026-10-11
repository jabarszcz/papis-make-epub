"""Shared test configuration: the fixture corpus and its extractions.

``fixtures/manifest.toml`` lists the corpus with its provenance:
captures, one open-access paper per platform (``fixtures/refresh.py``
re-captures them), and reductions, pages cut down to the parts that
exercise a form the captures do not. Each entry's content is
extracted once per session and shared by the golden, build and
rule tests.
"""

import os
import posixpath
import tomllib
import warnings
import zipfile
from collections.abc import Generator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from bs4 import BeautifulSoup, Tag, XMLParsedAsHTMLWarning
from papis.document import from_folder

from papis_make_epub import api
from papis_make_epub.book import BuildOptions, DocMeta, write_epub
from papis_make_epub.math_renderers import RendererUnavailableError
from papis_make_epub.saved_page import SavedPage
from papis_make_epub.sources import SOURCES, acm, ieee, sciencedirect
from papis_make_epub.sources.extraction import Content, TocEntry
from papis_make_epub.sources.node import Rule

FIXTURES = Path(__file__).parent / "fixtures"


# tool-gated tests skip where a program is missing; environments
# that supply every tool deliberately (the flake's checks) set
# PAPIS_MAKE_EPUB_NO_SKIPS, and a skip becomes a failure carrying
# the skip reason
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any,
                              ) -> Generator[None, Any, None]:
    outcome = yield
    report = outcome.get_result()
    if report.skipped and os.environ.get("PAPIS_MAKE_EPUB_NO_SKIPS"):
        report.outcome = "failed"


@dataclass(frozen=True)
class Entry:
    """One manifest entry: a capture or a reduction."""

    id: str
    kind: str
    platform: str
    url: str
    captured: date
    best_effort: bool

    @property
    def path(self) -> Path:
        if self.kind == "capture":
            return FIXTURES / "library" / self.id / f"{self.id}.html"
        return FIXTURES / "reductions" / f"{self.id}.html"


def _load_entries() -> list[Entry]:
    with open(FIXTURES / "manifest.toml", "rb") as f:
        data = tomllib.load(f)
    return [Entry(id=e["id"], kind=e["kind"], platform=e["platform"],
                  url=e["url"], captured=e["captured"],
                  best_effort=e.get("best_effort", False))
            for e in data["fixture"]]


ENTRIES = _load_entries()

# a reduction keeps little prose, so the platforms' page-shell word
# gates are lifted for it
_WORD_GATES = [(acm, "MIN_BODY_WORDS"), (ieee, "MIN_ARTICLE_WORDS"),
               (sciencedirect, "MIN_ARTICLE_WORDS")]

# the matches of every rule the sources applied while the corpus was
# extracted, per fixture id; test_rules reads it
RULE_MATCHES: dict[Rule, dict[str, int]] = {}


def build_options(entry: Entry) -> BuildOptions:
    # the mathml pass-through keeps math_images at 0 and the counts
    # deterministic; texmath converts the TeX-only sources' formulas
    return BuildOptions(math_inline="mathml", math_display="mathml",
                        best_effort=entry.best_effort)


def doc_meta(entry: Entry) -> DocMeta:
    """The book identity: a capture's committed bibliographic record,
    through the library's own mapping; a reduction's id and URL."""
    if entry.kind == "capture":
        doc = from_folder(str(FIXTURES / "library" / entry.id))
        return api.doc_meta(doc)
    return DocMeta(title=entry.id, authors=[], identifier=entry.url)


def _extract(entry: Entry) -> Content:
    source = next(s for s in SOURCES if s.key == entry.platform)
    page = SavedPage.from_file(entry.path)
    options = build_options(entry)
    # the TeX-only captures need texmath for the mathml renderer
    renderers = api.renderer_resolver(options)(source.math_formats)
    with pytest.MonkeyPatch.context() as mp:
        if entry.kind == "reduction":
            for module, name in _WORD_GATES:
                mp.setattr(module, name, 0)
        content = source.extract(page, renderers=renderers,
                                 best_effort=options.best_effort)
    for rule, n in content.rules.items():
        by_fixture = RULE_MATCHES.setdefault(rule, {})
        by_fixture[entry.id] = by_fixture.get(entry.id, 0) + n
    meta = doc_meta(entry)
    content.add_title_page(meta.title, meta.authors)
    return content


@pytest.fixture(scope="session")
def contents() -> dict[str, Content | str]:
    """Every entry's content, extracted once; a string is the reason
    the entry could not be extracted here (a missing tool)."""
    out: dict[str, Content | str] = {}
    for entry in ENTRIES:
        try:
            out[entry.id] = _extract(entry)
        except RendererUnavailableError as exc:
            out[entry.id] = str(exc)
    return out


@pytest.fixture(scope="session", params=ENTRIES, ids=lambda e: e.id)
def entry(request: pytest.FixtureRequest) -> Any:
    return request.param


@pytest.fixture(scope="session")
def content(entry: Entry, contents: dict[str, Content | str]) -> Content:
    result = contents[entry.id]
    if isinstance(result, str):
        pytest.skip(result)
    return result


@pytest.fixture(scope="session")
def built_epub(entry: Entry, content: Content,
               tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The entry's EPUB, built once and shared by the build-side
    tests (epubcheck and the packaging snapshot)."""
    out = tmp_path_factory.mktemp(entry.id) / f"{entry.id}.epub"
    write_epub(str(out), meta=doc_meta(entry), content=content,
               options=build_options(entry))
    return out


def _toc(entries: list[TocEntry]) -> list[Any]:
    # a leaf is its title; a parent is {title: [children]} so the
    # golden YAML reads as the section tree
    return [{e.title: _toc(e.children)} if e.children else e.title
            for e in entries]


def snapshot(ex: Content) -> dict[str, Any]:
    """The content's structure for the snapshot golden.

    Carries the shape and magnitude of the content (stats, media,
    the part and TOC structure) without the raw HTML or image bytes,
    so a golden diff reads as a structural changelog; the text
    itself is pinned by :func:`part_texts`, the built book's
    packaging by :func:`package_info`.
    """
    extensions: dict[str, int] = {}
    for name, _data in ex.media:
        ext = name.rsplit(".", 1)[-1].lower()
        extensions[ext] = extensions.get(ext, 0) + 1
    parts = []
    for p in ex.parts:
        text = BeautifulSoup(p.html, "lxml").get_text(" ", strip=True)
        parts.append({"epub_type": p.epub_type,
                      "text_length": len(text),
                      "toc": _toc(p.entries)})
    return {
        "language": ex.language,
        "stats": {"math": ex.stats.math,
                  "math_fallback": ex.stats.math_fallback,
                  "math_images": ex.stats.math_images,
                  "images": ex.stats.images,
                  "dead_links": ex.stats.dead_links},
        "media_count": len(ex.media),
        "media_extensions": dict(sorted(extensions.items())),
        "parts": parts,
    }


def part_texts(ex: Content) -> str:
    """The content's whole text for the text golden.

    One line per text node keeps the golden diff line-grained, so a
    re-capture's diff reads as a prose changelog; parts are
    delimited with their position and ``epub_type``.
    """
    blocks = []
    for i, p in enumerate(ex.parts):
        text = BeautifulSoup(p.html, "lxml").get_text("\n", strip=True)
        blocks.append(f"==== part {i} ({p.epub_type})\n{text}")
    return "\n\n".join(blocks) + "\n"


def _local_name(name: str) -> Any:
    # bs4's xml parser keeps namespace prefixes in tag names
    return lambda t: isinstance(t, Tag) and t.name.split(":")[-1] == name


def _nav_tree(el: Tag) -> list[Any]:
    # mirrors _toc: a leaf is its link text, a parent {text: children}
    out: list[Any] = []
    ol = el.find("ol", recursive=False)
    if not isinstance(ol, Tag):
        return out
    for li in ol.find_all("li", recursive=False):
        a = li.find(["a", "span"], recursive=False)
        text = a.get_text(strip=True) if isinstance(a, Tag) else ""
        children = _nav_tree(li)
        out.append({text: children} if children else text)
    return out


def package_info(epub_path: Path) -> dict[str, Any]:
    """The built book's packaging for the snapshot golden.

    Read back from the archive itself: OPF metadata, the manifest
    (media types and properties), the spine order, and the nav
    document's TOC tree and landmarks.
    """
    with zipfile.ZipFile(epub_path) as z:
        container = BeautifulSoup(z.read("META-INF/container.xml"), "xml")
        rootfile = container.find("rootfile")
        assert isinstance(rootfile, Tag)
        opf_path = str(rootfile["full-path"])
        opf = BeautifulSoup(z.read(opf_path), "xml")

        manifest: dict[str, str] = {}
        nav_href = None
        for item in opf.find_all(_local_name("item")):
            props = sorted(str(item.get("properties") or "").split())
            manifest[str(item["href"])] = " ".join(
                [str(item["media-type"]), *props])
            if "nav" in props:
                nav_href = str(item["href"])
        ids = {str(i["id"]): str(i["href"])
               for i in opf.find_all(_local_name("item"))}
        spine = [ids.get(str(ref["idref"]), str(ref["idref"]))
                 for ref in opf.find_all(_local_name("itemref"))]

        metadata: dict[str, Any] = {}
        md = opf.find(_local_name("metadata"))
        assert isinstance(md, Tag)
        for name in ("title", "creator", "identifier", "language"):
            values = [t.get_text(strip=True)
                      for t in md.find_all(_local_name(name))]
            metadata[name] = values if len(values) > 1 else (
                values[0] if values else None)

        assert nav_href is not None
        nav_path = posixpath.normpath(
            posixpath.join(posixpath.dirname(opf_path), nav_href))
        with warnings.catch_warnings():
            # the nav is XHTML; the html parser reads it with the
            # epub:type attributes kept literal
            warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
            nav = BeautifulSoup(z.read(nav_path), "lxml")
        toc_nav = nav.find("nav", attrs={"epub:type": "toc"})
        landmarks_nav = nav.find("nav", attrs={"epub:type": "landmarks"})
        landmarks = []
        if isinstance(landmarks_nav, Tag):
            landmarks = [
                {"type": str(a.get("epub:type") or ""),
                 "href": str(a.get("href") or ""),
                 "text": a.get_text(strip=True)}
                for a in landmarks_nav.find_all("a")]

    return {
        "metadata": metadata,
        "manifest": dict(sorted(manifest.items())),
        "spine": spine,
        "toc": _nav_tree(toc_nav) if isinstance(toc_nav, Tag) else [],
        "landmarks": landmarks,
    }
