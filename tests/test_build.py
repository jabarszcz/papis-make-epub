"""Validate each fixture's built EPUB with epubcheck.

Skipped where epubcheck is not on PATH; the flake's test check and
the dev shell provide it.
"""

import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from conftest import Entry

pytestmark = pytest.mark.skipif(shutil.which("epubcheck") is None,
                                reason="epubcheck not on PATH")

NOT_ALLOWED = re.compile(r'(element|attribute) "([^"]+)" not allowed here')


def test_epubcheck(entry: Entry, built_epub: Path,
                   data_regression: Any) -> None:
    result = subprocess.run(["epubcheck", str(built_epub)],
                            capture_output=True, text=True, check=False)
    output = result.stdout + result.stderr
    messages = [line for line in output.splitlines()
                if line.startswith(("FATAL", "ERROR"))]
    # a nonzero exit that reported nothing is a crashed checker
    assert result.returncode == 0 or messages, (
        f"epubcheck did not run for {entry.id}:\n{output}")
    # The content-model errors ("not allowed here") are the publishers'
    # markup nesting a block in an inline element (a div in a
    # reference's span, a table in a span); CSS lays a block inside an
    # inline out as a block, and repairing the pages' markup is out of
    # scope. Every other error is ours to fix. The ignored errors are
    # pinned as counts per element or attribute, so one the build
    # introduces fails the golden until the diff is reviewed.
    ignored: Counter[str] = Counter()
    errors = []
    for line in messages:
        match = NOT_ALLOWED.search(line)
        if match:
            ignored[" ".join(match.groups())] += 1
        else:
            errors.append(line)
    assert not errors, (
        f"epubcheck reported errors for {entry.id}:\n" + "\n".join(errors))
    data_regression.check({"not allowed here": dict(sorted(ignored.items()))})
