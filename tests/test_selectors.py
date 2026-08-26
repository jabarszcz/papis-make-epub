"""Every selector the sources drop must match in a fixture of its
platform: dated evidence of the form each handles, so that a change
or a removal is weighed knowing which saved pages it affects.

The selectors are a platform's chrome and residue knowledge, and a
fixture that matches one is its evidence, dated by the manifest. A
re-capture that no longer carries a form leaves its selector without
evidence; a reduction of the old capture keeps the form, and the
selector with it, so pages saved while the platform served it still
build. The selector goes when what it does conflicts with the
platform's current pages.

The sources' attribute edits (ACM's data-* and role, IEEE's Angular
and reference attributes, arXiv's list style, ScienceDirect's
back-link hrefs) are platform knowledge of the same kind without
this check.
"""

from conftest import DROP_MATCHES, Entry  # noqa: F401 (Entry: fixture typing)


def test_every_dropped_selector_matches_a_fixture(
        contents: dict[str, object]) -> None:
    unmatched = sorted(key for key, n in DROP_MATCHES.items() if n == 0)
    assert not unmatched, (
        "selectors matching no fixture of their platform; add a "
        "reduction that carries the form: "
        + ", ".join(f"{p}: {s!r}" for p, s in unmatched))
