"""Saved full-text pages, the library's on-disk input format.

A saved page is an html file with any media its save fetched in a
sidecar directory beside it, named by the html's stem plus a
saver-dependent suffix (browsers write "<stem>_files", suffix
localized). SavedPage models the html together with that layout:
saving writes it, figure resolution reads it.
"""

import os
import posixpath
import urllib.parse
from dataclasses import dataclass
from logging import Logger
from pathlib import Path, PurePath

from bs4 import BeautifulSoup, Tag

from papis_make_epub.fetch import media_urls, polite_get, resolve_base
from papis_make_epub.logging import get_logger

logger: Logger = get_logger(__name__)

# extensions the stem lookup probes for; the order is the
# preference, core EPUB image types before webp (which converts
# through Pillow)
_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")


def _media_rel(src: str) -> PurePath:
    """Where a media URL lands, relative to the sidecar dir.

    One total mapping shared by saving and resolution, so the round
    trip holds by construction: the URL's path, unquoted and
    dot-segment-folded as fetching folds it (posixpath.normpath on
    the "/"-separated URL path), then joined from its plain
    segments. After the fold, each dropped value occurs in one
    canonical position: "" as an absolute path's root, "." as the
    whole empty path, ".." only leading; every other segment is a
    plain name.
    """
    path = posixpath.normpath(urllib.parse.unquote(
        urllib.parse.urlsplit(src).path))
    return PurePath(*[p for p in path.split("/")
                      if p not in ("", ".", "..")])


@dataclass
class SavedPage:
    """A saved full-text page: its html and its on-disk surroundings."""

    text: str
    # the backing html file; None until saved
    path: Path | None = None

    @classmethod
    def from_file(cls, path: Path) -> "SavedPage":
        """The saved page backed by *path*, its text read now."""
        return cls(path.read_text(encoding="utf-8", errors="replace"),
                   path=path)

    def save(self, path: Path, page_url: str,
             root: Tag | None = None) -> None:
        """Write the html at *path* and its media into the sidecar dir.

        Media land inside "<stem>_files" beside the html, keeping
        their URL-relative structure (see :meth:`media_path`). *root*
        limits the media to a subtree of the caller's own parse; the
        whole document is covered otherwise. *page_url* anchors the
        page's relative media links. Fetch failures propagate.
        """
        self.path = path
        if root is None:
            root = BeautifulSoup(self.text, "lxml")
        doc = root if root.parent is None else list(root.parents)[-1]
        base_url = resolve_base(doc, page_url)
        path.write_text(self.text, encoding="utf-8")
        seen = set()
        for src in media_urls(root):
            if not src or src.startswith("data:") or src in seen:
                continue
            seen.add(src)
            # a src with an empty URL path ("#f", "?q") stores nothing
            if not _media_rel(src).parts:
                continue
            local = self.media_path(src)
            url = urllib.parse.urljoin(base_url, src)
            logger.info("Downloading '%s' to '%s'.", url,
                        os.path.relpath(local, path.parent))
            data = polite_get(url)
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)

    def media_path(self, src: str) -> Path:
        """The sidecar file where a media URL lands when saving."""
        return self._own_sidecar() / _media_rel(src)

    def sidecar_dir(self) -> Path | None:
        """The page's sidecar directory, or None.

        Savers name it by the html's stem plus their own suffix, so
        any sibling directory carrying the stem prefix qualifies; on
        several candidates, the one whose modification time is
        closest to the html's wins (a save writes both together, and
        later copies tend to touch both together).
        """
        if self.path is None:
            return None
        candidates = [
            d for d in self.path.parent.iterdir()
            if d.name.startswith(self.path.stem) and d.is_dir()]
        if not candidates:
            return None
        mtime = self.path.stat().st_mtime
        return min(candidates,
                   key=lambda d: abs(d.stat().st_mtime - mtime))

    def resolve(self, src: str) -> Path | None:
        """The local file of a figure src, or None.

        The src resolves at its mapped path (:func:`_media_rel`),
        against the page's directory and inside the sidecar dir;
        then by probing the sidecar's top level for its basename,
        and for its stem under each image extension. The name probes
        respond to defects observed in browser saves: refs left
        unrewritten to the saved files, and a saved file's extension
        differing from its src (a .gif src beside a saved .png),
        leaving the sidecar reachable only by name.
        """
        if self.path is None:
            return None
        rel = _media_rel(src)
        sidecar = self.sidecar_dir()
        for d in (self.path.parent, sidecar):
            if d is not None and rel.parts and (d / rel).is_file():
                return d / rel
        if sidecar is None:
            return None
        for name in (rel.name, *(rel.stem + ext for ext in _IMAGE_EXT)):
            candidate = sidecar / name
            if name and candidate.is_file():
                return candidate
        return None

    def _own_sidecar(self) -> Path:
        assert self.path is not None
        return self.path.with_name(self.path.stem + "_files")
