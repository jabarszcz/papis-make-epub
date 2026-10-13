"""Which links the book keeps: the generic rule of
:func:`papis_make_epub.sources.extraction.drop_non_content`."""

import pytest
from bs4 import BeautifulSoup

from papis_make_epub.sources.extraction import drop_non_content


@pytest.mark.parametrize(("href", "kept"), [
    ("#Fig1", True),
    ("https://link.springer.com/chapter/10.1007/x/figures/1", True),
    ("mailto:a@example.org", True),
    ("javascript:void(0)", False),
    # a browser save keeps the platform's site-relative links
    ("/chapter/10.1007/x/figures/1", False),
    ("figures/1", False),
    ("//link.springer.com/chapter/10.1007/x", False),
])
def test_link_kept(href: str, kept: bool) -> None:
    soup = BeautifulSoup(f'<p><a href="{href}"><img src="f.png"/></a></p>',
                         "lxml")
    p = soup.p
    assert p is not None
    drop_non_content(p)
    assert (p.a is not None) == kept
    assert p.img is not None
