"""Build a renderer-comparison EPUB for evaluating math on a device.

One section per renderer (mathml, pylatexenc, mathtext, mathjax, lasem),
each rendering an identical set of inline and display formulas, so the
renderers can be compared side by side on the target e-reader. Sources
are LaTeXML-shaped math elements (presentation MathML from texmath,
with the TeX kept in an annotation), processed by the same apply_math
pipeline the real builds use.

Run with the ``papis-make-epub-demo`` console script or
``python -m papis_make_epub.math_rendering_demo``; it needs the
tooling of all five renderers at once (texmath, mathjax-node,
lasem-render).
"""

import sys
from html import escape

import click
from bs4 import BeautifulSoup, Tag

from papis_make_epub.book import BuildOptions, DocMeta, write_epub
from papis_make_epub.formula import Formula
from papis_make_epub.math_renderers import (
    RENDERERS,
    MathRenderer,
    RenderContext,
    RendererUnavailableError,
    apply_math,
    make_renderer,
)
from papis_make_epub.sources.extraction import BuildStats, Content, Part, TocEntry
from papis_make_epub.texmath import find_texmath, tex_to_mathml

INLINE = [
    r"x_{i}^{2}+\alpha\beta",
    r"\forall x.\ P(x)\Rightarrow Q(x)",
    r"\Gamma\vdash e:\tau",
]

DISPLAY = [
    r"x=\frac{-b\pm\sqrt{b^{2}-4ac}}{2a}",
    r"\frac{\Gamma\vdash e_{1}:\tau_{1}\quad\Gamma\vdash e_{2}:\tau_{2}}"
    r"{\Gamma\vdash (e_{1},e_{2}):\tau_{1}\times\tau_{2}}",
    r"f(x)=\begin{cases}1&x>0\\0&\text{otherwise}\end{cases}",
    r"\begin{aligned}(a+b)^{2}&=a^{2}+2ab+b^{2}\\&\geq 4ab\end{aligned}",
]


def _annotated_mathml(tex: str, display: bool) -> str:
    """Presentation MathML for *tex* with the TeX kept LaTeXML-style."""
    mml = tex_to_mathml(tex)
    if mml is None:
        sys.exit(f"texmath could not convert {tex!r}")
    soup = BeautifulSoup(mml, "lxml")
    m = soup.find("math")
    if not isinstance(m, Tag):
        sys.exit(f"texmath produced no <math> for {tex!r}")
    m["display"] = "block" if display else "inline"
    sem = soup.new_tag("semantics")
    for c in list(m.children):
        sem.append(c.extract())
    ann = soup.new_tag("annotation", encoding="application/x-tex")
    ann.string = tex
    sem.append(ann)
    m.append(sem)
    return str(m)


def _sample_html() -> str:
    parts = ["<div>", "<h2>Inline</h2>"]
    for tex in INLINE:
        parts.append(f"<p><code>{escape(tex)}</code></p>")
        parts.append(f"<p>In context: the expression {_annotated_mathml(tex, False)}"
                     " appears mid-sentence to judge baseline and size.</p>")
    parts.append("<h2>Display</h2>")
    for tex in DISPLAY:
        parts.append(f"<p><code>{escape(tex)}</code></p>")
        parts.append(_annotated_mathml(tex, True))
    parts.append("</div>")
    return "".join(parts)


def build_demo(output: str) -> dict[str, tuple[int, int, int]]:
    """Build the comparison EPUB at ``output``.

    Returns per-renderer (rendered, degraded, images) counts. Raises
    :class:`RendererUnavailableError` when texmath (which generates
    the MathML sources) or a renderer's tooling is absent.
    """
    if find_texmath() is None:
        raise RendererUnavailableError(
            "texmath not on PATH (needed to generate the MathML sources)")
    # constructing every renderer is this tool's availability gate
    renderers: dict[str, MathRenderer] = {
        mode: make_renderer(mode) for mode in RENDERERS
    }

    source = _sample_html()

    meta = DocMeta(title="papis-make-epub math rendering demo",
                   authors=[],
                   identifier="papis-make-epub-math-rendering-demo")

    math_media: list[tuple[str, bytes]] = []
    doc_parts: list[Part] = []
    stats: dict[str, tuple[int, int, int]] = {}
    for n, (mode, renderer) in enumerate(renderers.items(), 1):
        soup = BeautifulSoup(source, "lxml")
        root = soup.find("div")
        assert isinstance(root, Tag)  # _sample_html always emits the div

        def formula(m: Tag) -> Formula:
            ann = m.find("annotation", encoding="application/x-tex")
            tex = (ann.get_text(strip=True) or None) if ann else None
            return Formula(node=m, mathml=m, tex=tex,
                           display=m.get("display") == "block")

        ctx = RenderContext(soup=soup, math_media=math_media)
        before = len(math_media)
        rendered, degraded = apply_math(
            [formula(m) for m in root.find_all("math")],
            renderer, renderer, ctx)
        images = len(math_media) - before

        doc_parts.append(Part(
            f'<h2 id="mode-{mode}">{n}. {mode}</h2>'
            f"<p>{rendered} formulas, "
            f"{degraded} fallbacks, "
            f"{images} images.</p>"
            + root.decode_contents(),
            entries=[TocEntry(f"{n}. {mode}", f"mode-{mode}")]))
        stats[mode] = (rendered, degraded, images)

    # the per-renderer counts above are this tool's report; the
    # content's own stats stay at their zero defaults
    content = Content(parts=doc_parts, media=math_media,
                      stats=BuildStats(), language="en")
    content.add_title_page(meta.title, meta.authors)
    write_epub(output, meta=meta, content=content, options=BuildOptions())
    return stats


@click.command()
@click.option("-o", "--output", default="math-rendering-demo.epub",
              type=click.Path(dir_okay=False, writable=True),
              help="Where to write the comparison EPUB.")
def main(output: str) -> None:
    """Build an EPUB rendering the same formulas through all renderers."""
    try:
        stats = build_demo(output)
    except RendererUnavailableError as exc:
        sys.exit(str(exc))
    for mode, (rendered, degraded, images) in stats.items():
        click.echo(f"{mode}: {rendered} formulas, {degraded} fallbacks, "
                   f"{images} images")
    click.echo(f"wrote {output}")


if __name__ == "__main__":
    main()
