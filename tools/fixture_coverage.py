#!/usr/bin/env python3
"""What the fixture corpus proves about the sources' rules, and what a
collection of saved pages would add.

For every rule a source can apply (a query call in its module, or a
link-target candidate it builds; see
``papis_make_epub/sources/node.py``), the report gives its source
position, the selectors or patterns it applies (a candidate's call),
the fixtures (by
manifest id) in which each matched, and, for the directories of saved
pages given on the command line (a papis library, for instance),
the pages it matched in. A rule matched by no fixture has no evidence
in the repository; a page that matches it is a candidate for a
reduction. Run from the repository root:

    python tools/fixture_coverage.py [DIR ...]

Line coverage of the source modules is the other half of the
picture and comes from the test run: ``pytest --cov``.
"""

import inspect
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

from papis_make_epub.api import renderer_resolver
from papis_make_epub.book import BuildOptions
from papis_make_epub.saved_page import SavedPage
from papis_make_epub.sources import SOURCES, acm, ieee, sciencedirect
from papis_make_epub.sources.node import Rule, rule_sites

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
# saved pages named per rule before the rest are counted
PAGES_SHOWN = 3

# rule -> {label: matches}; a label is a manifest id or a saved
# page's path
MATCHES: dict[Rule, dict[str, int]] = defaultdict(dict)


def _extract(label: str, platform: str, page: SavedPage,
             best_effort: bool, reduction: bool) -> str | None:
    """Extract and record the rules' matches; the failure reason, if
    any."""
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
    for rule, n in content.rules.items():
        MATCHES[rule][label] = MATCHES[rule].get(label, 0) + n
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

    saved: dict[str, int] = defaultdict(int)
    for d in dirs:
        for path in sorted(Path(d).rglob("*.htm*")):
            if "_files" in path.parent.name:
                continue
            text = path.read_text(errors="replace")
            hits = [s for s in SOURCES if s.matches(text)]
            if len(hits) != 1:
                continue
            platform = hits[0].key
            saved[platform] += 1
            failure = _extract(str(path), platform, SavedPage.from_file(path),
                               True, False)
            if failure:
                print(f"{path.name[:50]}: {failure}", file=sys.stderr)

    for source in SOURCES:
        module = inspect.getsourcefile(type(source)) or ""
        print(f"== {source.key}" + (f" ({saved[source.key]} saved pages)"
                                     if dirs else ""))
        for rs in rule_sites(module):
            rules = sorted((r for r in MATCHES if r.site == rs.site),
                           key=lambda r: (r.query, r.detail))
            if not rules:
                print(f"  {rs.where:20} {rs.call[:66]:66} never ran   <- no fixture")
            for rule in rules:
                _print_rule(rs.where, rule, rs.call, fixture_ids, bool(dirs))
    return 0


def _print_rule(where: str, rule: Rule, call: str, fixture_ids: set[str],
                with_pages: bool) -> None:
    # a link-target rule has no detail; its call names it
    detail = rule.detail or call
    by_label = MATCHES[rule]
    fixtures = sorted(label for label, n in by_label.items()
                      if n and label in fixture_ids)
    pages = sorted(Path(label).name[:40] for label, n in by_label.items()
                   if n and label not in fixture_ids)
    shown = ", ".join(pages[:PAGES_SHOWN])
    if len(pages) > PAGES_SHOWN:
        shown += f" (+{len(pages) - PAGES_SHOWN})"
    flag = "" if fixtures else "   <- no fixture"
    print(f"  {where:20} {rule.query:16} {detail[:48]:48} "
          f"fixtures: {', '.join(fixtures) or '-'}"
          + (f"; pages: {shown or '-'}" if with_pages else "") + flag)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
