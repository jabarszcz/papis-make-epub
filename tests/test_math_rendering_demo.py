"""Build the math rendering demo EPUB once, through ``papis make-epub
--math-demo``, and test its report and its output separately.

The demo pushes one formula corpus through every renderer whose
tooling is present via the real apply_math pipeline, so the
per-renderer tests double as the smoke coverage for the mathjax and
lasem renderers. A renderer whose program is missing shows as that
renderer's test skipped; the whole module skips without texmath, and
the validation test without epubcheck. The flake's test check and the
dev shell provide everything.
"""

import logging
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest
from papis.testing import PapisRunner

from papis_make_epub.cli import cli
from papis_make_epub.math_renderers import (
    RENDERERS,
    RendererUnavailableError,
    make_renderer,
)
from papis_make_epub.math_rendering_demo import DISPLAY, INLINE
from papis_make_epub.texmath import find_texmath

CORPUS = len(INLINE) + len(DISPLAY)


class DemoRun(NamedTuple):
    epub: Path
    exit_code: int
    output: str
    log: str  # the command's log messages, one per line
    available: frozenset[str]  # renderers whose tooling is present


class _Records(logging.Handler):
    """Collects the records logged while installed on the root logger
    (caplog is function-scoped; the demo runs once per module)."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> DemoRun:
    # texmath is the command's hard requirement; the fixture gates
    # ahead of it, since the command turns the missing tool into an
    # exit code
    if find_texmath() is None:
        pytest.skip("texmath not on PATH")
    available = set()
    for mode in RENDERERS:
        try:
            make_renderer(mode)
        except RendererUnavailableError:
            continue
        available.add(mode)

    epub = tmp_path_factory.mktemp("demo") / "math-rendering-demo.epub"
    handler = _Records()
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        run = PapisRunner().invoke(cli, ["--math-demo", str(epub)])
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
    return DemoRun(epub, run.exit_code, run.output,
                   "\n".join(r.getMessage() for r in handler.records),
                   frozenset(available))


def test_command_succeeds(demo: DemoRun) -> None:
    assert demo.exit_code == 0, demo.output
    assert demo.epub.exists()


@pytest.mark.parametrize("mode", list(RENDERERS))
def test_renderer_renders_the_corpus(demo: DemoRun, mode: str) -> None:
    if mode not in demo.available:
        pytest.skip(f"{mode} tooling not on PATH")
    assert f"{mode}: {CORPUS} formulas, 0 fallbacks" in demo.log


def test_missing_renderers_are_reported(demo: DemoRun) -> None:
    for mode in set(RENDERERS) - demo.available:
        assert f"Leaving out renderer '{mode}'" in demo.log, mode


@pytest.mark.skipif(shutil.which("epubcheck") is None,
                    reason="epubcheck not on PATH")
def test_epub_validates(demo: DemoRun) -> None:
    result = subprocess.run(["epubcheck", str(demo.epub)],
                            capture_output=True, text=True, check=False)
    output = result.stdout + result.stderr
    errors = [line for line in output.splitlines()
              if line.startswith(("FATAL", "ERROR"))]
    # a nonzero exit that reported nothing is a crashed checker
    assert result.returncode == 0 or errors, (
        f"epubcheck did not run:\n{output}")
    assert not errors, "epubcheck reported errors:\n" + "\n".join(errors)
