"""Render MathML formulas for readers that don't support it.

Some HTML sources come with embedded MathML which is valid in EPUB 3,
but not all readers implement it. The following renderers replace each
formula's node with markup the target device can display: Unicode
text, images, or the MathML itself for capable readers:

- ``mathml``: the pass-through, keeping presentation MathML for
  capable readers.

- ``pylatexenc``: Unicode text with sub/sup markup, converted from
  the formula's TeX by the pylatexenc library (pure Python).

- ``mathtext``: render TeX with matplotlib mathtext, a lightweight
  TeX-subset engine; the Python-only image renderer. TeX comes from
  the source's annotations when present (arXiv/LaTeXML), otherwise via
  the texmath binary from the MathML. aligned environments render as
  one image per row and cases as a braced alternative list; other
  environments (arrays, matrices) are unsupported, use ``mathjax`` or
  ``lasem`` for those.

- ``mathjax``: SVG per formula, through a persistent mathjax-node
  co-process.

- ``lasem``: PNG per formula via lasem-render.

Renderers can be chosen separately for inline and display math
(e.g. pylatexenc to unicode for inline, and mathtext images for
display formulas).

"""

import atexit
import io
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterable, Set as AbstractSet
from dataclasses import dataclass
from logging import Logger
from typing import IO, Any, ClassVar

import matplotlib
from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString, PageElement
from matplotlib import mathtext as _mathtext
from pylatexenc import latex2text, latexwalker, macrospec

from papis_make_epub.errors import BuildError
from papis_make_epub.formula import (
    ALL_FORMATS,
    MATHML,
    MATHML_NS,
    TEX,
    Formula,
)
from papis_make_epub.logging import get_logger
from papis_make_epub.texmath import ENV, find_texmath

logger: Logger = get_logger(__name__)


class MathRenderError(BuildError):
    """A chosen math renderer could not render a formula."""


# formula identification length in error messages
_ALT_LIMIT = 80


def _formula_error(message: str, formula: "Formula") -> MathRenderError:
    """A MathRenderError naming the formula, alt text truncated."""
    alt = formula.alt_text()
    if len(alt) > _ALT_LIMIT:
        alt = alt[:_ALT_LIMIT] + "…"
    return MathRenderError(f"{message}: {alt!r}")


# aliases for macros absent from the engines' vocabularies, shared by
# the TeX renderers: pylatexenc applies it before its database,
# mathtext before its own shim table
_TEX_PRE = [
    (r"\\implies", r"\\Rightarrow "),
    (r"\\land\b", r"\\wedge "),
    (r"\\lor\b", r"\\vee "),
    (r"\\invamp\b", "⅋"),
    (r"\\left\.|\\right\.", ""),
    (r"\\left\b|\\right\b", ""),
]


def is_math_img(img: Tag) -> bool:
    """True for equation images created by apply_math (math-* classes).

    The class attribute is a list on parsed tags but a plain string on
    tags constructed by apply_math; normalize before matching.
    """
    cls = img.get("class")
    names = cls.split() if isinstance(cls, str) else (cls or [])
    return any(c.startswith("math-") for c in names)


class RendererUnavailableError(BuildError):
    """A renderer's external dependency is missing or unusable.

    Construction is the availability gate: an existing instance is a
    working renderer.
    """


@dataclass
class RenderContext:
    """Per-document state shared by all render calls: the document
    soup and the collected math media."""

    soup: BeautifulSoup
    math_media: list[tuple[str, bytes]]

    def add_media(self, data: bytes, ext: str) -> str:
        fn = f"images/eq{len(self.math_media):04}.{ext}"
        self.math_media.append((fn, data))
        return fn

    def img_for(self, fn: str, width_em: float | None, valign: str | None,
                display: bool, alt: str) -> Tag:
        tag = self.soup.new_tag("img", src=fn)
        tag["alt"] = alt
        style = ""
        if width_em is not None:
            style += f"width: {width_em:.2f}em;"
        if valign and not display:
            style += f" {valign};"
        if style:
            tag["style"] = style
        tag["class"] = "math-display" if display else "math-inline"
        return tag


class MathRenderer(ABC):
    """A math rendering engine.

    The constructor acquires the engine's own dependencies, raising
    RendererUnavailableError when they are missing; ``accepts`` names
    the math formats the engine consumes (make_renderer checks it
    against what the source supplies); render turns one Formula into
    its replacement node, registering media through the context.
    ``fallbacks`` is the engine's best-effort ladder and ``help`` its
    one-line description for the CLI.
    """

    name: ClassVar[str]
    accepts: ClassVar[frozenset[str]]
    help: ClassVar[str]
    # best-effort rungs strictly below this engine, tried in order
    # before the verbatim terminal
    fallbacks: ClassVar[tuple[str, ...]] = ("mathtext", "pylatexenc")

    @abstractmethod
    def render(self, formula: Formula, ctx: RenderContext) -> PageElement:
        """The formula's replacement node, its media registered
        through *ctx*; MathRenderError when the engine cannot render
        it."""


# non-MathML attributes on source math: ``is`` is a custom-element
# hook ScienceDirect emits as is="true" (not MathML at all); ``intent``
# is MathML 4 (LaTeXML), which EPUB 3.3's MathML 3 predates
_NONCONFORMING_MATH_ATTRS = ("is", "intent")


def _conform_mathml(node: Tag, display: bool) -> Tag:
    """Make a source MathML node valid for XHTML and explicit about
    its position: declare the MathML namespace, drop the
    non-conforming attributes, and state the position as ``display``
    in place of the ``mode`` that MathML 3 deprecates and MathML Core
    lacks."""
    if not node.get("xmlns"):
        node["xmlns"] = MATHML_NS
    for el in (node, *node.find_all(True)):
        for attr in _NONCONFORMING_MATH_ATTRS:
            if attr in el.attrs:
                del el[attr]
    if "mode" in node.attrs:
        del node["mode"]
    node["display"] = "block" if display else "inline"
    return node


class MathmlRenderer(MathRenderer):
    """The pass-through: presentation MathML kept as it is.

    Annotations (the source TeX) and alttext stay in: a conforming
    MathML renderer displays only the presentation child of semantics,
    and the annotations carry value for capable readers and tools.
    For a source carrying only TeX, the MathML is produced through
    texmath.
    """

    name = "mathml"
    accepts: ClassVar[frozenset[str]] = frozenset({MATHML})
    help = ("pass presentation MathML through, for readers that "
            "implement it")
    fallbacks: ClassVar[tuple[str, ...]] = ()

    def render(self, formula: Formula, ctx: RenderContext) -> PageElement:
        m = formula.as_mathml
        if m is None:
            raise _formula_error(
                "texmath could not produce MathML for formula", formula)
        return _conform_mathml(m, formula.display)


class PylatexencRenderer(MathRenderer):
    """Unicode text with sub/sup markup, converted from the formula's TeX.

    pylatexenc parses the TeX and maps macros to Unicode text. Custom
    conversion rules turn ``_``/``^`` scripts into <sub>/<sup>
    elements: the rules wrap each converted script argument in a
    private-use sentinel pair (out-of-band for both the text and the
    target XHTML), and _script_fragment decodes the marked string into
    elements. A formula whose TeX cannot be obtained or fully
    converted fails loudly, like every renderer; degradation is the
    best-effort cascade's job.
    """

    name = "pylatexenc"
    accepts: ClassVar[frozenset[str]] = frozenset({TEX})
    help = "Unicode text with sub/sup markup"
    fallbacks: ClassVar[tuple[str, ...]] = ()

    # sentinel pairs marking sub/superscript spans through the
    # string-typed pylatexenc conversion
    _SUB0, _SUB1, _SUP0, _SUP1 = "\ue000", "\ue001", "\ue002", "\ue003"

    # the (walker context, converter) pair built by _converter
    _converter_state: ClassVar[tuple[Any, Any] | None] = None

    @classmethod
    def _converter(cls) -> tuple[Any, Any]:
        """Walker context and converter with the script rules, built once.

        The walker context parses ``_`` and ``^`` as argument-taking
        specials; the converter rules wrap each converted argument in
        the matching sentinel pair.
        """
        if cls._converter_state is None:
            def sub_repl(n: Any, l2tobj: Any) -> str:
                arg: str = l2tobj.nodelist_to_text([n.nodeargd.argnlist[0]])
                return cls._SUB0 + arg + cls._SUB1

            def sup_repl(n: Any, l2tobj: Any) -> str:
                arg: str = l2tobj.nodelist_to_text([n.nodeargd.argnlist[0]])
                return cls._SUP0 + arg + cls._SUP1

            lw_db = latexwalker.get_default_latex_context_db()
            lw_db.add_context_category("scripts", prepend=True, specials=[
                macrospec.SpecialsSpec(
                    "_", args_parser=macrospec.MacroStandardArgsParser("{")),
                macrospec.SpecialsSpec(
                    "^", args_parser=macrospec.MacroStandardArgsParser("{")),
            ])
            l2t_db = latex2text.get_default_latex_context_db()
            l2t_db.add_context_category("scripts", prepend=True, specials=[
                latex2text.SpecialsTextSpec("_", simplify_repl=sub_repl),
                latex2text.SpecialsTextSpec("^", simplify_repl=sup_repl),
            ])
            converter = latex2text.LatexNodes2Text(latex_context=l2t_db,
                                                   math_mode="text")
            cls._converter_state = (lw_db, converter)
        return cls._converter_state

    @classmethod
    def _tex_to_text(cls, tex: str) -> str | None:
        """TeX to sentinel-marked text; None when not fully converted."""
        s = tex
        for pat, rep in _TEX_PRE:
            s = re.sub(pat, rep, s)
        lw_db, converter = cls._converter()
        try:
            nodes, _, _ = latexwalker.LatexWalker(
                s, latex_context=lw_db).get_latex_nodes()
            out = converter.nodelist_to_text(nodes).strip()
        except Exception:
            return None
        return out if out and "\\" not in out else None

    @classmethod
    def _script_fragment(cls, soup: BeautifulSoup, txt: str) -> PageElement:
        """Decode sentinel-marked text into a span with <sub>/<sup>."""
        if cls._SUB0 not in txt and cls._SUP0 not in txt:
            return NavigableString(txt)

        root = soup.new_tag("span")
        stack = [root]
        buf: list[str] = []

        def flush() -> None:
            if buf:
                stack[-1].append(NavigableString("".join(buf)))
                buf.clear()

        for ch in txt:
            if ch in {cls._SUB0, cls._SUP0}:
                flush()
                t = soup.new_tag("sub" if ch == cls._SUB0 else "sup")
                stack[-1].append(t)
                stack.append(t)
            elif ch in {cls._SUB1, cls._SUP1}:
                flush()
                if len(stack) > 1:
                    stack.pop()
            else:
                buf.append(ch)
        flush()
        return root

    def render(self, formula: Formula, ctx: RenderContext) -> PageElement:
        tex = formula.as_tex
        if tex is None:
            raise _formula_error(
                "pylatexenc renderer: no TeX obtainable for formula "
                "(texmath missing or failed)", formula)
        txt = self._tex_to_text(tex)
        if txt is None:
            raise _formula_error(
                "pylatexenc could not fully convert formula", formula)
        return self._script_fragment(ctx.soup, txt)


class MathtextRenderer(MathRenderer):
    """Images via matplotlib mathtext: the Python-only image renderer.

    mathtext is matplotlib's "lightweight TeX expression parser and
    layout engine", supporting a subset of TeX markup. This renderer
    wraps that engine with rewrites bridging source TeX to the
    subset: the notation shims below and the layout-environment
    handling in _tex_to_lines. Both are heuristic, derived from a
    sample of documents, and neither complete nor fixed; the renderer
    exists to render math as images without tools outside Python.
    """

    name = "mathtext"
    accepts: ClassVar[frozenset[str]] = frozenset({TEX})
    help = ("images rendered from TeX (source annotations when "
            "present, else via the texmath binary)")
    fallbacks: ClassVar[tuple[str, ...]] = ("pylatexenc",)

    # additional rewrites so matplotlib mathtext accepts
    # LaTeXML/texmath TeX. Deliberately limited to notation shims (a
    # symbol or spacing macro mathtext lacks); source macros that
    # need semantic reshaping (\verb, mathpar's \inferrule) are out
    # of scope; documents carrying them build with --math-display
    # mathjax instead, which renders the already-expanded MathML and
    # has no TeX-dialect problem.
    _MATHTEXT_PRE: ClassVar = [
        (r"\\definecolor\[named\]\{[^}]*\}\{[^}]*\}\{[^}]*\}", ""),
        (r"\\color\[rgb\]\{[^}]*\}", ""),
        (r"\\text\{([^{}]*)\}", r"\\mathrm{\1}"),
        # mathtext rejects empty \frac arguments (axiom rules)
        (r"\\frac\{\}", r"\\frac{\\,}"),
        # symbols mathtext lacks whose Unicode DejaVu covers
        (r"\\square\b|\\Box\b", "□"),
        (r"\\texttt\b", r"\\mathtt"),
        # mathtext has no \middle; the bare delimiter renders fine
        (r"\\middle\b", ""),
        # semantic brackets (U+301A/B): mathtext's fonts lack the glyphs
        ("〚", r"[\\![ "),
        ("〛", r" ]\\!]"),
        (r"\\textit\b", r"\\mathit"),
        # mathtext has no \big-family sizing; the bare delimiter
        # renders fine
        (r"\\[Bb]igg?[lrm]?\b", ""),
        (r"\\mathop\b", ""),
        (r"\\par\b", " "),
        (r"\\iff\b", r"\\Leftrightarrow "),
    ]

    # matplotlib's default font size in points, the em reference
    _FONT_PT = 10.0

    # layout environments: cases and aligned are rewritten; anything
    # else, arrays and matrices included, is unsupported
    _CASES_RE: ClassVar = re.compile(
        r"\\begin\{cases\}"
        r"(?P<body>(?:(?!\\begin\{cases\}|\\end\{cases\}).)*)"
        r"\\end\{cases\}",
        re.S)
    _ALIGNED_RE: ClassVar = re.compile(
        r"\\begin\{aligned\}"
        r"(?P<body>(?:(?!\\begin\{aligned\}|\\end\{aligned\}).)*)"
        r"\\end\{aligned\}",
        re.S)
    # a row holding only spacing renders to an empty (unusable) image
    _SPACING_ONLY: ClassVar = re.compile(r"(?:\\[,;!:]|\\q?quad\b|\\ |[{}\s~])*")

    @staticmethod
    def _split_top_level(s: str, sep: str) -> list[str]:
        """Split on ``&`` or ``\\\\`` at brace and environment depth zero.

        Escaped characters (``\\&``, ``\\{``, ``\\}``) are literal TeX,
        not structure; any backslash pair is consumed whole so the
        escaped character is never classified.
        """
        parts: list[str] = []
        cur: list[str] = []
        depth = i = 0
        while i < len(s):
            c = s[i]
            if s.startswith("\\begin{", i):
                depth += 1
            elif s.startswith("\\end{", i):
                depth -= 1
            elif c == "\\":
                if depth == 0 and sep == "\\\\" and s.startswith("\\\\", i):
                    parts.append("".join(cur))
                    cur = []
                else:
                    cur.append(s[i:i + 2])
                i += 2
                continue
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            if depth == 0 and sep == "&" and c == "&":
                parts.append("".join(cur))
                cur = []
                i += 1
                continue
            cur.append(c)
            i += 1
        parts.append("".join(cur))
        return parts

    @classmethod
    def _body_rows(cls, body: str) -> list[str]:
        """Rows of a cases/aligned body, alignment points as thin spaces."""
        return [r"\ ".join(c.strip()
                           for c in cls._split_top_level(r, "&")
                           if c.strip())
                for r in cls._split_top_level(body, "\\\\")]

    @classmethod
    def _tex_to_lines(cls, tex: str) -> list[str]:
        """Rewrite one formula's TeX into the lines to render.

        Usually the formula itself. cases environments (piecewise
        definitions) are rewritten to a left brace with ;-separated
        alternatives. A formula that is entirely one aligned
        environment (a multi-line derivation) yields one line per
        non-empty row; an aligned nested inside a larger expression
        joins its rows side by side. Any other environment (arrays,
        matrices) raises MathRenderError: mathtext has no 2D layout.
        """
        s = re.sub(r"\\mspace\{[^{}]*\}", " ", tex.strip())
        # mathtext has no cases
        while (cm := cls._CASES_RE.search(s)) is not None:
            alternatives = r";\ \ ".join(
                r for r in cls._body_rows(cm.group("body")) if r)
            s = s[:cm.start()] + r"\{ {" + alternatives + "}" + s[cm.end():]
        # aligned rows are the lines of a multi-line derivation. The
        # tempered pattern matches an innermost environment first, so
        # a nested aligned joins side by side into its row before the
        # enclosing one becomes matchable; an aligned spanning the
        # whole formula stacks, one line per non-empty row
        while (am := cls._ALIGNED_RE.search(s)) is not None:
            if am.start() == 0 and am.end() == len(s):
                lines: list[str] = []
                for row in cls._body_rows(am.group("body")):
                    if row and not cls._SPACING_ONLY.fullmatch(row):
                        lines.extend(cls._tex_to_lines(row))
                if not lines:
                    raise MathRenderError(
                        "aligned environment with no content")
                return lines
            joined = r"\ \qquad ".join(
                r for r in cls._body_rows(am.group("body")) if r)
            s = s[:am.start()] + "{" + joined + "}" + s[am.end():]
        if (env := re.search(r"\\begin\{([^{}]*)\}", s)) is not None:
            raise MathRenderError(
                f"mathtext has no 2D layout for {env.group(1)!r} "
                "environments; render this document with mathjax or lasem")
        return [s]

    @classmethod
    def _render_tex_png(cls, tex: str,
                        dpi: int = 300) -> tuple[bytes, float] | None:
        """Render TeX to PNG with matplotlib mathtext.

        Returns the PNG and its natural width in em (relative to the
        mathtext font size) so the EPUB can size it to scale with the
        reader's font size. None on failure.
        """
        for pat, rep in _TEX_PRE + cls._MATHTEXT_PRE:
            tex = re.sub(pat, rep, tex)
        # annotations keep the source's line breaks and mathtext's
        # parser rejects newlines inside a formula; math-mode TeX
        # reads any whitespace run as one space, so collapsing loses
        # nothing
        tex = " ".join(tex.split())

        buf = io.BytesIO()
        try:
            _mathtext.math_to_image(f"${tex}$", buf, dpi=dpi, format="png")
        except Exception:
            return None

        png = buf.getvalue()
        # bytes 16-20 are the IHDR width field of any PNG
        width_px = int.from_bytes(png[16:20], "big")
        em_px = cls._FONT_PT * dpi / 72.0
        return png, width_px / em_px

    def render(self, formula: Formula, ctx: RenderContext) -> PageElement:
        alt = formula.alt_text()
        display = formula.display
        tex = formula.as_tex
        if tex is None:
            if find_texmath() is None:
                raise _formula_error(
                    "mathtext renderer: formula has no TeX and "
                    "texmath is not on PATH", formula)
            raise _formula_error(
                "texmath could not convert formula", formula)
        try:
            lines = self._tex_to_lines(tex)
        except MathRenderError as exc:
            raise MathRenderError(f"{exc} (formula {alt[:80]!r})") from None
        rendered: list[tuple[bytes, float]] = []
        for line in lines:
            image = self._render_tex_png(line)
            if image is None:
                raise MathRenderError(
                    f"mathtext could not render {line[:80]!r} "
                    f"(from formula {alt[:60]!r})")
            rendered.append(image)
        if len(rendered) == 1:
            png, width_em = rendered[0]
            return ctx.img_for(ctx.add_media(png, "png"), width_em,
                               None, display, alt)
        # multiple lines are the rows of an aligned environment: block
        # images stacked in display position, inline images flowing in
        # inline position
        span = ctx.soup.new_tag("span")
        for png, width_em in rendered:
            span.append(ctx.img_for(ctx.add_media(png, "png"),
                                    width_em, None, display, alt))
        return span


def _stop(proc: subprocess.Popen[str]) -> None:
    """End a co-process: its input closed, the child terminated and
    reaped, killed if it ignores the termination, and its output
    closed last, once the child's exit has ended any pending read of
    it (see :class:`_LineReader`)."""
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except OSError:  # a dead child: nothing left to flush to
            pass
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    if proc.stdout is not None:
        proc.stdout.close()


class _LineReader:
    """The lines of a pipe, read on a thread so that a wait for the
    next one can time out on every platform (a pipe cannot be polled
    with a timeout portably)."""

    def __init__(self, pipe: IO[str]) -> None:
        self._lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, args=(pipe,), daemon=True).start()

    def _pump(self, pipe: IO[str]) -> None:
        try:
            for line in pipe:
                self._lines.put(line)
        except (OSError, ValueError):  # the pipe closed under the read
            pass
        self._lines.put("")  # the EOF marker, as readline's

    def readline(self, timeout: float) -> str | None:
        """The next line, "" at EOF, None when none arrived in time."""
        try:
            return self._lines.get(timeout=timeout)
        except queue.Empty:
            return None


class MathjaxRenderer(MathRenderer):
    """SVG per formula via a persistent mathjax-node co-process.

    Consumes either format: MathML is preferred when the source has it
    (arXiv annotations carry unexpanded author macros, while the
    MathML is the expanded ground truth); TeX-only formulas are
    typeset from the TeX directly, MathJax's native input. The child
    prints a ready line, then reads one JSON request ({math, format})
    per line and answers one JSON-encoded SVG (or null) per line, in
    order. It is started at construction, whose success is the
    availability check, and kept for the lifetime of the instance, so
    one node startup serves any number of documents.
    """

    name = "mathjax"
    accepts: ClassVar[frozenset[str]] = frozenset({MATHML, TEX})
    help = "SVG via mathjax-node"
    _DRIVER = os.path.join(os.path.dirname(__file__), "run_mathjax.js")
    # seconds allowed for the ready line, and for each answer
    _READY_TIMEOUT = 60.0
    _RENDER_TIMEOUT = 120.0

    def __init__(self) -> None:
        node = shutil.which("node")
        if node is None:
            raise RendererUnavailableError(
                "mathjax renderer needs node on PATH")
        self._node = node
        self._proc: subprocess.Popen[str] | None = None
        self._lines: _LineReader | None = None
        atexit.register(self._close)
        self._proc, self._lines = self._spawn()

    def _spawn(self) -> tuple[subprocess.Popen[str], _LineReader]:
        """The driver started and confirmed up by its ready line, with
        the reader of its answers."""
        proc = subprocess.Popen(
            [self._node, self._DRIVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", env=ENV)
        assert proc.stdout is not None
        lines = _LineReader(proc.stdout)
        line = lines.readline(self._READY_TIMEOUT)
        if line is None or line.strip() != "ready":
            _stop(proc)
            raise RendererUnavailableError(
                "the mathjax-node driver did not start; it needs the "
                "mathjax-node library resolvable by node (NODE_PATH)")
        return proc, lines

    @staticmethod
    def _svg_sized(svg: str) -> tuple[bytes, float | None, str | None]:
        """The SVG with its root width and height in px, its width in
        em, and the root's style (MathJax's vertical-align).

        Renderers that cannot resolve ex on an SVG root get no
        intrinsic size or aspect ratio, and the drawing is clipped to
        a wrong box (observed on an e-ink reader). Only parseability
        and the ratio matter (the display size comes from the img's
        em width), so 1ex is taken as 8px, half of a 16px em.
        """
        doc = BeautifulSoup(svg, "xml")
        root = doc.find("svg")
        if not isinstance(root, Tag):
            return svg.encode("utf-8"), None, None
        for attr in ("width", "height"):
            value = root.get(attr)
            if isinstance(value, str) and value.endswith("ex"):
                root[attr] = f"{float(value[:-2]) * 8:.1f}px"
        width = root.get("width")
        width_em = (float(width[:-2]) / 16.0
                    if isinstance(width, str) and width.endswith("px")
                    else None)
        style = root.get("style")
        valign = (style.rstrip("; ")
                  if isinstance(style, str) and "vertical-align" in style
                  else None)
        return str(doc).encode("utf-8"), width_em, valign

    def _close(self) -> None:
        if self._proc is not None:
            _stop(self._proc)
            self._proc = None
            self._lines = None

    def _child(self) -> tuple[subprocess.Popen[str], _LineReader]:
        if (self._proc is None or self._lines is None
                or self._proc.poll() is not None):
            self._proc, self._lines = self._spawn()
        return self._proc, self._lines

    def render(self, formula: Formula, ctx: RenderContext) -> PageElement:
        alt = formula.alt_text()
        display = formula.display
        request: dict[str, str | None]
        if formula.mathml is not None:
            request = {"math": str(formula.as_mathml), "format": "MathML"}
        else:
            request = {"math": formula.as_tex,
                       "format": "TeX" if display else "inline-TeX"}
        proc, lines = self._child()
        assert proc.stdin is not None
        try:
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
        except OSError as exc:
            self._close()
            raise RendererUnavailableError(
                "mathjax driver died (broken pipe)") from exc
        # strict request/response, so at most one reply is pending;
        # the timeout guards against a hung child
        line = lines.readline(self._RENDER_TIMEOUT)
        if line is None:
            self._close()
            raise RendererUnavailableError(
                f"mathjax driver timed out on formula: {alt[:80]!r}")
        if not line:
            self._close()
            raise RendererUnavailableError("mathjax driver died (EOF)")
        result = json.loads(line)
        if result is None:
            raise _formula_error("mathjax could not render formula",
                                 formula)
        svg, width_em, valign = self._svg_sized(result)
        return ctx.img_for(ctx.add_media(svg, "svg"), width_em, valign,
                           display, alt)


class LasemRenderer(MathRenderer):
    """PNG per formula via lasem-render.

    lasem lays out stretchy operators through the TeX Computer Modern
    fonts (cmr10/cmmi10/cmsy10/cmex10) at their legacy glyph
    positions; when fontconfig substitutes an ordinary font, those
    positions render as stray Latin letters. The renders therefore
    run under a private fontconfig over the BaKoMa copies bundled
    with matplotlib (rejecting the LastResortHE face bundled
    alongside, which claims every codepoint as a block-placeholder
    glyph and would hijack ordinary operators), independent of the
    system's fonts.
    """

    name = "lasem"
    accepts: ClassVar[frozenset[str]] = frozenset({MATHML})
    help = "PNG via lasem-render"

    # assumed MathML base font size for em sizing of renders, points
    _FONT_PT = 12.0

    def __init__(self) -> None:
        binary = self._find()
        if binary is None:
            raise RendererUnavailableError(
                "lasem renderer needs lasem-render on PATH")
        self._binary = binary
        self._env: dict[str, str] | None = None
        # the private fontconfig and its cache, for the instance's life
        self._fc_dir: tempfile.TemporaryDirectory[str] | None = None

    @staticmethod
    def _find() -> str | None:
        for name in ("lasem-render", "lasem-render-0.6", "lasem-render-0.4"):
            p = shutil.which(name)
            if p:
                return p
        return None

    def _render_png(self, mml: str,
                    ppi: int = 300) -> tuple[bytes, float] | None:
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "m.mml")
            out = os.path.join(td, "m.png")
            with open(src, "w", encoding="utf-8") as f:
                f.write(mml)
            r = subprocess.run(
                [self._binary, "-f", "png", "-p", str(ppi), "-o", out, src],
                capture_output=True, timeout=60, env=self._render_env(),
                check=False)
            if r.returncode != 0 or not os.path.isfile(out):
                return None
            with open(out, "rb") as f:
                png = f.read()
        # bytes 16-20 are the IHDR width field of any PNG
        width_px = int.from_bytes(png[16:20], "big")
        em_px = self._FONT_PT * ppi / 72.0
        return png, width_px / em_px

    def _render_env(self) -> dict[str, str]:
        if self._env is not None:
            return self._env
        font_dir = os.path.join(matplotlib.get_data_path(),
                                "fonts", "ttf")
        self._fc_dir = tempfile.TemporaryDirectory(
            prefix="papis-make-epub-lasem-fc-")
        atexit.register(self._fc_dir.cleanup)
        td = self._fc_dir.name
        os.mkdir(os.path.join(td, "cache"))
        fc = os.path.join(td, "fonts.conf")
        with open(fc, "w", encoding="utf-8") as f:
            f.write(f"""\
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <dir>{font_dir}</dir>
  <cachedir>{os.path.join(td, "cache")}</cachedir>
  <selectfont><rejectfont>
    <glob>*/LastResortHE-Regular.ttf</glob>
  </rejectfont></selectfont>
</fontconfig>
""")
        self._env = dict(ENV, FONTCONFIG_FILE=fc)
        return self._env

    def render(self, formula: Formula, ctx: RenderContext) -> PageElement:
        mml = formula.as_mathml
        if mml is None:
            raise _formula_error(
                "texmath could not produce MathML for formula", formula)
        out = self._render_png(str(mml))
        if out is None:
            raise _formula_error("lasem could not render formula", formula)
        png, width_em = out
        return ctx.img_for(ctx.add_media(png, "png"), width_em, None,
                           formula.display, formula.alt_text())


_RENDERER_CLASSES: list[type[MathRenderer]] = [
    MathmlRenderer,
    PylatexencRenderer,
    MathtextRenderer,
    MathjaxRenderer,
    LasemRenderer,
]
RENDERERS: dict[str, type[MathRenderer]] = {c.name: c for c in _RENDERER_CLASSES}


def make_renderer(name: str, *,
                  math_formats: AbstractSet[str] = ALL_FORMATS) -> MathRenderer:
    """Construct the named renderer; construction is the availability gate.

    *math_formats* is what the source supplies per formula (see
    :mod:`papis_make_epub.formula`); a renderer consuming none of them
    is unavailable unless texmath can convert between the formats.
    """
    try:
        cls = RENDERERS[name]
    except KeyError:
        raise ValueError(f"unknown math renderer {name!r}; choose from "
                         f"{', '.join(RENDERERS)}") from None
    if not (cls.accepts & math_formats) and find_texmath() is None:
        raise RendererUnavailableError(
            f"{name} renderer consumes {'/'.join(sorted(cls.accepts))}, the "
            f"source supplies {'/'.join(sorted(math_formats))}, and the "
            "texmath converter is not on PATH")
    return cls()


def _fallback_chain(name: str) -> list[MathRenderer]:
    """The usable fallback renderers below *name* (its ``fallbacks``).

    Fallbacks are constructed without the source-format gate: an
    unusable rung is skipped, so a best-effort build still produces a
    book.
    """
    chain: list[MathRenderer] = []
    for n in RENDERERS[name].fallbacks:
        try:
            chain.append(RENDERERS[n]())
        except RendererUnavailableError as exc:
            logger.info("best-effort: %s rung unavailable (%s)", n, exc)
    return chain


def _verbatim(formula: Formula) -> PageElement:
    """The total terminal: the formula kept as the source had it.

    MathML passes through with annotations stripped (they
    double-render on the readers that need fallbacks in the first
    place); a TeX-only formula becomes its text. The strip happens
    on a copy: the Formula's own node keeps its annotations for
    later accessors.
    """
    if formula.mathml is not None:
        copy = BeautifulSoup(str(formula.mathml), "lxml").find("math")
        if isinstance(copy, Tag):
            for a in copy.find_all(["annotation", "annotation-xml"]):
                a.decompose()
            return copy
    return NavigableString(formula.tex or "")


def _render_best_effort(renderer: MathRenderer, formula: Formula,
                        ctx: RenderContext,
                        chains: dict[str, list[MathRenderer]]) -> PageElement:
    if renderer.name not in chains:
        chains[renderer.name] = _fallback_chain(renderer.name)
    alt = formula.alt_text()
    for fb in chains[renderer.name]:
        logger.warning("best-effort: trying %s on formula %r",
                       fb.name, alt[:60])
        try:
            return fb.render(formula, ctx)
        except MathRenderError as exc:
            logger.warning("best-effort: %s also failed (%s)", fb.name, exc)
    logger.warning("best-effort: keeping formula %r verbatim", alt[:60])
    return _verbatim(formula)


def apply_math(formulas: Iterable[Formula], inline_renderer: MathRenderer,
               display_renderer: MathRenderer, ctx: RenderContext, *,
               best_effort: bool = False) -> tuple[int, int]:
    """Render every formula, replacing its node in the document.

    A MathRenderError from the selected renderer propagates and
    fails the build; with *best_effort* rendering instead falls down
    the fallback ladder to a verbatim terminal that cannot fail,
    each degradation logged with the formula.
    RendererUnavailableError propagates in every mode.
    Returns (formulas rendered, formulas degraded).
    """
    chains: dict[str, list[MathRenderer]] = {}
    rendered = degraded = 0
    for f in formulas:
        rendered += 1
        renderer = display_renderer if f.display else inline_renderer
        try:
            replacement = renderer.render(f, ctx)
        except MathRenderError as exc:
            if not best_effort:
                raise
            logger.warning("best-effort: %s failed: %s", renderer.name, exc)
            replacement = _render_best_effort(renderer, f, ctx, chains)
            degraded += 1
        if replacement is not f.node:
            f.node.replace_with(replacement)
    return rendered, degraded
