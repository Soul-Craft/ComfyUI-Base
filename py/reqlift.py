#!/usr/bin/env python3
"""Lift every upstream pin out of a set of requirement files: one derived file the venv installs from.

Usage:
    reqlift.py --out DERIVED --map MAP.tsv [--extra LABEL=SPEC]... LABEL=PATH [LABEL=PATH ...]

3.0.0: every package the base installs is at its newest. ComfyUI pins its frontend packages with ==, packs pin
their own dependencies, and installing a requirements file as written puts each of those back down on every run.
So nothing is installed from an upstream file directly: this helper merges ComfyUI's, every located pack's and the
package's PIP_EXTRA into one file with the holds removed, and uv resolves the newest set from it.

    ==  ===  <  <=        stripped (each is a hold below the newest)
    ~=X.Y                 becomes >=X.Y (its floor stays, its ceiling goes)
    >=  >  !=             kept (a floor, or a single known-bad version, never a hold on anything newer)
    extras, markers       kept; markers are parsed, so an == inside a marker is untouched
    -r FILE               followed, relative to the file that names it, recursively
    -c FILE               dropped and reported (a constraints file is a set of pins)
    -e PATH, local paths  kept (made absolute)
    --extra-index-url / -f / --find-links / --trusted-host / --pre   carried over, once each; a pack's
                          -i / --index-url joins as an EXTRA index, never replacing PyPI for every other pack
    --hash=...            dropped and reported (hash mode would pin every line)
    VCS URL @ref          the @ref is dropped, so it follows the default branch
    archive URL           kept, reported as held (a file has no newer version to move to)
    torch torchvision torchaudio triton   exempt: the venv's torch step owns them (exact names; torchsde is not exempt)

MAP.tsv gets one row per requirement: name, label (which pack asked), action, original, derived. A failure later
can then name the pack that asked for the package, even though everything was installed in one call.
"""
import argparse
import os
import re
import sys

try:
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.specifiers import SpecifierSet
except ImportError:  # a venv made with --seed always has pip, and pip vendors packaging
    from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
    from pip._vendor.packaging.specifiers import SpecifierSet

EXEMPT = {"torch", "torchvision", "torchaudio", "triton"}
CARRY = ("-i", "--index-url", "--extra-index-url", "-f", "--find-links", "--trusted-host", "--pre", "--prefer-binary")
VCS = ("git+", "hg+", "svn+", "bzr+")


def canon(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def lift_specifier(spec):
    """→ (new SpecifierSet text, [what was lifted])"""
    keep, lifted = [], []
    for s in SpecifierSet(str(spec)):
        op, ver = s.operator, s.version
        if op in ("==", "===", "<", "<="):
            lifted.append(op + ver)
        elif op == "~=":
            keep.append(">=" + ver)
            lifted.append(op + ver)
        else:
            keep.append(op + ver)
    return ",".join(sorted(keep)), lifted


def strip_vcs_ref(url):
    """git+https://github.com/org/repo.git@v1.2#egg=x → git+https://github.com/org/repo.git#egg=x (a user@ before the host stays)"""
    frag = ""
    if "#" in url:
        url, frag = url.split("#", 1)
        frag = "#" + frag
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url + frag
    slash, at = rest.find("/"), rest.rfind("@")
    if 0 <= slash < at:
        rest = rest[:at]
    return scheme + sep + rest + frag


class Lifter:
    def __init__(self):
        self.lines, self.options, self.rows, self.seen_files = [], [], [], set()

    def row(self, name, label, action, orig, new):
        self.rows.append((name, label, action, orig.replace("\t", " "), new.replace("\t", " ")))

    def option(self, text):
        if text not in self.options:
            self.options.append(text)

    def add_file(self, label, path):
        path = os.path.abspath(path)
        if path in self.seen_files:
            return
        self.seen_files.add(path)
        base = os.path.dirname(path)
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read().replace("\\\n", " ")
        for line in raw.splitlines():
            self.add_line(label, line, base)

    def add_line(self, label, line, base=None):
        base = base or os.getcwd()
        line = re.sub(r"(^|\s)#.*$", "", line).strip()
        if not line:
            return
        if line.startswith("-"):
            opt, _, arg = line.replace("=", " ", 1).partition(" ") if line.startswith("--") else (line[:2], "", line[2:])
            arg = arg.strip()
            if opt in ("-r", "--requirement"):
                inc = arg if os.path.isabs(arg) else os.path.join(base, arg)
                if os.path.exists(inc):
                    self.row("", label, "include", line, inc)
                    self.add_file(label, inc)
                else:
                    self.row("", label, "missing-include", line, inc)
            elif opt in ("-c", "--constraint"):
                self.row("", label, "dropped-constraints", line, "")
            elif opt in ("-e", "--editable"):
                tgt = arg
                if not re.match(r"^[a-z0-9+]+://", arg) and not arg.startswith(VCS):
                    tgt = arg if os.path.isabs(arg) else os.path.normpath(os.path.join(base, arg))
                elif arg.startswith(VCS):
                    tgt = strip_vcs_ref(arg)
                self.lines.append("-e " + tgt)
                self.row("", label, "editable", line, "-e " + tgt)
            elif opt in CARRY:
                # a pack's own --index-url must never replace PyPI for everyone: it joins as an EXTRA index
                long = {"-i": "--extra-index-url", "--index-url": "--extra-index-url", "-f": "--find-links"}.get(opt, opt)
                self.option(long if opt in ("--pre", "--prefer-binary") else "%s %s" % (long, arg))
                self.row("", label, "option", line, self.options[-1] if self.options else line)
            else:
                self.row("", label, "dropped-option", line, "")
            return
        hashes = re.findall(r"\s--hash[= ]\S+", line)
        if hashes:
            line = re.sub(r"\s--hash[= ]\S+", "", line).strip()
        if line.startswith(VCS):
            new = strip_vcs_ref(line)
            self.lines.append(new)
            m = re.search(r"#egg=([A-Za-z0-9_.\-]+)", line)
            self.row(canon(m.group(1)) if m else "", label, "vcs-head" if new != line else "kept", line, new)
            return
        if re.match(r"^https?://", line):
            self.lines.append(line)
            self.row("", label, "held-archive", line, line)
            return
        if line.startswith((".", "/", "file:")):
            tgt = line if not line.startswith(".") else os.path.normpath(os.path.join(base, line))
            self.lines.append(tgt)
            self.row("", label, "local", line, tgt)
            return
        try:
            req = Requirement(line)
        except InvalidRequirement:
            self.row("", label, "unparsed", line, "")
            return
        name = canon(req.name)
        if name in EXEMPT:
            self.row(name, label, "exempt-torch", line, "")
            return
        extras = "[%s]" % ",".join(sorted(req.extras)) if req.extras else ""
        marker = " ; %s" % req.marker if req.marker else ""
        if req.url:
            if req.url.startswith(VCS):
                new = "%s%s @ %s%s" % (req.name, extras, strip_vcs_ref(req.url), marker)
                self.lines.append(new)
                self.row(name, label, "vcs-head", line, new)
            else:
                self.lines.append(line)
                self.row(name, label, "held-archive", line, line)
            return
        spec, lifted = lift_specifier(req.specifier)
        new = "%s%s%s%s" % (req.name, extras, spec, marker)
        self.lines.append(new)
        action = "lifted" if lifted else "kept"
        if hashes:
            action += "+dropped-hash"
        self.row(name, label, action, line, new)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--map", required=True)
    ap.add_argument("--extra", action="append", default=[], help="LABEL=SPEC, one requirement line (PIP_EXTRA)")
    ap.add_argument("files", nargs="*", help="LABEL=PATH")
    a = ap.parse_args()
    lf = Lifter()
    for item in a.files:
        label, _, path = item.partition("=")
        if not path:
            label, path = os.path.basename(os.path.dirname(os.path.abspath(item))), item
        if os.path.exists(path):
            lf.add_file(label, path)
    for item in a.extra:
        label, _, spec = item.partition("=")
        lf.add_line(label or "PIP_EXTRA", spec)
    with open(a.out, "w", encoding="utf-8") as fh:
        for o in lf.options:
            fh.write(o + "\n")
        seen = set()
        for ln in lf.lines:
            if ln not in seen:
                seen.add(ln)
                fh.write(ln + "\n")
    with open(a.map, "w", encoding="utf-8") as fh:
        for r in lf.rows:
            fh.write("\t".join(r) + "\n")
    lifted = [r for r in lf.rows if r[2].startswith(("lifted", "vcs-head"))]
    held = [r for r in lf.rows if r[2] in ("held-archive", "unparsed", "missing-include", "dropped-constraints", "dropped-option")]
    for r in lifted:
        print("lifted\t%s\t%s\t%s -> %s" % (r[1], r[0], r[3], r[4]))
    for r in held:
        print("held\t%s\t%s\t%s (%s)" % (r[1], r[0] or "-", r[3], r[2]))
    print("summary\t%d line(s) from %d file(s): %d lifted, %d held" % (len(lf.lines), len(lf.seen_files), len(lifted), len(held)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
