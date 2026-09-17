"""Asserted text transformations for deriving one brand's package from another's sources.

The rule: every rewrite is anchored, and an anchor that no longer matches STOPS the run instead of
writing a half-derived file. A plain str.replace that finds nothing returns its input unchanged and
says nothing; that silence once produced a script branded with the wrong name, shipped, because the
source line it was meant to rewrite had moved. Here the same miss raises DeriveError naming the file
and the anchor, and nothing is written.

    text = replace_once(text, 'PKG_NAME="Example Image Creator"', 'PKG_NAME="Example Image Creator (Other)"', "script.sh")
    text = replace_all(text, "Brand A", "Brand B", "handbook.md", at_least=2)
    text = cut_block(text, "# ---- optional", "# ---- next section", "script.sh", keep_end=True)

    d = Derivation()
    d.emit(dst / "script.sh", text, mode=0o755)
    d.write()                       # every output, parent dirs created
    d.check_or_die()                # what --check runs: the tree must equal a fresh derivation

Stdlib only, so a derivation tool can run from a bare checkout on any machine.
"""
from __future__ import annotations

import pathlib
import stat


class DeriveError(Exception):
    """An anchor did not match, or a derived tree is stale. The message names the file and the anchor."""


def _anchor(s: str) -> str:
    return repr(s[:70])


def replace_once(text: str, old: str, new: str, where: str) -> str:
    """Replace `old` with `new`; `old` must occur exactly once in `text`."""
    n = text.count(old)
    if n != 1:
        raise DeriveError(f"{where}: expected exactly one match for anchor {_anchor(old)}, found {n}")
    return text.replace(old, new)


def replace_all(text: str, old: str, new: str, where: str, at_least: int = 1) -> str:
    """Replace every `old` with `new`; there must be at least `at_least` of them."""
    n = text.count(old)
    if n < at_least:
        raise DeriveError(f"{where}: expected at least {at_least} match(es) for anchor {_anchor(old)}, found {n}")
    return text.replace(old, new)


def cut_block(text: str, start: str, end: str, where: str, keep_end: bool = False) -> str:
    """Remove from the unique `start` marker through the unique `end` marker. The end marker is removed
    too unless `keep_end`; both markers must occur exactly once, and `end` must follow `start`."""
    for label, marker in (("block start", start), ("block end", end)):
        n = text.count(marker)
        if n != 1:
            raise DeriveError(f"{where}: expected exactly one {label} {_anchor(marker)}, found {n}")
    a = text.index(start)
    b = text.index(end)
    if b < a + len(start):
        raise DeriveError(f"{where}: block end {_anchor(end)} comes before block start {_anchor(start)}")
    return text[:a] + (text[b:] if keep_end else text[b + len(end):])


class Derivation:
    """The outputs of one derivation: destination path -> content, written in one go or checked as one.

    `emit(path, content, mode=None)` adds an output; `content` is str (written as UTF-8) or bytes. A `mode`
    of 0o755 makes the file executable; without it, a destination that already exists and is executable
    stays executable. `write()` writes everything; `check()` compares content only (not modes) with what is
    on disk and returns the stale or missing paths, writing nothing; `check_or_die()` raises on any."""

    def __init__(self, outputs: dict[pathlib.Path, str | bytes] | None = None) -> None:
        self._data: dict[pathlib.Path, bytes] = {}
        self._mode: dict[pathlib.Path, int | None] = {}
        for path, content in (outputs or {}).items():
            self.emit(path, content)

    def emit(self, path: pathlib.Path | str, content: str | bytes, mode: int | None = None) -> "Derivation":
        path = pathlib.Path(path)
        self._data[path] = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        self._mode[path] = mode
        return self

    @property
    def paths(self) -> list[pathlib.Path]:
        return list(self._data)

    def write(self) -> None:
        for path, data in self._data.items():
            mode = self._mode[path]
            if mode is None and path.exists() and path.stat().st_mode & stat.S_IXUSR:
                mode = 0o755
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            if mode is not None:
                path.chmod(mode)

    def check(self) -> list[pathlib.Path]:
        return [p for p, data in self._data.items() if not p.is_file() or p.read_bytes() != data]

    def check_or_die(self) -> None:
        stale = self.check()
        if stale:
            raise DeriveError("stale, rerun the derivation:\n  " + "\n  ".join(str(p) for p in stale))
