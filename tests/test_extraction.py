"""Snapshot tests of each fixture's content and its built book.

The snapshot pins the content's structure and the built book's
packaging; the text golden pins the extracted text whole. A
re-capture changes them, so the goldens are regenerated with
``--force-regen`` and the diff is reviewed; collapsed counts and
structural drift show in the snapshot, injected or lost prose in
the text.
"""

from pathlib import Path
from typing import Any

from conftest import Entry, package_info, part_texts, snapshot

from papis_make_epub.sources.extraction import Content


def test_snapshot(entry: Entry, content: Content,
                  built_epub: Path, data_regression: Any) -> None:
    data_regression.check(snapshot(content)
                          | {"package": package_info(built_epub)})


def test_text(entry: Entry, content: Content,
              file_regression: Any) -> None:
    file_regression.check(part_texts(content), extension=".txt")
