"""The saved-page layout decisions: media destinations, sidecar
selection, and figure resolution.

Synthetic saves in tmp_path stand in for the layouts observed in the
wild; the fixture corpus only exercises exact-path hits and inlined
data URIs.
"""

import os
from pathlib import Path

import pytest

from papis_make_epub.saved_page import SavedPage


def make_save(tmp_path: Path, files: dict[str, str]) -> SavedPage:
    page = tmp_path / "page.html"
    page.write_text("<html></html>")
    for rel, content in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    return SavedPage("<html></html>", path=page)


@pytest.mark.parametrize(("src", "expected"), [
    ("img/a.png", "page_files/img/a.png"),
    ("a.png?width=800", "page_files/a.png"),
    ("a.png#fig1", "page_files/a.png"),
    ("a%20b.png", "page_files/a b.png"),
    # dot segments fold as fetching folds them
    ("img/x/../a.png", "page_files/img/a.png"),
    # the root, the host, and leading ".." drop; plain segments stay
    ("../../a.png", "page_files/a.png"),
    ("/abs/a.png", "page_files/abs/a.png"),
    ("https://host/img/a.png", "page_files/img/a.png"),
])
def test_media_path(tmp_path: Path, src: str, expected: str) -> None:
    page = SavedPage("", path=tmp_path / "page.html")
    assert page.media_path(src) == tmp_path / expected


def test_save_writes_the_html(tmp_path: Path) -> None:
    page = SavedPage("<html><body>hi</body></html>")
    page.save(tmp_path / "page.html", "https://example.org/x")
    assert page.path == tmp_path / "page.html"
    assert (tmp_path / "page.html").read_text() == page.text


def test_resolves_by_exact_path(tmp_path: Path) -> None:
    page = make_save(tmp_path, {"imgs/x.png": "a",
                                "page_files/f/y.png": "b"})
    # against the page's directory (rewritten browser refs, and pages
    # saved with their URL structure beside them)
    assert page.resolve("imgs/x.png") == tmp_path / "imgs/x.png"
    # inside the sidecar dir (our own fetch layout)
    assert page.resolve("f/y.png") == tmp_path / "page_files/f/y.png"
    # query string and percent-encoding do not defeat the path
    assert page.resolve("imgs/x.png?v=2") == tmp_path / "imgs/x.png"


def test_relative_page_path(tmp_path: Path,
                            monkeypatch: pytest.MonkeyPatch) -> None:
    # a bare FILE argument stays as spelled; its parent is "."
    make_save(tmp_path, {"page_files/a.png": "a"})
    monkeypatch.chdir(tmp_path)
    page = SavedPage("<html></html>", path=Path("page.html"))
    assert page.sidecar_dir() == Path("page_files")
    assert page.resolve("cdn/a.png") == Path("page_files/a.png")


def test_falls_back_by_basename_then_stem(tmp_path: Path) -> None:
    page = make_save(tmp_path, {"page_files/a.png": "a",
                                "page_files/b.png": "b"})
    # basename: the src names a folder the save did not create
    assert page.resolve("cdn/img/a.png") == tmp_path / "page_files/a.png"
    # stem: the save fetched another format than the src names
    assert page.resolve("b.gif") == tmp_path / "page_files/b.png"
    assert page.resolve("missing.png") is None


def test_stem_prefers_core_image_types(tmp_path: Path) -> None:
    page = make_save(tmp_path, {"page_files/b.png": "png",
                                "page_files/b.webp": "webp"})
    assert page.resolve("b.gif") == tmp_path / "page_files/b.png"


def test_escaping_src_stays_inside(tmp_path: Path) -> None:
    # out-of-tree srcs map inside the sidecar; nothing outside the
    # page's directory is ever probed
    page = make_save(tmp_path, {"page_files/x.png": "x"})
    assert page.resolve("../../x.png") == tmp_path / "page_files/x.png"
    assert page.resolve("/etc/x.png") == tmp_path / "page_files/x.png"
    assert page.resolve("../secret.png") is None


def test_other_saves_sidecars_are_ignored(tmp_path: Path) -> None:
    page = make_save(tmp_path, {"other_files/z.png": "z"})
    assert page.resolve("z.png") is None


def test_sidecar_tie_breaks_by_mtime(tmp_path: Path) -> None:
    page = make_save(tmp_path, {"page_files/x.png": "near",
                                "page_extra/x.png": "far"})
    mtime = (tmp_path / "page.html").stat().st_mtime
    os.utime(tmp_path / "page_files", (mtime, mtime))
    os.utime(tmp_path / "page_extra", (mtime - 9000, mtime - 9000))
    assert page.sidecar_dir() == tmp_path / "page_files"


def test_unsaved_page_resolves_nothing() -> None:
    assert SavedPage("<html></html>").resolve("x.png") is None
    assert SavedPage("<html></html>").sidecar_dir() is None
