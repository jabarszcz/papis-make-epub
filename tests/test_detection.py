"""The source detection over the fixture corpus.

Commits the detection tuning: every capture and reduction must
match exactly its own platform, so a marker that starts matching a
foreign page (or stops matching its own) fails here rather than at
build time. For a reduction this also checks that the pruning kept
the detector's marker.
"""

from conftest import ENTRIES

from papis_make_epub.sources import SOURCES


def test_each_fixture_matches_exactly_its_platform() -> None:
    for entry in ENTRIES:
        text = entry.path.read_text(errors="replace")
        hits = [s.key for s in SOURCES if s.matches(text)]
        assert hits == [entry.platform], entry.id


def test_plain_html_matches_nothing() -> None:
    text = "<html><body><p>not a full-text page</p></body></html>"
    assert [s.key for s in SOURCES if s.matches(text)] == []
