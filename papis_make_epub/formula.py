"""One source formula and the formats it is available in.

A source hands each formula to the renderers as a Formula:
the DOM node to replace, the display position, and whichever formats
the source carries: presentation MathML, TeX, or both. The
accessors serve either format regardless of what was supplied,
converting through the texmath binary when needed (once per formula,
failures included), so a renderer declares what it consumes and
stays independent of what a source provides. The source fields stay
as supplied: a conversion can be lossy, and a renderer that can
consume either format reads the source's own.
"""

from dataclasses import dataclass
from functools import cached_property

from bs4 import BeautifulSoup, Tag

from papis_make_epub.texmath import mathml_to_tex, tex_to_mathml

MATHML = "mathml"
TEX = "tex"
ALL_FORMATS = frozenset({MATHML, TEX})

MATHML_NS = "http://www.w3.org/1998/Math/MathML"


def _declare_namespace(math: Tag) -> Tag:
    """*math* with the MathML namespace declared on it (texmath
    rejects undeclared MathML, and XHTML needs the declaration)."""
    if not math.get("xmlns"):
        math["xmlns"] = MATHML_NS
    return math


@dataclass
class Formula:
    """A formula's DOM node, position, and source formats."""

    # the element apply_math replaces with the rendered output
    node: Tag
    display: bool
    # presentation MathML element, when the source carries it
    mathml: Tag | None = None
    # TeX source, when the source carries it
    tex: str | None = None

    @cached_property
    def as_tex(self) -> str | None:
        """The TeX: the source's, or texmath's conversion of the
        source MathML; None when there is neither or the conversion
        fails."""
        if self.tex is not None:
            return self.tex
        if self.mathml is not None:
            return mathml_to_tex(str(_declare_namespace(self.mathml)))
        return None

    @cached_property
    def as_mathml(self) -> Tag | None:
        """The presentation MathML element, its namespace declared:
        the source's own, or texmath's conversion of the source TeX
        with the ``display`` attribute set from the formula's position
        (texmath marks every formula ``display="block"``); None when
        there is neither or the conversion fails."""
        if self.mathml is not None:
            return _declare_namespace(self.mathml)
        if self.tex is None:
            return None
        s = tex_to_mathml(self.tex)
        m = BeautifulSoup(s, "lxml").find("math") if s else None
        if not isinstance(m, Tag):
            return None
        _declare_namespace(m)
        m["display"] = "block" if self.display else "inline"
        return m

    def alt_text(self) -> str:
        """Readable identification for alt attributes and error
        messages: the TeX (:attr:`as_tex`), else the MathML's text,
        its annotations left out."""
        if self.as_tex is not None:
            return self.as_tex
        if self.mathml is not None:
            return " ".join(
                s.strip() for s in self.mathml.find_all(string=True)
                if s.strip() and s.find_parent(
                    ["annotation", "annotation-xml"]) is None)
        return ""

