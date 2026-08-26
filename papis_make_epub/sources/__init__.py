"""One module per source platform, listed here.

Each platform module defines a Source subclass holding the platform's
whole identity: the ``--from`` key, the display name, the math
formats the source supplies, the detector, and the extraction.
SOURCES lists one instance of each; the order is the ``--from``
choice order. What the platform modules share is in
:mod:`papis_make_epub.sources.extraction`.
"""

from papis_make_epub.sources.acm import AcmSource
from papis_make_epub.sources.arxiv import ArxivSource
from papis_make_epub.sources.extraction import Source
from papis_make_epub.sources.ieee import IeeeSource
from papis_make_epub.sources.sciencedirect import ScienceDirectSource
from papis_make_epub.sources.springer import SpringerSource

SOURCES: list[Source] = [
    AcmSource(),
    ArxivSource(),
    IeeeSource(),
    ScienceDirectSource(),
    SpringerSource(),
]

__all__ = ["SOURCES", "Source"]
