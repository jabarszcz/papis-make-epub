"""Every rule a source applies must fire in a fixture of its platform:
dated evidence of the form each handles, so that a change or a
removal is weighed knowing which saved pages it affects.

A rule is a query call in a platform module (a selector, a fallback
selector, an attribute pattern) or a link-target candidate it builds
(see :mod:`papis_make_epub.sources.node`), and a fixture in which it
matches (the candidate resolving a link) is its evidence, dated by
the manifest. A re-capture that no
longer carries a form leaves its rule without evidence; a reduction
of the old capture keeps the form, and the rule with it, so pages
saved while the platform served it still build. The rule goes when
what it does conflicts with the platform's current pages.

The platform modules reach the page through the queries alone, which
the ruff configuration checks, so the query calls listed from their
source are every rule they apply.
"""

import inspect

from conftest import RULE_MATCHES

from papis_make_epub.sources import SOURCES
from papis_make_epub.sources.node import Rule, rule_sites

MODULES = sorted({inspect.getsourcefile(type(s)) or "" for s in SOURCES})


# pytest collects the test_-prefixed functions; this one gets the
# prefix once every rule has its fixture (gaps are open at this point)
def every_rule_fires_in_a_fixture(contents: dict[str, object]) -> None:
    fired: dict[Rule, int] = {
        rule: sum(by_fixture.values()) for rule, by_fixture in RULE_MATCHES.items()}
    gaps = []
    for path in MODULES:
        for rs in rule_sites(path):
            rules = [r for r in fired if r.site == rs.site]
            if not rules:
                gaps.append(f"{rs.where}: {rs.call} never ran on a fixture")
            # a link-target rule has no detail; its call names it
            gaps.extend(
                f"{rs.where}: {f'{r.query} {r.detail!r}' if r.detail else rs.call}"
                " matched no fixture"
                for r in rules if fired[r] == 0)
    assert not gaps, (
        "rules without evidence in the fixtures; add a reduction that "
        "carries the form:\n" + "\n".join(gaps))
