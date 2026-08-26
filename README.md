# papis-make-epub

Build EPUB renditions of articles in your
[Papis](https://github.com/papis/papis) library. Current sources
include fetching arXiv's LaTeXML-generated HTML or using downloaded
HTML pages from publishers' websites (see [Sources](#sources)). Math
is often served as MathML, and sometimes as MathJax/LaTeX, which can
optionally be rendered at build time for readers that don't render
math themselves (see [Math Rendering](#math-rendering)).

## Installation

Install papis-make-epub in the same Python environment as papis, and
it registers the `papis make-epub` command as a plugin:

    pip install papis-make-epub

Installed elsewhere, the package also works through papis's external
command mechanism: papis runs any `papis-make-epub` script found on
`PATH` as `papis make-epub`.

Some of the optional math renderers call external programs: `node`
with the `mathjax-node` library resolvable through `NODE_PATH` for
the `mathjax` renderer, `lasem-render` for the `lasem` renderer, and
the `texmath` executable whenever the selected renderer needs a
conversion between TeX and MathML (see
[Math Rendering](#math-rendering)). Make sure the ones you use are
on `PATH`.

### Nix

For Nix users, everything above comes as one package: papis with
the plugin and the renderers' programs on its `PATH`. Try it out
without installing:

    nix run github:jabarszcz/papis-make-epub -- --help

To install, add the flake as an input to a NixOS or home-manager
configuration:

```nix
inputs.papis-make-epub.url = "github:jabarszcz/papis-make-epub";
```

and install the package:

```nix
environment.systemPackages = [  # home.packages for home-manager
  inputs.papis-make-epub.packages.${pkgs.system}.papis-with-make-epub
];
```

For other compositions (more plugins, only some of the programs),
adapt the recipe in [`nix/default.nix`](nix/default.nix).

Without flakes, fetch the source and import [`nix/`](nix/), which
composes the same wrapped papis:

```nix
let
  papis-make-epub = fetchTarball
    "https://github.com/jabarszcz/papis-make-epub/archive/main.tar.gz";
in {
  environment.systemPackages = [  # home.packages for home-manager
    (import "${papis-make-epub}/nix" { inherit pkgs; })
  ];
}
```

## Basic Usage

### 1. Manually download the HTML page for the article

The article should already be in your Papis library. An arXiv paper
needs no download: its HTML rendition is fetched at the next step. For
the other sources, you'll first want to manually download the HTML
page with the article contents, as headless downloads are often
blocked: save it from the browser ("Save Page As, Web Page complete").

MathJax version 3 consumes the MathML when it runs, replacing it with
its rendered output, so on a platform running it (sciencedirect.com
currently) keep MathJax from running: block its script before loading
the page (devtools → Network → right-click the mathjax request →
"Block request URL", then reload).

To get a single file without the `_files` folder, capture with
[SingleFile](https://github.com/gildas-lormeau/SingleFile) instead,
which the [re-capture script](#goldens-and-re-capture) also uses.
Disable its script blocking (under Network) to keep the TeX that
MathJax v2 leaves in `<script>` elements (IEEE Xplore, Springer).

### 2. Convert it to EPUB

Convert and add the EPUB to your Papis document, identified by a
query. The book's metadata comes from the document's `info.yaml`.

    papis make-epub <query> saved-page.html

A page already among the document's files (e.g. added with
`papis addto`) is picked up automatically, and the HTML rendition
of the arXiv paper named by the metadata (`eprint`, or an
arxiv.org URL) is fetched, so both build without a file argument:

    papis make-epub <query>

## Sources

The EPUB is translated from a full-text HTML rendition served by
the article's publisher (not every article has one). We handle
each platform's markup in its own module under
[`papis_make_epub/sources/`](papis_make_epub/sources/). The source is
automatically detected from the page markup, and the `--from` flag can
be used if ever that fails.

| `--from`        | Sites                          | Math         |
|-----------------|--------------------------------|--------------|
| `arxiv`         | arxiv.org                      | MathML + TeX |
| `acm`           | dl.acm.org (`/doi/full/` page) | MathML       |
| `springer`      | link.springer.com, nature.com  | TeX          |
| `ieee`          | ieeexplore.ieee.org            | TeX          |
| `sciencedirect` | sciencedirect.com              | MathML       |

The Math column names what the page carries per formula: MathML,
or the TeX the platform serves for MathJax to render; arXiv's
MathML also carries the TeX, as annotations.
[Math Rendering](#math-rendering) covers how either is rendered.

Potential future sources:
- running LaTeXML ourselves when a LaTeX version is available;
- doing OCR or VLMs/agentic extraction from PDFs.

## Math Rendering

E-readers differ in what math they can display, so rendering can be
pre-baked (choose the renderer with the `--math`, `--math-inline` or
`--math-display` flags).

| Renderer     | Consumes      | Output                    | Program                      |
|--------------|---------------|---------------------------|------------------------------|
| `mathml`     | MathML        | MathML                    | none                         |
| `pylatexenc` | TeX           | Unicode with sub/sup tags | none                         |
| `mathtext`   | TeX           | PNG (matplotlib)          | none                         |
| `mathjax`    | MathML or TeX | SVG                       | `node` (with `mathjax-node`) |
| `lasem`      | MathML        | PNG                       | `lasem-render`               |

A source supplies each formula as TeX or MathML (the Math column
in the Sources table); a renderer consuming the other form needs
the `texmath` program, which converts each formula, and could
fail on some or be lossy.

The default renderer, `mathml`, passes the MathML through,
leaving the rendering to the reading device; every other renderer
trades something away (text flattens layout, images fix the font
and size). `papis-make-epub-demo` builds a comparison EPUB, one
section per renderer over identical formulas, for judging the
renderers on a device.

The build fails on the first formula the selected renderer
cannot render. With `--best-effort` it continues: rendering falls
back along `mathjax`/`lasem` → `mathtext` → `pylatexenc` → verbatim
from the selected renderer down.

## Testing

Publisher pages change, and there is no simple check that an EPUB
is the right rendition of a page; the suite therefore leans on
characterization tests: goldens pin the current output, cheap to
keep, at the cost of reviewing their diffs once in a while.
epubcheck validates each built EPUB (content-model errors already in
the publishers' own markup are tolerated, their count pinned in a
golden).

### Running

To test, run `pytest -rs`; `-rs` lists the tests skipped for
missing programs. The dev shell (`nix develop`) provides all of
them and makes the checkout importable, so plain `pytest` is
enough. Outside it, install the checkout in editable mode first
(`pip install --editable . --group dev`). `nix flake check` runs the
whole test matrix: the suite once per supported interpreter and
once against papis's development branch, no skips, plus ruff and
mypy. One cell runs alone with
`nix build .#checks.x86_64-linux.test-python312`.

### Fixtures

The tests run on committed pages, whole captures and reductions,
listed in [`tests/fixtures/manifest.toml`](tests/fixtures/manifest.toml)
with each one's URL and capture date.

We only commit CC BY whole captures, and their size adds up, so
there are one or two per platform, re-captured to follow what the
platforms serve. Each also has its bibliographic record in the
fixtures' papis library (`tests/fixtures/library`), which
`tests/fixtures/papis-fixtures` runs papis against with the user's
own configuration kept out.

Reductions are pages cut down to the parts that carry a form the
captures do not, kept under fair use/dealing. A reduction is cut by hand or
agentically when the form is seen, and stays as cut: evidence that
the platform served the form at that date.

Every CSS selector a source drops a platform's chrome
with (its calls to `Extraction.drop`, in
[`papis_make_epub/sources/extraction.py`](papis_make_epub/sources/extraction.py))
must match a fixture of its platform, which
[`tests/test_selectors.py`](tests/test_selectors.py) checks, so each
has dated evidence of the form it handles, and a change or a
removal is weighed knowing which saved pages it affects.

### Goldens and re-capture

Two events change what the code produces, failing the golden
tests: a fix or improvement to the extraction, and a re-capture
tracking what the publishers serve (procedure below). In both
cases: 1. regenerate with `pytest --force-regen`, saving the
extraction's structure, packaging, and text to the golden files,
2. review the diff to make sure the changes are expected, and
3. commit the goldens together with what moved them.

To re-capture, run `nix run .#refresh-fixtures` (`-- --help` for
its options). The scripted captures run first; then each manual
capture is walked through in turn, its instructions printed and the
run waiting until the file is saved, or skipped. A capture that
validates gets the day's date written into the manifest. Then
regenerate, review, and commit as described above.

Two occasions call for a reduction.

- A fix: the reduction is cut from the page that showed the problem
  and is its regression test.
- A re-capture: the selector test fails when the platform has
  stopped serving a form a selector handles. The reduction, cut from
  the previous capture, keeps the form in the corpus, so pages saved
  while the platform served it still build.
