"""Facade over the texmath binary (TeX <-> presentation MathML).

Both conversion directions have independent clients: the Formula
accessors in :mod:`papis_make_epub.formula` convert between a
source's math format and a renderer's input, and the
renderer-comparison EPUB generates its MathML samples from TeX.
Every function returns None when the binary is missing or the
conversion fails; callers decide how loudly that fails.
"""

import os
import shutil
import subprocess

# subprocess environment pinning a predictable locale for child tools
ENV = dict(os.environ, LC_ALL="C.UTF-8")


def find_texmath() -> str | None:
    return shutil.which("texmath")


def _convert(source: str, from_fmt: str, to_fmt: str) -> str | None:
    binary = find_texmath()
    if binary is None:
        return None
    try:
        # explicit pipe encoding: text=True alone follows the parent's
        # locale, not the LC_ALL passed to the child
        r = subprocess.run([binary, "-f", from_fmt, "-t", to_fmt],
                           input=source, capture_output=True,
                           text=True, encoding="utf-8", timeout=60,
                           env=ENV, check=False)
    except Exception:
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def tex_to_mathml(tex: str) -> str | None:
    return _convert(tex, "tex", "mathml")


def mathml_to_tex(mathml: str) -> str | None:
    """*mathml* must carry the MathML xmlns; texmath rejects it bare."""
    return _convert(mathml, "mathml", "tex")
