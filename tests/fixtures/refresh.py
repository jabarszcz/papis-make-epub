#!/usr/bin/env python3
"""Refresh the committed golden-fixture captures.

The captures are the manifest's entries of kind "capture"
(``manifest.toml``, which records each one's URL, date and capture
procedure): open-access CC BY papers, each page beside its
bibliographic record under ``library/<id>``. Run from the dev
shell, which puts single-file (single-file-cli) and chromium on
PATH:

    python tests/fixtures/refresh.py [ID ...] [--headed]

The scripted captures run first and land in their document folders
as library/<id>/<id>.html; then each manual capture (a page the
scripted browser is not served: dl.acm.org, sciencedirect.com) is
walked through in turn, its instructions printed and the run
waiting until the file is saved, or skipped. Every capture is
scrubbed of the visit's own data, the inlined script bundles and
the inlined webfonts, then validated against the source detectors
(exactly the right source must match); a browser save's sidecar
directory is pruned to the article's figures, and a capture that
validates gets today's date written into the manifest. --validate
only scrubs, validates and prunes the existing files. Some pages load in a visible
window only; --headed opens one in place of the headless default
(ieeexplore.ieee.org needs it).

A refresh changes the extraction goldens, so re-pin them in the same
change; a selector the new page no longer carries fails
test_selectors, and the old capture (from git history) is then
reduced to what still carried it.
"""

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from papis_make_epub.api import renderer_resolver
from papis_make_epub.book import BuildOptions
from papis_make_epub.saved_page import SavedPage
from papis_make_epub.sources import SOURCES
from papis_make_epub.sources.arxiv import fetch_page

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.toml"


def _captures() -> list[dict[str, Any]]:
    with open(MANIFEST, "rb") as f:
        data = tomllib.load(f)
    return [e for e in data["fixture"] if e["kind"] == "capture"]


PAPERS = _captures()


def record_date(entry_id: str, day: datetime.date) -> None:
    """Write *day* as the entry's ``captured`` date in the manifest.

    The manifest is edited in place, line by line, so its comments
    survive: the ``captured`` line that follows the entry's ``id``
    line is replaced.
    """
    lines = MANIFEST.read_text().splitlines(keepends=True)
    current = None
    for i, line in enumerate(lines):
        if m := re.match(r'id = "([^"]+)"', line):
            current = m.group(1)
        elif current == entry_id and re.match(r"captured = \d{4}-\d{2}-\d{2}",
                                              line):
            lines[i] = f"captured = {day.isoformat()}\n"
            MANIFEST.write_text("".join(lines))
            print(f"    manifest: captured = {day.isoformat()} for {entry_id}")
            return
    print(f"    manifest: no captured line found for {entry_id}; add one")


def capture_path(entry: dict[str, Any]) -> Path:
    """The capture's file: the page in its document's folder."""
    entry_id = str(entry["id"])
    return HERE / "library" / entry_id / f"{entry_id}.html"


def python_fetch(entry: dict[str, Any], out: Path) -> bool:
    try:
        fetch_page(entry["python_fetch"], str(out.parent), name=out.name)
    except Exception as exc:
        print(f"    fetch failed: {exc}")
        return False
    return True


def capture(entry: dict[str, Any], out: Path, headed: bool) -> bool:
    out.unlink(missing_ok=True)
    # the positionals come first: single-file's array options
    # (--browser-script, --browser-args) take every following
    # non-flag token as one more value
    cmd = ["single-file", entry["url"], str(out)]
    chromium = shutil.which("chromium")
    if chromium:
        cmd += ["--browser-executable-path", chromium]
    cmd += [
        # single-file blocks scripts by default; the page's scripts run
        # either way, and blocking drops the script elements from the
        # saved page, where MathJax leaves the math sources it rendered
        "--block-scripts=false",
        # puppeteer launches the browser with navigator.webdriver
        # set, and the interactive challenge some pages run never
        # completes under it; the flag leaves it unset
        "--browser-args", '["--disable-blink-features=AutomationControlled"]',
        # settle time for hydration and interstitials
        "--browser-wait-delay=" + str(entry.get("wait", 5000)),
        # scroll-triggered lazy loaders need the event on top of the
        # default deferred-image loading
        "--load-deferred-images-dispatch-scroll-event=true",
        "--browser-width=1440",
        "--browser-height=900",
    ]
    if headed or entry.get("headed"):
        cmd += ["--browser-headless=false"]
    if entry.get("browser_script"):
        # run in the page before it loads: folds the capture must open
        cmd += ["--browser-script", str(HERE / entry["browser_script"])]
    if entry.get("wait_until"):
        cmd += ["--browser-wait-until=" + entry["wait_until"]]
    try:
        r = subprocess.run(cmd, check=False, timeout=300)
    except subprocess.TimeoutExpired:
        print("    single-file timed out after 300 s")
        return False
    if r.returncode != 0 or not out.exists():
        print(f"    single-file exited {r.returncode}"
              + ("" if out.exists() else f", no {out.name} written")
              + "; command was:\n      " + " ".join(cmd))
        return False
    return True


# Capture-borne data that leaks the capturing visit rather than the
# article, found by reviewing every fixture. Each committed capture
# is public, so the visit's identity must not ride along.
_SCRUBS = [
    # SingleFile's save comment carries a timestamp in the local time
    # zone ("saved date:" is SingleFile's own wording)
    ("save-timestamp",
     re.compile(rb"saved date: (?!\(scrubbed\))[^\n]+"),
     b"saved date: (scrubbed)"),
    # any JWT: ScienceDirect's authorToken/genAiToken (session id,
    # account sub, jti) and ACM's Cloudflare Stream playback token
    ("jwt",
     re.compile(rb"eyJ[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+\.[A-Za-z0-9_=-]+"),
     b"(jwt-scrubbed)"),
    # ScienceDirect user-context JSON: visitor ip, ids, session,
    # entitlement token, capture time, and country
    ("sd-ip",
     re.compile(rb'"ipAddress":"[^"]+"'), b'"ipAddress":""'),
    ("sd-session",
     re.compile(rb'"appSessionId":"[^"]+"'), b'"appSessionId":""'),
    ("sd-user-id",
     re.compile(rb'"userId":"[^"]+"'), b'"userId":""'),
    ("sd-web-user-id",
     re.compile(rb'"webUserId":"[^"]+"'), b'"webUserId":""'),
    ("sd-usage-info",
     re.compile(rb'"usageInfo":"[^"]+"'), b'"usageInfo":""'),
    ("sd-entitled-token",
     re.compile(rb'"entitledToken":"[^"]+"'), b'"entitledToken":""'),
    ("sd-load-timestamp",
     re.compile(rb'"loadTimestamp":[1-9]\d*'), b'"loadTimestamp":0'),
    ("sd-country",
     re.compile(rb'"countryCode":"[^"]+"'), b'"countryCode":""'),
    # ACM's Cloudflare per-visit challenge blob (ray id, timestamp,
    # visitor uuid, token) and the ray-id + edge-datacenter nonce
    ("cf-params",
     re.compile(rb"window\.__CF\$cv\$params=\{[^}]+\}"),
     b"window.__CF$cv$params={}"),
    ("cf-nonce",
     re.compile(rb"nonce=[0-9a-f]{16}-[A-Z]{3}"), b"nonce="),
    # Springer's per-session save-article CSRF token and the Google
    # Tag Manager on-screen timing attributes (the visitor's dwell)
    ("springer-csrf",
     re.compile(rb"_csrf=[^\"'&]+"), b"_csrf="),
    ("gtm-dwell",
     re.compile(rb'\s*data-gtm-vis-[\w-]+="[^"]*"'), b""),
    # the per-pageview correlator in captured ad-request URLs
    ("gpt-correlator",
     re.compile(rb'(gampad/[^"\s]*?[?&]amp;c=)\d{2,}'), rb"\g<1>0"),
    # IEEE's captured scroll position and per-visit script salt
    ("ieee-scroll",
     re.compile(rb"--scroll-y:[1-9]\d*"), b"--scroll-y:0"),
    ("ieee-salt",
     re.compile(rb'window\.j_\.sL="[0-9a-f]+"'), b'window.j_.sL=""'),
    ("ieee-bot-blob",
     re.compile(rb'window\.SZ\.Oj="[0-9a-f]+"'), b'window.SZ.Oj=""'),
    # per-request routing id (dl.acm.org)
    ("acm-request-id",
     re.compile(rb'\s*data-request-id="?[-\w.]+"?'), b""),
    # the capturing visit's scroll/viewport state (dl.acm.org); the
    # platform's own zero-state is a bare 0, which the replacements
    # match and the px-requiring patterns cannot re-match
    ("acm-sticky-top",
     re.compile(rb"--article-sticky-top:[\d.]+px"),
     b"--article-sticky-top:0"),
    ("acm-menu-translate",
     re.compile(rb"--sections-menu-translate-y:-?[\d.]+px"),
     b"--sections-menu-translate-y:0"),
    # the platforms' account keys with their page-side vendors
    # (IEEE: Disqus comments and the Hum beacon; ScienceDirect: Google
    # Maps and Pendo), handed to every browser that loads the page;
    # they identify the publishers' accounts, which the fixture need
    # not carry, and no source reads the scripts holding them
    ("disqus-key",
     re.compile(rb"public_api_key:'[^']+'"), b"public_api_key:''"),
    ("vendor-api-key",
     re.compile(rb"apiKey: '[^']+'"), b"apiKey: ''"),
    ("sd-maps-key",
     re.compile(rb'"googleMapsApiKey":"[^"]+"'), b'"googleMapsApiKey":""'),
]


# script elements with a src attribute: the platform's application
# bundles, which the capture tool inlines, and most of a saved page's
# bytes where it does (ACM, ScienceDirect). No source reads them.
# Inline script bodies stay: math sources and page state live there
# (IEEE's math/tex elements and its xplGlobal marker, ScienceDirect's
# state).
_SRC_SCRIPTS = re.compile(rb"<script\b[^>]*\bsrc=.*?</script>", re.S)

# @font-face rules whose font the capture tool inlined as a data
# URI: the platforms' webfonts, several of them proprietary (Font
# Awesome Pro, Nature's Harding, the Elsevier faces), which a public
# fixture must not redistribute. No source reads CSS; the rule goes
# whole, so the stylesheet stays valid and the text falls back to
# the device's fonts.
_FONT_FACES = re.compile(rb"@font-face\s*\{[^{}]*\bdata:[^{}]*\}")

# runs of whitespace-only lines: the platforms' templates emit them,
# and each element the scrubs above remove leaves its line behind
_BLANK_RUNS = re.compile(rb"\n[ \t]*\n(?:[ \t]*\n)+")


def scrub(out: Path) -> None:
    data = out.read_bytes()
    counts: list[str] = []
    for label, pat, repl in _SCRUBS:
        data, n = pat.subn(repl, data)
        if n:
            counts.append(f"{label} x{n}")
    data, n = _SRC_SCRIPTS.subn(b"", data)
    if n:
        counts.append(f"src-scripts x{n}")
    data, n = _FONT_FACES.subn(b"", data)
    if n:
        counts.append(f"font-faces x{n}")
    data, n = _BLANK_RUNS.subn(b"\n\n", data)
    if n:
        counts.append(f"blank-runs x{n}")
    out.write_bytes(data)
    print("    scrubbed: " + (", ".join(counts) if counts else "nothing"))


@dataclass
class _RecordingPage(SavedPage):
    """A saved page that records the sidecar files its figures
    resolved to."""

    resolved: set[Path] = field(default_factory=set)

    def resolve(self, src: str) -> Path | None:
        path = super().resolve(src)
        if path is not None:
            self.resolved.add(path.resolve())
        return path


def prune_sidecar(entry: dict[str, Any], out: Path) -> None:
    """Keep in a browser save's sidecar directory the files the
    extraction embeds, the article's figures, and delete the rest:
    the page's scripts, stylesheets (webfonts included) and chrome
    images, most of the save's bytes."""
    page = _RecordingPage(out.read_text(encoding="utf-8", errors="replace"),
                          path=out)
    sidecar = page.sidecar_dir()
    if sidecar is None:
        return
    source = next(s for s in SOURCES if s.key == entry["platform"])
    renderers = renderer_resolver(BuildOptions(best_effort=True))(
        source.math_formats)
    source.extract(page, renderers=renderers, best_effort=True)
    kept = page.resolved
    removed = 0
    for path in sorted(sidecar.rglob("*"), reverse=True):
        if path.is_file() and path.resolve() not in kept:
            path.unlink()
            removed += 1
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    print(f"    sidecar: kept {len(kept)} figure files, removed {removed}")


def validate(entry: dict[str, Any], out: Path) -> tuple[bool, list[str]]:
    text = out.read_text(errors="replace")
    hits = [s.key for s in SOURCES if s.matches(text)]
    return hits == [entry["platform"]], hits


def check(entry: dict[str, Any], out: Path, *, date: bool) -> bool:
    """Scrub and validate the capture at *out*; with *date*, record
    today as its capture date. Whether it validated."""
    if not out.exists():
        print(f"    {os.path.relpath(out)} missing")
        return False
    scrub(out)
    ok, hits = validate(entry, out)
    size = out.stat().st_size
    if not ok:
        print(f"    BAD capture {out.name} ({size // 1024} KiB): "
              f"detected as {hits or 'nothing'} (an interstitial "
              f"page? try --headed)")
        return False
    print(f"    valid: {os.path.relpath(out)} ({size // 1024} KiB)")
    prune_sidecar(entry, out)
    if date:
        record_date(entry["id"], datetime.date.today())
    return True


def guided_manual(entry: dict[str, Any], out: Path) -> bool | None:
    """Walk the user through one manual capture: print the
    instructions, wait for the file, check it. None when skipped."""
    print(f"    manual capture: {entry['manual']}")
    if not sys.stdin.isatty():
        print("    no terminal to wait on: skipped (save the file, then "
              "run again, or --validate)")
        return None
    answer = input(f"    [Enter] when {os.path.relpath(out)} is saved, "
                   "s to skip: ").strip().lower()
    if answer.startswith("s"):
        print("    skipped")
        return None
    return check(entry, out, date=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Re-capture the golden fixtures (see module docstring).")
    ap.add_argument("keys", nargs="*", metavar="ID",
                    help="Which captures to refresh (default: all): "
                         + ", ".join(p["id"] for p in PAPERS) + ".")
    ap.add_argument("--headed", action="store_true",
                    help="Drive a visible browser window (for pages "
                         "that load in one only).")
    ap.add_argument("--validate", action="store_true",
                    help="Scrub and validate the existing captures "
                         "without fetching anything or recording dates.")
    args = ap.parse_args()
    known = {p["id"] for p in PAPERS}
    unknown = [k for k in args.keys if k not in known]
    if unknown:
        ap.error(f"unknown ids: {', '.join(unknown)}")
    selected = [p for p in PAPERS if not args.keys or p["id"] in args.keys]
    print(f"manifest: {os.path.relpath(MANIFEST)} "
          f"({len(selected)} of {len(PAPERS)} captures: "
          + ", ".join(p["id"] for p in selected) + ")")

    if args.validate:
        done, skipped, failures = validate_all(selected)
    else:
        done, skipped, failures = refresh_all(selected, args.headed)

    what = "validated" if args.validate else "refreshed"
    if done:
        print(f"{what}: {', '.join(done)}")
    if skipped:
        print(f"skipped (existing capture kept, manifest date unchanged): "
              f"{', '.join(skipped)}")
    if failures:
        print(f"failed: {', '.join(failures)}")
        return 1
    return 0


def validate_all(selected: list[dict[str, Any]],
                 ) -> tuple[list[str], list[str], list[str]]:
    """Check every selected capture; (validated, skipped, failed) ids."""
    done: list[str] = []
    failures: list[str] = []
    for entry in selected:
        out = capture_path(entry)
        print(f"==> {entry['id']}: {os.path.relpath(out)}", flush=True)
        (done if check(entry, out, date=False) else failures).append(
            entry["id"])
    return done, [], failures


def refresh_all(selected: list[dict[str, Any]], headed: bool,
                ) -> tuple[list[str], list[str], list[str]]:
    """Re-capture the scripted entries, then walk through the manual
    ones; (refreshed, skipped, failed) ids."""
    done: list[str] = []
    skipped: list[str] = []
    failures: list[str] = []
    scripted = [p for p in selected if not p.get("manual")]
    for i, entry in enumerate(scripted):
        if i:
            time.sleep(5)
        out = capture_path(entry)
        print(f"==> {entry['id']}: {entry['url']}", flush=True)
        if entry.get("python_fetch"):
            fetched = python_fetch(entry, out)
        else:
            fetched = capture(entry, out, headed)
        if not fetched:
            print(f"    capture FAILED for {entry['id']}")
            failures.append(entry["id"])
            continue
        print(f"    wrote {os.path.relpath(out)}")
        (done if check(entry, out, date=True) else failures).append(
            entry["id"])
    for entry in [p for p in selected if p.get("manual")]:
        out = capture_path(entry)
        print(f"==> {entry['id']}: {entry['url']}", flush=True)
        result = guided_manual(entry, out)
        if result is None:
            skipped.append(entry["id"])
        else:
            (done if result else failures).append(entry["id"])
    return done, skipped, failures


if __name__ == "__main__":
    sys.exit(main())
