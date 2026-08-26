"""The command, run against a copy of the fixtures library.

Each test gets a temporary papis configuration whose library is a
writable copy of ``fixtures/library``, so the command runs end to end
(query, saved-page detection, build, files registration) on the same
corpus the extraction tests pin. The fetch path is exercised with a
faked ``fetch_page`` that materializes the committed arxiv capture.
"""

import logging
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from conftest import FIXTURES
from papis.document import from_folder
from papis.testing import PapisRunner, TemporaryLibrary

from papis_make_epub.cli import cli
from papis_make_epub.saved_page import SavedPage

LIBRARY = FIXTURES / "library"


@pytest.fixture
def fixtures_lib(caplog: Any) -> Iterator[TemporaryLibrary]:
    caplog.set_level(logging.INFO)
    with TemporaryLibrary(populate=False) as lib:
        for doc_dir in sorted(LIBRARY.iterdir()):
            shutil.copytree(doc_dir, Path(lib.libdir) / doc_dir.name)
        yield lib


def invoke(*args: str) -> Result:
    return PapisRunner().invoke(cli, list(args))


def doc_folder(lib: TemporaryLibrary, key: str) -> Path:
    return Path(lib.libdir) / key


def registered_files(lib: TemporaryLibrary, key: str) -> list[str]:
    return list(from_folder(str(doc_folder(lib, key)))["files"])


def test_builds_from_saved_page(fixtures_lib: TemporaryLibrary) -> None:
    result = invoke("author:barrow")
    assert result.exit_code == 0
    epubs = [f for f in registered_files(fixtures_lib, "sciencedirect")
             if f.endswith(".epub")]
    assert len(epubs) == 1
    assert (doc_folder(fixtures_lib, "sciencedirect") / epubs[0]).exists()


def test_existing_epub_skips_then_force_rebuilds(
        fixtures_lib: TemporaryLibrary, caplog: Any) -> None:
    assert invoke("author:barrow").exit_code == 0
    caplog.clear()
    assert invoke("author:barrow").exit_code == 0
    assert any("already has" in r.message for r in caplog.records)
    caplog.clear()
    assert invoke("--force", "author:barrow").exit_code == 0
    assert any(r.message.startswith("Built") for r in caplog.records)


def test_several_saved_pages_fail(fixtures_lib: TemporaryLibrary,
                                  caplog: Any) -> None:
    folder = doc_folder(fixtures_lib, "sciencedirect")
    shutil.copy(LIBRARY / "acm" / "acm.html", folder / "second.html")
    doc = from_folder(str(folder))
    doc["files"] = [*doc["files"], "second.html"]
    doc.save()

    result = invoke("author:barrow")
    assert result.exit_code == 1
    messages = [r.message for r in caplog.records]
    assert any("several saved pages" in m and "second.html" in m
               for m in messages)


def test_unrecognized_page_falls_through(fixtures_lib: TemporaryLibrary,
                                         caplog: Any) -> None:
    # with its page unrecognized and no arXiv metadata, the Barrow
    # document reaches the fetch and is skipped there
    page = doc_folder(fixtures_lib, "sciencedirect") / "sciencedirect.html"
    page.write_text("<html><body><p>replaced</p></body></html>")

    result = invoke("author:barrow")
    assert result.exit_code == 0
    assert any("no arXiv source" in r.message for r in caplog.records)


def test_file_argument(fixtures_lib: TemporaryLibrary) -> None:
    result = invoke("author:barrow",
                    str(LIBRARY / "sciencedirect" / "sciencedirect.html"))
    assert result.exit_code == 0
    assert any(f.endswith(".epub")
               for f in registered_files(fixtures_lib, "sciencedirect"))


def test_from_without_file_fails(fixtures_lib: TemporaryLibrary,
                                 caplog: Any) -> None:
    result = invoke("--from", "ieee", "author:barrow")
    assert result.exit_code == 1
    assert any("give the file too" in r.message for r in caplog.records)


def test_fetch_route(fixtures_lib: TemporaryLibrary,
                     monkeypatch: Any) -> None:
    # the arxiv document falls to the fetch once its saved page is
    # deregistered; the fake materializes the committed capture and
    # its figure sidecars the way fetch_page does
    folder = doc_folder(fixtures_lib, "arxiv")
    doc = from_folder(str(folder))
    doc["files"] = [f for f in doc["files"] if not f.endswith(".html")]
    doc.save()

    def fake_fetch_page(arxiv_id: str, dest_dir: str) -> SavedPage:
        assert arxiv_id == "2312.00752v2"
        text = (LIBRARY / "arxiv" / "arxiv.html").read_text(errors="replace")
        path = Path(dest_dir) / "page.html"
        path.write_text(text)
        shutil.copytree(LIBRARY / "arxiv" / "arxiv_files",
                        Path(dest_dir) / "page_files")
        return SavedPage(text, path=path)

    monkeypatch.setattr("papis_make_epub.api.fetch_page", fake_fetch_page)

    result = invoke("author:gu")
    assert result.exit_code == 0
    epubs = [f for f in registered_files(fixtures_lib, "arxiv")
             if f.endswith(".epub")]
    assert len(epubs) == 1
