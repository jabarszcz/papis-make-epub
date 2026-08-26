#!/usr/bin/env python3
"""What the fixture corpus proves about the sources' selectors, and
what a private page collection would add.

For every selector the sources drop, the table gives the fixtures
(by manifest id) in which it matched and, for each directory of
saved pages given on the command line, how many of that platform's
pages matched it. A selector matched by no fixture has no evidence
in the repository; a private page that matches it is a candidate
for a reduction. Run from the repository root:

    python tools/fixture_coverage.py [DIR ...]

Line coverage of the source modules is the other half of the
picture and comes from the test run: ``pytest --cov``.
"""

import sys
import tomllib
from collections import defaultdict
from pathlib import Path

from papis_make_epub.api import renderer_resolver
from papis_make_epub.book import BuildOptions
from papis_make_epub.saved_page import SavedPage
from papis_make_epub.sources import SOURCES, acm, ieee, sciencedirect

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# (platform, selector) -> {label: matches}; a label is a manifest id
# or a private page's name
MATCHES: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)


def _extract(label: str, platform: str, page: SavedPage,
             best_effort: bool, reduction: bool) -> str | None:
    """Extract and record the selector matches the extraction counted;
    the failure reason, if any."""
    source = next(s for s in SOURCES if s.key == platform)
    options = BuildOptions(best_effort=best_effort)
    saved = (acm.MIN_BODY_WORDS, ieee.MIN_ARTICLE_WORDS,
             sciencedirect.MIN_ARTICLE_WORDS)
    if reduction:
        acm.MIN_BODY_WORDS = ieee.MIN_ARTICLE_WORDS = 0
        sciencedirect.MIN_ARTICLE_WORDS = 0
    try:
        renderers = renderer_resolver(options)(source.math_formats)
        content = source.extract(page, renderers=renderers,
                                 best_effort=best_effort)
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc)[:80]}"
    finally:
        (acm.MIN_BODY_WORDS, ieee.MIN_ARTICLE_WORDS,
         sciencedirect.MIN_ARTICLE_WORDS) = saved
    for sub, n in content.stats.dropped.items():
        MATCHES[(platform, sub)][label] = n
    return None


def main(dirs: list[str]) -> int:
    with open(FIXTURES / "manifest.toml", "rb") as f:
        entries = tomllib.load(f)["fixture"]
    fixture_ids = set()
    for e in entries:
        path = (FIXTURES / "library" / e["id"] / f"{e['id']}.html"
                if e["kind"] == "capture"
                else FIXTURES / "reductions" / f"{e['id']}.html")
        fixture_ids.add(e["id"])
        failure = _extract(e["id"], e["platform"], SavedPage.from_file(path),
                           e.get("best_effort", False),
                           e["kind"] == "reduction")
        if failure:
            print(f"fixture {e['id']}: {failure}", file=sys.stderr)

    private: dict[str, int] = defaultdict(int)
    for d in dirs:
        for path in sorted(Path(d).rglob("*.htm*")):
            if "_files" in path.parent.name:
                continue
            text = path.read_text(errors="replace")
            hits = [s for s in SOURCES if s.matches(text)]
            if len(hits) != 1:
                continue
            platform = hits[0].key
            private[platform] += 1
            failure = _extract(str(path), platform, SavedPage.from_file(path),
                               True, False)
            if failure:
                print(f"{path.name[:50]}: {failure}", file=sys.stderr)

    for platform in [s.key for s in SOURCES]:
        print(f"== {platform}" + (f" ({private[platform]} private pages)"
                                  if dirs else ""))
        for (p, sel), by_label in sorted(MATCHES.items()):
            if p != platform:
                continue
            fixtures = sorted(label for label, n in by_label.items()
                              if n and label in fixture_ids)
            pages = sum(1 for label, n in by_label.items()
                        if n and label not in fixture_ids)
            flag = "" if fixtures else "   <- no fixture"
            print(f"  {sel:45} fixtures: {', '.join(fixtures) or '-':24}"
                  + (f" private pages: {pages}" if dirs else "") + flag)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
