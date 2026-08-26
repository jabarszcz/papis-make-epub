"""Build the math rendering demo EPUB and validate it with epubcheck.

The demo pushes one formula corpus through every renderer via the
real apply_math pipeline, so this doubles as the smoke coverage for
the mathjax and lasem renderers. Skipped where the demo's tooling or
epubcheck is missing; the flake's test check and the dev shell
provide both.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from papis_make_epub.math_renderers import RENDERERS, RendererUnavailableError
from papis_make_epub.math_rendering_demo import DISPLAY, INLINE, build_demo

pytestmark = pytest.mark.skipif(shutil.which("epubcheck") is None,
                                reason="epubcheck not on PATH")


def test_demo_renders_and_validates(tmp_path: Path) -> None:
    out = tmp_path / "math-rendering-demo.epub"
    try:
        stats = build_demo(str(out))
    except RendererUnavailableError as exc:
        pytest.skip(str(exc))
    corpus = len(INLINE) + len(DISPLAY)
    assert set(stats) == set(RENDERERS)
    for mode, (rendered, degraded, _images) in stats.items():
        assert rendered == corpus, mode
        assert degraded == 0, mode
    result = subprocess.run(["epubcheck", str(out)],
                            capture_output=True, text=True, check=False)
    output = result.stdout + result.stderr
    errors = [line for line in output.splitlines()
              if line.startswith(("FATAL", "ERROR"))]
    # a nonzero exit that reported nothing is a crashed checker
    assert result.returncode == 0 or errors, (
        f"epubcheck did not run:\n{output}")
    assert not errors, "epubcheck reported errors:\n" + "\n".join(errors)
