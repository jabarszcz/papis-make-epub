"""The page as the platform modules see it: elements whose queries
record what they matched, per call site.

A platform module's selectors and attribute rules are its knowledge
of the platform's markup. Every query a module makes goes through a
:class:`Node`, which records, for the call site in the module, the
selector or pattern and the number of matches. The records trace each
rule to the fixtures in which it fires, and name the rules that fire
in none (``tests/test_rules.py``, ``tools/fixture_coverage.py``).
The ids a module derives for a cross-reference's target are rules
too: each is built by :func:`target` and recorded, for that call
site, as resolving the link or not.

A platform module imports nothing from bs4 and reads no private
attribute of a node (the ruff configuration in ``pyproject.toml``
checks both), so the queries below are the only way into the page.
"""

import ast
import copy
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Union

import soupsieve
from bs4 import Tag
from bs4.element import NavigableString

# the modules whose frames the recorder skips to find the call site
_INTERNAL = {__file__}


def internal_module(path: str) -> None:
    """Have the recorder skip *path*'s frames: a module that calls the
    queries on behalf of a platform module."""
    _INTERNAL.add(path)


@dataclass(frozen=True)
class Site:
    """Where a query is called: the file, and the position where the
    call expression ends (for a call over several lines, a frame
    reports the span of its last line only; the end is the part both
    the frame and the parsed source agree on)."""

    path: str
    end_line: int
    end_col: int


@dataclass(frozen=True)
class Rule:
    """One rule as recorded: its call site, the query, and the
    selector or attribute pattern."""

    site: Site
    query: str
    detail: str


def _call_site() -> Site:
    frame = sys._getframe(1)
    while frame.f_back is not None and frame.f_code.co_filename in _INTERNAL:
        frame = frame.f_back
    positions = list(frame.f_code.co_positions())[frame.f_lasti // 2]
    end_line, end_col = positions[1], positions[3]
    return Site(frame.f_code.co_filename, end_line or 0, end_col or 0)


class Rules:
    """The matches recorded per rule during one extraction."""

    def __init__(self) -> None:
        self.matches: dict[Rule, int] = {}

    def record(self, query: str, detail: str, n: int,
               site: Site | None = None) -> None:
        rule = Rule(site or _call_site(), query, detail)
        self.matches[rule] = self.matches.get(rule, 0) + n


class Target(str):
    """A candidate id for a cross-reference's target, carrying the
    call site that built it (see :func:`target`)."""

    site: Site


def target(id_: str) -> Target:
    """*id_* as a candidate target of a cross-reference: a link-target
    rule, which :meth:`Extraction.resolve_links` records for this call
    site as resolving the link or not."""
    candidate = Target(id_)
    candidate.site = _call_site()
    return candidate


def _parts(selector: str) -> list[str]:
    """The comma-separated selectors of a selector list, recorded
    apart (each is a rule of its own)."""
    return [s.strip() for s in selector.split(",")]


Content = Union["Node", str]


def _raw(content: Content) -> Tag | NavigableString:
    return content._tag if isinstance(content, Node) else NavigableString(content)


class Node:
    """An element of the page, for a platform module.

    The queries record their matches with the recorder the extraction
    passed; the reads and edits are plain operations on the element.
    """

    def __init__(self, tag: Tag, rules: Rules) -> None:
        self._tag = tag
        self._rules = rules

    def __repr__(self) -> str:
        return f"Node(<{self._tag.name}>)"

    def _wrap(self, tags: Iterable[Tag]) -> list["Node"]:
        return [Node(t, self._rules) for t in tags]

    # queries: each records its matches for the call site (and attr,
    # below, which reads a value)

    def first(self, *selectors: str) -> "Node | None":
        """The first element under this one that the first matching
        selector matches, trying them in order; the selectors after
        it are recorded as not matching."""
        site = _call_site()
        found = None
        for selector in selectors:
            match = None if found is not None else self._tag.select_one(selector)
            self._rules.record("first", selector, int(match is not None), site)
            if match is not None:
                found = Node(match, self._rules)
        return found

    def select(self, selector: str) -> list["Node"]:
        """The elements under this one that *selector* matches, in
        document order; each selector of a list is recorded apart."""
        site = _call_site()
        parts = _parts(selector)
        if len(parts) > 1:
            for part in parts:
                self._rules.record("select", part, len(self._tag.select(part)),
                                   site)
        matches = self._wrap(self._tag.select(selector))
        if len(parts) == 1:
            self._rules.record("select", selector, len(matches), site)
        return matches

    def children(self, selector: str) -> list["Node"]:
        """The child elements that *selector* matches."""
        matches = [c for c in self._tag.find_all(True, recursive=False)
                   if soupsieve.match(selector, c)]
        self._rules.record("children", selector, len(matches))
        return self._wrap(matches)

    def parent(self, selector: str) -> "Node | None":
        """The nearest ancestor that *selector* matches."""
        found = next((p for p in self._tag.parents
                      if isinstance(p, Tag) and p.name != "[document]"
                      and soupsieve.match(selector, p)), None)
        self._rules.record("parent", selector, int(found is not None))
        return Node(found, self._rules) if found is not None else None

    def matches(self, selector: str) -> bool:
        """Whether *selector* matches this element itself; each
        selector of a list is recorded apart."""
        site = _call_site()
        hits = [soupsieve.match(part, self._tag) for part in _parts(selector)]
        for part, hit in zip(_parts(selector), hits, strict=True):
            self._rules.record("matches", part, int(hit), site)
        return any(hits)

    def has(self, selector: str) -> bool:
        """Whether *selector* matches an element under this one."""
        found = self._tag.select_one(selector) is not None
        self._rules.record("has", selector, int(found))
        return found

    def drop(self, selector: str) -> None:
        """Remove every element under this one that *selector*
        matches; each selector of a list is recorded apart."""
        site = _call_site()
        for part in _parts(selector):
            self._rules.record("drop", part, len(self._tag.select(part)), site)
        for el in self._tag.select(selector):
            el.decompose()

    def strip_attributes(self, *patterns: str) -> None:
        """Remove from this element and its descendants every
        attribute whose name a pattern matches in full (a regular
        expression); each pattern is recorded with the attributes it
        removed."""
        site = _call_site()
        counts = dict.fromkeys(patterns, 0)
        compiled = [(p, re.compile(p)) for p in patterns]
        for el in (self._tag, *self._tag.find_all(True)):
            for attr in list(el.attrs):
                for pattern, rx in compiled:
                    if rx.fullmatch(attr):
                        del el[attr]
                        counts[pattern] += 1
                        break
        for pattern, n in counts.items():
            self._rules.record("strip_attributes", pattern, n, site)

    # reads

    @property
    def is_removed(self) -> bool:
        """Whether the element was removed (with an ancestor)."""
        return bool(self._tag.decomposed)

    def attr(self, name: str) -> str | None:
        """The attribute's value, recorded as present or not; a
        multi-valued one (class) joined by spaces. (Named apart from
        a mapping's ``get``, which the rule listing would count.)"""
        value = self._tag.get(name)
        self._rules.record("attr", name, int(value is not None))
        if value is None:
            return None
        return " ".join(value) if isinstance(value, list) else str(value)

    def text(self, separator: str = "", strip: bool = False) -> str:
        return str(self._tag.get_text(separator, strip=strip))

    # edits

    def set(self, attr: str, value: str) -> None:
        self._tag[attr] = value

    def delete(self, attr: str) -> None:
        if self._tag.has_attr(attr):
            del self._tag[attr]

    def rename(self, name: str) -> None:
        """Give the element the tag name *name*, its attributes and
        content kept."""
        self._tag.name = name

    def remove(self) -> None:
        self._tag.decompose()

    def unwrap(self) -> None:
        self._tag.unwrap()

    def replace_with(self, replacement: Content) -> None:
        self._tag.replace_with(_raw(replacement))

    def append(self, child: Content) -> None:
        self._tag.append(_raw(child))

    def insert(self, position: int, child: Content) -> None:
        self._tag.insert(position, _raw(child))

    def adopt_children(self, other: "Node") -> None:
        """Move *other*'s content, text included, to the end of this
        element."""
        self._tag.extend(list(other._tag.contents))

    def copy(self) -> "Node":
        """A detached deep copy of this element."""
        return Node(copy.copy(self._tag), self._rules)


# the Node methods that record (and Extraction.drop, which calls one)
QUERIES = frozenset({"first", "select", "children", "parent", "matches",
                     "has", "attr", "drop", "strip_attributes"})
# the functions that build link-target candidates
TARGETS = frozenset({"target", "fragment_targets"})


@dataclass(frozen=True)
class RuleSite:
    """A query call in a platform module: the site its records carry,
    and, for reports, the line where the call starts and the call as
    written (one site can record several rules: a fallback chain, a
    selector list, selectors built at run time)."""

    site: Site
    line: int
    call: str

    @property
    def where(self) -> str:
        """The site for a report: the module's file name and the line
        the call starts on."""
        return f"{Path(self.site.path).name}:{self.line}"


def rule_sites(path: str) -> list[RuleSite]:
    """The query and link-target calls in the module at *path*: the
    rules it can record, whether a run reaches them or not."""
    with open(path, encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, path)
    return sorted(
        (RuleSite(Site(path, call.end_lineno or 0, call.end_col_offset or 0),
                  call.lineno,
                  " ".join((ast.get_source_segment(source, call) or "").split()))
         for call in ast.walk(tree)
         if isinstance(call, ast.Call)
         and ((isinstance(call.func, ast.Attribute) and call.func.attr in QUERIES)
              or (isinstance(call.func, ast.Name) and call.func.id in TARGETS))),
        key=lambda r: (r.site.end_line, r.site.end_col))


def tag_of(node: Node) -> Tag:
    """The element a node wraps, for the generic stages."""
    return node._tag
