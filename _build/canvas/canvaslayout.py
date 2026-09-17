#!/usr/bin/env python3
"""ComfyUI Base — the canvas layout engine: a package's frames laid out from its tables, reproducibly.

    python3 "<package>/_build/layout.py" [--verify | --dry-run] [--strict-sizes] [--structure-sha]

A package's `_build/layout.py` holds only tables (ADOPT, COCKPIT, STRIPS, ...) and ends with `run(globals())`;
everything that turns tables into geometry lives here. Every rule below was paid for by a canvas defect: the size floor from the frontend's computeSize(), the
membership guard (rgthree decides behaviour from frame membership, so a relayout may never change it), grid-aligned
frame padding, the cockpit tree, the deterministic ordering pass. Tables the engine reads (S.<NAME>): DEFAULTS
below are what a package gets when it declares nothing."""
import argparse, json, math, os, sys, types, importlib.util, pathlib
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "py"))
from canvas import structure_sha, CHROME_LEFT, CHROME_TOP, TITLE_H, GRID, PREVIEW_RESERVE, _idkey  # noqa: E402
# _idkey: a ProGrade-derived graph carries string node ids ("2682:0") beside integers, and
# every sort below puts a raw id in its key. Integers keep numeric order, strings follow.

DEFAULTS = dict(SHIP_SCALE=0.58, COCKPIT_GAP=20, COCKPIT_ROW_GAP=40, BAND_W=15000, READING_ASPECT=0.45, MACHINE_ASPECT=0.45, BAND_GAP=220,
                MAX_LANE_H=3000, COCKPIT=[], COCKPIT_ORDER=[], COCKPIT_ASPECT={}, NEW_GROUPS={}, ALLOWED_LOSS={}, ADOPT={}, KEEP_UNGROUPED=set(),
                FOLD_HOME={}, PANEL_HOME={}, STRIPS={}, RENAME_OVERRIDE={}, NOTE_IDS=(), NOTE_ID_FROM=0, NODE_TITLES={}, SIZES_PATH=None, PKG_DIR=None)
S = types.SimpleNamespace(**DEFAULTS)      # the active package's tables; set by run() / configure()

import argparse, json, math, os, sys
from collections import OrderedDict

PAD_L = PAD_R = 10          # suite requires >= 10.  Grid multiples: a frame built from grid-placed nodes lands on the grid,
PAD_T = 30                  # suite requires >= 28.  so the outward snap in apply() is a no-op and a 20-unit gap stays 20
PAD_B = 10                  # suite requires >= 5    (14/34/14 snapped every frame 10 wider and ate the cockpit's gaps)
GAP = 20                    # the density knee: 40 gave 0.214 fill, 14 gave 0.253 but reads cramped
NEST_INSET = 24             # suite requires >= 14
NEST_GAP = 40               # between STACKED CHILD FRAMES. GAP (20) is a node-to-node figure and
                            # measure() under-reports a frame by a few px, so stacking children on
                            # it left 10px between two frames -- test_spacing_rules wants >= 18.
GROUP_GAP = 60              # suite requires >= 18 between unrelated groups
CELL_GAP = 110              # inside a preset strip: must clear the deepest span's inset

# CHROME_LEFT, CHROME_TOP: from canvas.py (measured by snapshot.py)   # css px of canvas under the left icon strip and the workflow tab bar (frontend 1.49.6, measured)           # above the dpr-1 cliff of 0.571, so widgets render anywhere

def snap(v):
    return int(round(v / GRID) * GRID)

SELF_DRAWN = ("Label (rgthree)", "Bookmark (rgthree)")      # rgthree's panels compute a real size (one row per toggle): they take the floor like any node
MULTILINE = ("PrimitiveStringMultiline", "MarkdownNote", "Note")

def load_sizes(path=None):
    path = path or S.SIZES_PATH
    if not path: return None
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except (OSError, ValueError):
        return None

SIZES = None
ESTIMATED = set()

def estimate_size(n):
    """LGraphNode.computeSize from the JSON alone: rows of slots, widgets of 20 (multiline 60), the
    title. Used only for a node sizes.json has not seen; the next snapshot replaces it."""
    ins = [i for i in n.get("inputs", []) or [] if "widget" not in i]
    outs = n.get("outputs", []) or []
    rows = max(len(ins), len(outs), 1)
    wv = n.get("widgets_values_named") or n.get("widgets_values") or []
    nwid = len(wv) if not isinstance(wv, dict) else len(wv)
    if n.get("type") in MULTILINE: nwid = max(nwid, 1)
    wh = 60 if n.get("type") in MULTILINE else 20
    h = rows * 20 + sum(wh + 4 for _ in range(nwid)) + 14
    texts = [n.get("title") or n.get("type") or ""] + [i.get("label") or i.get("name") or "" for i in n.get("inputs", []) or []] + [o.get("name") or "" for o in outs]
    w = max(210 if nwid else 140, max(len(t) for t in texts) * 14 * 0.6 + 40)
    return w, h

def computed_min(n, graph_key="root"):
    """(w, h) the frontend draws this node at, without the title bar. None for self-drawn types."""
    if n.get("type") in SELF_DRAWN: return None
    key = str(n["id"]) if graph_key == "root" else f"{graph_key}:{n['id']}"
    rec = (SIZES or {}).get("nodes", {}).get(key)
    if rec is not None and rec.get("type") == n.get("type") and not rec.get("missing"):
        w, h = rec["computed"]
        return float(w), float(h)
    ESTIMATED.add(key)
    return estimate_size(n)

# ---------------------------------------------------------------- geometry helpers
def nsize(n):
    s = n["size"]
    if isinstance(s, dict):
        s = [s.get("0"), s.get("1")]
    w, h = float(s[0]), float(s[1])
    if (n.get("flags") or {}).get("collapsed"):
        return 80.0, 0.0 + TITLE_H
    cm = computed_min(n)
    if cm is not None:
        w, h = max(w, ceil_grid(cm[0])), max(h, ceil_grid(cm[1]))   # the frontend will not draw it smaller; grid-rounded, as grow_sizes writes it
    if n.get("type") == "LoadImage": h += PREVIEW_RESERVE           # the picture preview that appears once a file is loaded (useImagePreviewWidget minHeight)
    return w, h + TITLE_H          # the title bar is part of the footprint


def ceil_grid(v): return int(math.ceil(v / GRID - 1e-9) * GRID)

def _graphs(wf):
    yield "root", wf["nodes"]
    for sg in wf.get("definitions", {}).get("subgraphs", []) or []: yield sg["id"], sg.get("nodes", []) or []

def grow_sizes(wf):
    """Write the floor back: a node whose stored size is below the frontend's computed size gets
    the computed size (rule 6b: geometry, sizes included, belongs to this tool). Interiors too."""
    grown = []
    for gkey, nodes in _graphs(wf):
      for n in nodes:
        if (n.get("flags") or {}).get("collapsed"): continue
        cm = computed_min(n, gkey)
        if cm is None: continue
        s = n["size"]; s = [s.get("0"), s.get("1")] if isinstance(s, dict) else list(s)
        w, h = float(s[0]), float(s[1])
        if w < cm[0] - 0.5 or h < cm[1] - 0.5:
            n["size"] = [max(w, ceil_grid(cm[0])), max(h, ceil_grid(cm[1]))]
            grown.append((n["id"], (w, h), n["size"]))
    return grown

def check_sizes(wf):
    """Every node's stored size, root and interior, is at least what the frontend computes. Empty = ok."""
    bad = []
    for gkey, nodes in _graphs(wf):
      for n in nodes:
        if (n.get("flags") or {}).get("collapsed"): continue
        cm = computed_min(n, gkey)
        if cm is None: continue
        s = n["size"]; s = [s.get("0"), s.get("1")] if isinstance(s, dict) else s
        if float(s[0]) < cm[0] - 0.5 or float(s[1]) < cm[1] - 0.5: bad.append((n["id"], n.get("type"), [float(s[0]), float(s[1])], cm))
    return bad

def nsize_stored(n):
    """The size the FILE says, title bar included -- what the last layout placed against."""
    s = n["size"]
    if isinstance(s, dict): s = [s.get("0"), s.get("1")]
    w, h = float(s[0]), float(s[1])
    if (n.get("flags") or {}).get("collapsed"): w, h = 80.0, 0.0
    return w, h + TITLE_H

def nbound(n, stored=False):
    x, y = float(n["pos"][0]), float(n["pos"][1])
    w, h = nsize_stored(n) if stored else nsize(n)
    return (x, y - TITLE_H, x + w, y - TITLE_H + h)

def gbound(g):
    x, y, w, h = g["bounding"]
    return (float(x), float(y), float(x) + float(w), float(y) + float(h))

def contains(o, i):
    return o[0] <= i[0] and o[1] <= i[1] and o[2] >= i[2] and o[3] >= i[3]

def membership(wf):
    """group title -> frozenset of contained node ids. The contract, read off the canvas."""
    gs = [(g["title"], gbound(g)) for g in wf["groups"]]
    def centre_in(G, b):                      # rgthree's own rule (fast_groups_service.js): the node's CENTRE decides
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        return G[0] <= cx <= G[2] and G[1] <= cy <= G[3]
    out = {}
    for t, G in gs:
        out[t] = frozenset(n["id"] for n in wf["nodes"] if centre_in(G, nbound(n, stored=True)))
    return out

# ---------------------------------------------------------------- packing
def pack_grid(items, target_aspect=1.7):
    """Lay (id, w, h) out in columns, aiming for a readable aspect. Returns (placements, w, h).

    Column-major so a group reads top-to-bottom then left-to-right, which is how the eye walks
    a stack of settings."""
    if not items:
        return [], 0.0, 0.0
    total = sum(h for _, _, h in items)
    widest = max(w for _, w, _ in items)
    best = None
    for ncol in range(1, len(items) + 1):
        per = -(-len(items) // ncol)
        cols, i = [], 0
        while i < len(items):
            cols.append(items[i:i + per]); i += per
        w = sum(max(x[1] for x in c) for c in cols) + GAP * (len(cols) - 1)
        h = max(sum(x[2] for x in c) + GAP * (len(c) - 1) for c in cols)
        score = abs((w / h if h else 99) - target_aspect)
        if best is None or score < best[0]:
            best = (score, cols, w, h)
    _, cols, w, h = best
    out, x = [], 0.0
    for c in cols:
        cw = max(i[1] for i in c); y = 0.0
        for nid, iw, ih in c:
            out.append((nid, x, y)); y += ih + GAP
        x += cw + GAP
    return out, w, h

# ---------------------------------------------------------------- blocks
class Block:
    def measure(self, ctx): raise NotImplementedError
    def place(self, ctx, x, y): raise NotImplementedError

class Nodes(Block):
    """A bare set of nodes with no frame of their own.

    The default aspect is deliberately tall and narrow. Groups are packed into vertical lanes,
    so a wide group forces a wide lane and the canvas grows sideways: at aspect 1.7 this file
    came out 11,340 px across, which is 52.8 Mpx of canvas for 11.3 Mpx of node."""
    def __init__(self, ids, aspect=0.45):
        self.ids, self.aspect = list(ids), aspect
    def measure(self, ctx):
        items = [(i, *nsize(ctx.node[i])) for i in self.ids if i in ctx.node]
        self.pl, w, h = pack_grid(items, self.aspect)
        return w, h
    def place(self, ctx, x, y):
        for nid, dx, dy in self.pl:
            ctx.put(nid, x + dx, y + dy)

class Group(Block):
    """A titled frame whose bounds are computed from whatever ends up inside it."""
    def __init__(self, title, body, inner=()):
        self.title, self.body, self.inner = title, body, list(inner)
    def measure(self, ctx):
        w, h = self.body.measure(ctx)
        for g in self.inner:
            iw, ih = g.measure(ctx)
            w = max(w, iw + 2 * NEST_INSET)
            h += ih + NEST_GAP + NEST_INSET
        return ceil_grid(w + PAD_L + PAD_R), ceil_grid(h + PAD_T + PAD_B)     # grid: the next block starts on the grid
    def place(self, ctx, x, y):
        bx, by = x + PAD_L, y + PAD_T
        bw, bh = self.body.measure(ctx)
        self.body.place(ctx, bx, by)
        cy = by + bh + (NEST_GAP if bh else 0)
        for g in self.inner:
            g.place(ctx, bx + NEST_INSET, cy)
            cy += g.measure(ctx)[1] + NEST_GAP
        ctx.frame(self.title, self, [g.title for g in self.inner])

class Row(Block):
    def __init__(self, children, gap=GROUP_GAP):
        self.children, self.gap = list(children), gap
    def measure(self, ctx):
        ms = [c.measure(ctx) for c in self.children]
        if not ms: return 0.0, 0.0
        return sum(m[0] for m in ms) + self.gap * (len(ms) - 1), max(m[1] for m in ms)
    def place(self, ctx, x, y):
        for c in self.children:
            c.place(ctx, x, y)
            x += c.measure(ctx)[0] + self.gap

class Col(Block):
    def __init__(self, children, gap=GROUP_GAP):
        self.children, self.gap = list(children), gap
    def measure(self, ctx):
        ms = [c.measure(ctx) for c in self.children]
        if not ms: return 0.0, 0.0
        return max(m[0] for m in ms), sum(m[1] for m in ms) + self.gap * (len(ms) - 1)
    def place(self, ctx, x, y):
        for c in self.children:
            c.place(ctx, x, y)
            y += c.measure(ctx)[1] + self.gap

class Skyline(Block):
    """Flow children left to right, each dropped to the lowest point it will rest on.

    Placing them in rows makes a row as tall as its tallest member, so every shorter frame
    leaves dead space beneath it -- the largest single waste once lanes were balanced.
    This keeps a height profile across the lane's width and seats each frame on it, which is the
    same idea as a skyline bin-packer and needs no more bookkeeping than one list of segments."""
    def __init__(self, children, max_w, gap=GROUP_GAP):
        self.children, self.max_w, self.gap = list(children), max_w, gap
    def _solve(self, ctx):
        prof = [(0.0, self.max_w, 0.0)]                  # (x0, x1, height) segments
        out = []
        for c in self.children:
            w, h = c.measure(ctx)
            w = min(w, self.max_w)
            # Reserve the gap on BOTH axes. Reserving only vertically let two frames sit flush,
            # and flush frames overlap once each one's padding is drawn -- reported as an
            # unexpected overlap between IMG 1 and VID 2.
            wg = w + self.gap
            best = None
            xs = sorted({p[0] for p in prof} | {0.0})
            for x in xs:
                if x + w > self.max_w + 0.5:
                    continue
                y = max([p[2] for p in prof if p[0] < x + wg and p[1] > x] or [0.0])
                if best is None or y < best[1] or (y == best[1] and x < best[0]):
                    best = (x, y)
            if best is None:
                best = (0.0, max(p[2] for p in prof))
            x, y = best
            out.append((c, x, y, w, h))
            new = []
            for p in prof:
                if p[1] <= x or p[0] >= x + wg:
                    new.append(p); continue
                if p[0] < x: new.append((p[0], x, p[2]))
                if p[1] > x + wg: new.append((x + wg, p[1], p[2]))
            new.append((x, x + wg, y + h + self.gap))
            prof = sorted(new)
        return out
    def measure(self, ctx):
        pl = self._solve(ctx)
        if not pl: return 0.0, 0.0
        return (max(x + w for _, x, _, w, _ in pl), max(y + h for _, _, y, _, h in pl))
    def place(self, ctx, x0, y0):
        for c, x, y, _, _ in self._solve(ctx):
            c.place(ctx, x0 + x, y0 + y)

class Strip(Block):
    """The preset cascade, drawn as what it is.

    Master and Quality must intersect: they share the upscale column, and rgthree reads that
    intersection off the bounds. Laid out as ordered cells, each frame spanning a contiguous
    run of them, the overlap becomes a clean rectangle in the middle of a band instead of two
    rectangles that happen to collide."""
    def __init__(self, cells, spans):
        self.cells, self.spans = cells, spans          # cells: [(key, [node ids])]
    def measure(self, ctx):
        # Two passes. A strip is as tall as its tallest cell, so a cell packed to a pleasing
        # aspect and then left short wastes the whole band's height beneath it. Measure once to
        # learn the band height, then re-pack every cell into the fewest columns that still fit
        # it -- narrower strip, same height, far less empty frame.
        first = [(key, Nodes(ids, aspect=0.5), ids) for key, ids in self.cells]
        band = max([b.measure(ctx)[1] for _, b, _ in first] or [0])
        self.blocks = OrderedDict()
        w = h = 0.0
        for key, _, ids in first:
            best = None
            for asp in (0.18, 0.25, 0.32, 0.4, 0.5, 0.7, 1.0):
                cand = Nodes(ids, aspect=asp)
                cw, ch = cand.measure(ctx)
                if ch <= band and (best is None or cw < best[1]):
                    best = (cand, cw, ch)
            b, cw, ch = best or (Nodes(ids, aspect=0.5), *Nodes(ids, aspect=0.5).measure(ctx))
            self.blocks[key] = (b, cw, ch)
            w += cw + CELL_GAP
            h = max(h, ch)
        # The deepest span sticks out past the cells on every side; that margin has to be part of
        # the footprint or the next group is laid down inside it -- which is what "groups too
        # close" reported for 🔗3 against the audio anchor.
        m = 2 * self.max_depth() * NEST_INSET
        return ceil_grid(w - CELL_GAP + PAD_L + PAD_R + m), ceil_grid(h + PAD_T + PAD_B + m)
    def max_depth(self):
        return max([d for _, _, d in self.spans] or [0])
    def place(self, ctx, x, y):
        self.measure(ctx)
        m = self.max_depth() * NEST_INSET
        x, y = x + m, y + m
        _, H0 = self.measure(ctx)
        H = H0 - 2 * m
        cx = x + PAD_L
        self.span_x = {}
        for key, _ in self.cells:
            b, cw, ch = self.blocks[key]
            b.place(ctx, cx, y + PAD_T)
            self.span_x[key] = (cx, cx + cw)
            cx += cw + CELL_GAP
        cell_ids = dict(self.cells)
        for title, keys, depth in self.spans:
            # Measure the span from where the nodes ACTUALLY landed, not from the cell arithmetic.
            # ctx.put snaps every node to the grid, which can move it up to 5px, and a frame drawn
            # from the unsnapped cell edges then leaves 9px of padding where 10 is required.
            # Inset on all four sides, not only top and bottom: two frames sharing a left-hand
            # cell would otherwise start at the same x, and a nested pair with a coincident edge
            # is what test_spacing_rules calls a tight nest -- reported for 🔗2 inside 🔗3.
            ids = [i for k in keys for i in cell_ids.get(k, []) if i in ctx.pos]
            if ids:
                bs = [(ctx.pos[i][0], ctx.pos[i][1] - TITLE_H,
                       ctx.pos[i][0] + nsize(ctx.node[i])[0],
                       ctx.pos[i][1] - TITLE_H + nsize(ctx.node[i])[1]) for i in ids]
                x0 = min(b[0] for b in bs) - PAD_L - depth * NEST_INSET
                x1 = max(b[2] for b in bs) + PAD_R + depth * NEST_INSET
                y0 = min(b[1] for b in bs) - PAD_T - depth * NEST_INSET
                y1 = max(b[3] for b in bs) + PAD_B + depth * NEST_INSET
            else:
                x0 = min(self.span_x[k][0] for k in keys) - PAD_L - depth * NEST_INSET
                x1 = max(self.span_x[k][1] for k in keys) + PAD_R + depth * NEST_INSET
                y0, y1 = y - depth * NEST_INSET, y + H + depth * NEST_INSET
            ctx.rect(title, x0, y0, x1, y1)

# ---------------------------------------------------------------- context
class Ctx:
    def __init__(self, wf):
        self.wf = wf
        self.node = {n["id"]: n for n in wf["nodes"]}
        self.pos = {}
        self.rects = {}
    def put(self, nid, x, y):
        self.pos[nid] = (snap(x), snap(y + TITLE_H))     # pos is the body top-left
    def frame(self, title, blk, inner_titles=()):
        """Compute a group's bounds from what it holds.

        It must enclose its children's FRAMES with clearance, not merely their nodes. A child
        frame pads beyond its own contents, so an outer box sized to the nodes alone ends up
        inside its child on one edge -- which is what test_spacing_rules calls a tight nest, and
        what it reported for a Models frame around its engine group on the first run."""
        ids = self._ids_of(blk)
        bs = [(self.pos[i][0], self.pos[i][1] - TITLE_H,
               self.pos[i][0] + nsize(self.node[i])[0],
               self.pos[i][1] - TITLE_H + nsize(self.node[i])[1]) for i in ids if i in self.pos]
        if not bs:
            return
        x0 = min(b[0] for b in bs) - PAD_L; y0 = min(b[1] for b in bs) - PAD_T
        x1 = max(b[2] for b in bs) + PAD_R; y1 = max(b[3] for b in bs) + PAD_B
        for t in inner_titles:
            r = self.rects.get(t)
            if r:
                x0 = min(x0, r[0] - NEST_INSET); y0 = min(y0, r[1] - NEST_INSET)
                x1 = max(x1, r[2] + NEST_INSET); y1 = max(y1, r[3] + NEST_INSET)
        self.rects[title] = (x0, y0, x1, y1)
    def rect(self, title, x0, y0, x1, y1):
        self.rects[title] = (x0, y0, x1, y1)
    def _ids_of(self, blk):
        out = set()
        stack = [blk]
        while stack:
            b = stack.pop()
            if isinstance(b, Nodes): out |= {i for i in b.ids if i in self.node}
            elif isinstance(b, Group): stack += [b.body] + b.inner
            elif isinstance(b, (Row, Col)): stack += b.children
            elif isinstance(b, Strip): stack += [x[0] for x in b.blocks.values()]
        return out

# ---------------------------------------------------------------- write back
GEOM_KEYS = ("pos", "size", "order")

def apply(wf, ctx):
    for nid, (x, y) in ctx.pos.items():
        wf_node = ctx.node[nid]
        wf_node["pos"] = [x, y]
    for g in wf["groups"]:
        r = ctx.rects.get(g["title"])
        if r is None:
            continue
        # Snap frames OUTWARD. Rounding to nearest can shave a few pixels off an edge and drop a
        # node's padding under the minimum the suite requires -- 9px where 10 was needed.
        x0 = math.floor(r[0] / GRID) * GRID; y0 = math.floor(r[1] / GRID) * GRID
        x1 = math.ceil(r[2] / GRID) * GRID;  y1 = math.ceil(r[3] / GRID) * GRID
        g["bounding"] = [x0, y0, x1 - x0, y1 - y0]
    for i, n in enumerate(sorted(wf["nodes"], key=lambda n: (n["pos"][0], n["pos"][1]))):
        n["order"] = i
    boxes = [nbound(n) for n in wf["nodes"]] + [gbound(g) for g in wf["groups"]]
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    wf.setdefault("extra", {})["ds"] = {"scale": S.SHIP_SCALE, "offset": [-x0 + snap(CHROME_LEFT / S.SHIP_SCALE) + 30, -y0 + snap(CHROME_TOP / S.SHIP_SCALE) + 30]}

def diff_report(before, after):
    """Every difference between two workflow files, keyed by what changed."""
    out = []
    bn = {n["id"]: n for n in before["nodes"]}
    an = {n["id"]: n for n in after["nodes"]}
    if set(bn) != set(an):
        out.append("node set changed: +%s -%s" % (sorted(set(an) - set(bn)), sorted(set(bn) - set(an))))
    for i in set(bn) & set(an):
        for k in set(bn[i]) | set(an[i]):
            if bn[i].get(k) != an[i].get(k) and k not in GEOM_KEYS:
                out.append("node %s: %s changed" % (i, k))
    if json.dumps(before.get("links"), sort_keys=True) != json.dumps(after.get("links"), sort_keys=True):
        out.append("links changed")
    def _shape(defs):                     # interior pos/size are geometry too (grow_sizes writes them); everything else is not
        return json.dumps([{k: ([{kk: vv for kk, vv in n.items() if kk not in GEOM_KEYS} for n in v] if k == "nodes" else v)
                            for k, v in d.items()} for d in (defs or {}).get("subgraphs", [])], sort_keys=True)
    if _shape(before.get("definitions")) != _shape(after.get("definitions")):
        out.append("definitions changed")
    bg = {g["title"]: g for g in before["groups"]}
    ag = {g["title"]: g for g in after["groups"]}
    if set(bg) != set(ag):
        out.append("group titles changed: +%s -%s" % (sorted(set(ag) - set(bg)), sorted(set(bg) - set(ag))))
    for t in set(bg) & set(ag):
        for k in set(bg[t]) | set(ag[t]):
            if bg[t].get(k) != ag[t].get(k) and k != "bounding":
                out.append("group %r: %s changed" % (t, k))
    return out

TITLE_MAX = 28

def shorten(t):
    if len(t) <= TITLE_MAX:
        return t
    if t in S.RENAME_OVERRIDE:
        return S.RENAME_OVERRIDE[t]
    head = t.split(" (")[0]
    if head and len(head) <= TITLE_MAX:
        return head
    parts = t.split(" · "); out = parts[0]
    for q in parts[1:]:
        if len(out + " · " + q) <= TITLE_MAX:
            out += " · " + q
        else:
            break
    return out

def rename_groups(wf):
    m = {}
    for g in wf["groups"]:
        new = S.RENAME_OVERRIDE.get(g["title"], shorten(g["title"]))
        if new != g["title"]:
            m[g["title"]] = new
            g["title"] = new
    seen = [g["title"] for g in wf["groups"]]
    assert len(seen) == len(set(seen)), "shortening collided two group titles"
    return m

# ---------------------------------------------------------------- banners
# The rgthree Label draws its text at its own fontSize and takes no notice of the node box, so a
# 40px banner in a 249x26 node paints straight across whatever is to its right. All five were
# doing it, which is most of the smearing across the top-left of the canvas. Size them to the
# text instead, and say what the canvas actually looks like now -- the old four read CONTROL DECK
# / PIPELINE / REFERENCE INPUTS / CONTINUE & CHAIN, which were four regions that no longer exist.

def fit_labels(wf):
    """Give every Label a box its own text fits in.

    Arial at fontSize F averages about 0.58F per character across mixed case, and the emoji run
    wider, so the estimate is deliberately generous -- a label slightly too wide costs nothing,
    one too narrow paints over its neighbours."""
    for n in wf["nodes"]:
        if n["type"] != "Label (rgthree)":
            continue
        f = float((n.get("properties") or {}).get("fontSize", 14))
        w = max(160, int(len(n["title"]) * f * 0.58) + 20)
        n["size"] = [snap(w), max(30, snap(int(f * 1.45)))]   # 30: the readable-size floor


def add_notes(wf):
    """The section notes must already exist; restructure.py writes them. Returns {} (nothing to adopt)."""
    have = {n["id"] for n in wf["nodes"] if n["type"] == "MarkdownNote"}
    missing = [i for i in S.NOTE_IDS if i not in have]
    assert not missing, f"section notes missing: {missing} -- run _build/restructure.py first"
    return {}



def fold_adoptions(wf):
    """node id -> home title: the declared fold homes and panel homes present in the file."""
    out = {}
    for sg in wf.get("definitions", {}).get("subgraphs", []) or []:
        if sg["name"] in S.FOLD_HOME:
            for n in wf["nodes"]:
                if n.get("type") == sg["id"]: out[n["id"]] = S.FOLD_HOME[sg["name"]]
    for n in wf["nodes"]:
        if (n.get("title") or "") in S.PANEL_HOME: out[n["id"]] = S.PANEL_HOME[n["title"]]
    return out


def build(wf, mem):
    """`mem` is passed in, not re-derived.

    Membership is read off the geometry, so anything that changes a node's box changes the
    answer. Widening the banner labels drops the two small ones out of the 280x110 frames they
    live in, and a node with no owner is never placed. One derivation, taken before any resize,
    is the authority for the whole run."""
    ctx = Ctx(wf)
    # innermost owner, then adoption, so every node is placed exactly once
    own = {}
    for t, ids in mem.items():
        for i in ids:
            if own.get(i) is None or len(mem[t]) < len(mem[own[i]]):
                own[i] = t
    for nid, t in list(S.ADOPT.items()) + list(fold_adoptions(wf).items()):
        hits = [x for x in mem if x.startswith(t)]
        own[nid] = hits[0] if hits else t
    for t, ids in S.NEW_GROUPS.items():
        for i in ids:
            own[i] = t
    owned = {}
    for i, t in own.items():
        owned.setdefault(t, []).append(i)

    # strip families are placed as cell bands, not as ordinary groups
    strip_titles, strip_blocks = set(), {}
    for marker, (cellkeys, spans) in S.STRIPS.items():
        fams = {}
        for title, ids in mem.items():
            if title.startswith(marker) and title[len(marker)] in "0123":
                fams[title[:len(marker) + 1]] = (title, ids)
        span_titles = [s[0] for s in spans if s[0] in fams]
        if len(span_titles) < 2:
            continue
        sets = {k: fams[k][1] for k in span_titles}
        allids = set().union(*sets.values())
        buckets = {}
        for i in allids:
            buckets.setdefault(tuple(sorted(k for k in sets if i in sets[k])), []).append(i)
        # map signature -> cell key, by how many spans claim it
        cells, used = [], set()
        for key in cellkeys:
            claim = [s[0] for s in spans if key in s[1]]
            sig = tuple(sorted(claim))
            ids = sorted(buckets.get(sig, []))
            cells.append((key, ids)); used |= set(ids)
        blk = Strip(cells, [(fams[t][0], keys, d) for t, keys, d in spans if t in fams])
        strip_blocks[marker] = blk
        strip_titles |= {fams[k][0] for k in span_titles}
    return ctx, mem, owned, strip_titles, strip_blocks

def group_block(title, ids, inner=(), aspect=0.45):
    """A frame's internal aspect follows the band it lives in.

    The reading band wraps like text, so a frame packed tall and narrow forces a row as tall as
    itself and wastes the whole width beside it -- the engine group came out 1,070 x 2,990 and
    made a 3,000px row out of a band of small ones. Wide and short there; tall and narrow in the
    machine band, where frames stack into dense columns instead."""
    return Group(title, Nodes(ids, aspect), inner)

def ranks(wf):
    """Longest-path rank per node, over the canvas links.

    This is what makes the canvas read left to right. Back edges are dropped while ranking
    rather than reported as an error: a handful exist by design (the analyzer reads the finished
    render and shows it beside the brief), and a layout that refuses to rank because of them is
    no use to anyone."""
    ids = {n["id"] for n in wf["nodes"]}
    edges = []
    for row in wf.get("links") or []:
        if isinstance(row, list) and len(row) >= 4: s_, t_ = row[1], row[3]
        elif isinstance(row, dict): s_, t_ = row.get("origin_id"), row.get("target_id")
        else: continue
        if s_ in ids and t_ in ids and s_ != t_: edges.append((s_, t_))
    rank = {i: 0 for i in ids}
    for _ in range(len(ids)):                      # relax; converges on a DAG, saturates on a cycle
        moved = False
        for s_, t_ in edges:
            if rank[t_] < rank[s_] + 1:
                rank[t_] = rank[s_] + 1; moved = True
        if not moved: break
    return rank

                           # 0.24 fill, this one in the narrowest canvas, which scans better
                           # 3400 0.237, 4200 0.240. 3800 gives 0.256 in the narrowest canvas.

def assemble(wf, ctx, mem, owned, strip_titles, strip_blocks):
    """Lanes in dataflow order, filled top to bottom.

    The first attempt placed a cockpit block of user-facing groups above a machine room ordered
    by a hand-written list. It cut the canvas by 2.4x and then made flow WORSE -- 36% of links
    backward became 46% -- because grouping by who-touches-it fights grouping by what-feeds-what.

    A well-laid graph gets zero backward links by making those the same ordering: its numbered path 1..7 IS
    the dataflow, inputs on the left and the result on the right. So the ordering here is the
    graph's, and the user-facing groups sort to the top of whichever lane their rank puts them
    in -- the path still reads 1..7 across the top, because that is genuinely where the work
    flows."""
    used = set(strip_titles)
    def blk_for(title):
        """Build a group and, recursively, every group nested inside it."""
        used.add(title)
        def key(i):                                                   # a 📖 note for a text field sorts just before its field
            pr = wf_nodes[i].get("properties") or {}; f = pr.get("note_for", pr.get("h3_for"))
            return ((rank.get(f, 0), _idkey(f), 0, _idkey(i)) if f in wf_nodes
                    else (rank.get(i, 0), _idkey(i), 1, _idkey(i)))
        owned[title] = sorted(owned.get(title, []), key=key)
        inner = []
        for other in sorted(NESTED_IN.get(title, ())):
            if other in mem and other not in used:
                inner.append(blk_for(other))
        asp = S.READING_ASPECT if cockpit_pos(title) < len(S.COCKPIT_ORDER) else S.MACHINE_ASPECT
        for pref, a in S.COCKPIT_ASPECT.items():
            if title.startswith(pref): asp = a
        return group_block(title, owned.get(title, []), inner, asp)

    rank = ranks(wf); wf_nodes = {n["id"]: n for n in wf["nodes"]}
    def cockpit_pos(title):
        for i, pref in enumerate(S.COCKPIT_ORDER):
            if title.startswith(pref): return i
        return len(S.COCKPIT_ORDER)

    # Rank the GROUPS, not the average of their members.
    #
    # A group is placed as one unit, so what decides whether its links run forward is the lane it
    # lands in relative to the groups feeding it -- not the mean rank of what is inside it. The
    # 🎞️ REFERENCES frame holds eighteen switches fed by the fifteen slot frames; averaged, it
    # sorted into a lane left of half its own inputs and every one of those links read backwards.
    unit_of = {}
    for marker, blk in strip_blocks.items():
        for _, ids in blk.cells:
            for i in ids: unit_of[i] = "strip:" + marker
    for t, ids in owned.items():
        for i in ids: unit_of.setdefault(i, t)
    uedges = set()
    for row in wf.get("links") or []:
        if isinstance(row, list) and len(row) >= 4: a, b = row[1], row[3]
        elif isinstance(row, dict): a, b = row.get("origin_id"), row.get("target_id")
        else: continue
        ua, ub = unit_of.get(a), unit_of.get(b)
        if ua and ub and ua != ub: uedges.add((ua, ub))
    # The group graph is CYCLIC: only 24 of 49 groups are orderable, with twelve mutually
    # dependent pairs (GEN SETTINGS <-> Processing, the engine <-> the analyzer, and so on).
    # That is not a defect to fix -- a group here is an rgthree toggle set, not a pipeline stage,
    # and toggle sets legitimately feed each other. So there is no ordering with zero backward
    # edges, and the job is to find one with as few as possible.
    #
    # Eades-Lin-Smyth greedy: repeatedly peel off sinks, then sources, then the vertex with the
    # largest out-degree minus in-degree. Weighted by how many links each group pair carries, so
    # an edge worth eight links outranks one worth a single link.
    weight = {}
    for row in wf.get("links") or []:
        if isinstance(row, list) and len(row) >= 4: a, b = row[1], row[3]
        elif isinstance(row, dict): a, b = row.get("origin_id"), row.get("target_id")
        else: continue
        ua, ub = unit_of.get(a), unit_of.get(b)
        if ua and ub and ua != ub:
            weight[(ua, ub)] = weight.get((ua, ub), 0) + 1
    verts = sorted(set(unit_of.values()))
    out_w = {v: 0 for v in verts}; in_w = {v: 0 for v in verts}
    for (a, b), w in weight.items():
        out_w[a] += w; in_w[b] += w
    # Every iteration here is over a SORTED sequence, never a set.
    #
    # Two runs of this tool on the same input produced different canvases, because the peel order
    # below reads a set of group titles and Python's string hashing is seeded per process. The
    # ties it breaks are common -- most frames have equal in and out weight at some point -- so
    # the result moved every run. A build tool that cannot reproduce itself is worse than none:
    # every diff is noise and no run can be trusted to be the one that was tested.
    left, right, remaining = [], [], set(verts)
    live = dict(weight)
    def drop(v):
        for (a, b) in sorted(e for e in live if v in e):
            w = live.pop((a, b))
            out_w[a] -= w; in_w[b] -= w
        remaining.discard(v)
    while remaining:
        moved = True
        while moved:
            moved = False
            for v in [x for x in sorted(remaining) if out_w[x] == 0]:
                right.append(v); drop(v); moved = True
            for v in [x for x in sorted(remaining) if in_w[x] == 0 and x in remaining]:
                left.append(v); drop(v); moved = True
        if remaining:
            v = max(sorted(remaining), key=lambda x: out_w[x] - in_w[x])
            left.append(v); drop(v)
    order = left + right[::-1]
    urank = {v: i for i, v in enumerate(order)}
    # A group with no links at all has out-degree zero, so the sink rule sweeps it to the far
    # right -- which is where the masthead ended up, 100% along a canvas it is meant to open.
    # Unlinked frames carry no dataflow, so they are ordered by their step instead.
    linked = {v for e in weight for v in e}

    # ---- the cockpit: declared rows of columns; a frame is placed here once, by prefix, with its nested children
    nested_children_all = set().union(*NESTED_IN.values()) if NESTED_IN else set()
    def matching(pref):
        return sorted(t for t in list(mem) + [x for x in S.NEW_GROUPS if x not in mem] if t.startswith(pref) and t not in used and t not in nested_children_all)
    rows = []
    for row in S.COCKPIT:
        cols = []
        for col in row:
            blocks = []
            for pref in col:
                for t in matching(pref):
                    owned[t] = sorted(owned.get(t, []), key=lambda i: (rank.get(i, 0), _idkey(i)))
                    blocks.append(blk_for(t))
            if blocks: cols.append(Col(blocks, gap=S.COCKPIT_GAP))
        if cols: rows.append(Row(cols, gap=S.COCKPIT_GAP))
    cockpit = Col(rows, gap=S.COCKPIT_ROW_GAP)

    entries = []                                    # (rank, is_machine, cockpit_index, title, block)
    for marker, blk in strip_blocks.items():
        entries.append((urank.get("strip:" + marker, 0), 1, 99, marker, blk))
    # Parents must be built before children, or whichever sorts first claims itself as a
    # top-level group and the other is left an empty shell. That is how Models lost all eight of
    # its nodes on the first run of this lane layout -- its child group sorted before it alphabetically.
    nested_children = set().union(*NESTED_IN.values()) if NESTED_IN else set()   # membership test only
    for t in sorted(list(mem) + [x for x in S.NEW_GROUPS if x not in mem]):
        if t in used or t in nested_children: continue
        owned[t] = sorted(owned.get(t, []), key=lambda i: (rank.get(i, 0), _idkey(i)))
        b = blk_for(t)
        subs = [t] + sorted(NESTED_IN.get(t, ()))
        cp = cockpit_pos(t)
        if not any(x in linked for x in subs):
            # No links at all -- the masthead, the panels, the LoRA rows. Eades reads a vertex
            # with no out-edges as a sink and sweeps it to the far right, which put the start
            # card and the control panel at 100% along a canvas they are meant to open. An
            # unlinked frame carries no dataflow, so it is placed by its step in the user's
            # sequence instead, spread proportionally along the same axis.
            span = max(urank.values()) if urank else 1
            r = (cp / max(len(S.COCKPIT_ORDER), 1)) * span if cp < len(S.COCKPIT_ORDER) else span
        else:
            r = max([urank.get(x, 0) for x in subs] or [0])
        entries.append((r, 0 if cp < len(S.COCKPIT_ORDER) else 1, cp, t, b))
    entries.sort(key=lambda e: (e[0], e[1], e[2]))

    # Two bands, ordered by different things on purpose.
    #
    # The reading band is everything a person touches, in the order they touch it -- the
    # handbook's own first-render sequence. It wraps like text, left to right then down, so the
    # nine image slots tile into a block instead of forming a 3,000px column, and the brief is
    # not at x=9,700 when it is the first thing you type. Frames here are packed wide and short
    # so a row stays low.
    #
    # Below it the plumbing keeps the lane structure: ordered by the graph, filled to a swept
    # height, packed on a skyline. Nobody reads it in sequence, so it is free to be dense rather
    # than legible, and lanes are markedly denser than a second wrapping band.
    #
    # Ordering the WHOLE canvas by dataflow was the previous attempt. It gives beautiful wiring
    # and a hostile interface: the controls came out 2,100px apart, the references split across
    # three columns, and the brief landed last -- which is the complaint this work started from.
    # One lane structure, but the frames a person touches claim the leftmost lanes, in the order
    # they are touched. The plumbing follows in dataflow order.
    #
    # Two full bands were tried instead and were worse on every measure -- 62 to 84 Mpx of canvas
    # against 46.7, and fill roughly halved -- because a wrapping band leaves a row as tall as
    # its tallest frame and wastes the width beside every short one. Lanes pack; bands read. This
    # keeps the packing and buys the reading order by choosing which lane a frame lands in rather
    # than by changing how lanes work.
    entries.sort(key=lambda e: (e[1], e[2] if e[1] == 0 else e[0]))

    lane_cap = max(S.MAX_LANE_H, cockpit.measure(ctx)[1])      # the machinery is no taller than the cockpit: one clean rectangle
    lanes, cur, cur_h = [], [], 0.0
    for e in entries:
        h = e[4].measure(ctx)[1]
        if cur and cur_h + GROUP_GAP + h > lane_cap:
            lanes.append(cur); cur, cur_h = [], 0.0
        cur.append(e); cur_h += h + GROUP_GAP
    if cur: lanes.append(cur)
    cols = []
    for lane in lanes:
        W = max(e[4].measure(ctx)[0] for e in lane)
        cols.append(Skyline([e[4] for e in lane], W))
    return Row([cockpit, Row(cols, gap=GROUP_GAP + 40)], gap=S.BAND_GAP)

NESTED_IN = {}      # filled by main() from the live NEST relationships

def derive_nesting(wf):
    """Which group frames currently sit inside which. Read off the canvas, not declared.

    The nesting is load-bearing (Models > engine > variant; the engine group > its rules)
    and is asserted by test_spacing_rules through its NEST allowlist, so it is preserved rather
    than re-decided here."""
    out = {}
    gs = [(g["title"], gbound(g)) for g in wf["groups"]]
    for ta, A in gs:
        for tb, B in gs:
            if ta != tb and contains(A, B):
                out.setdefault(ta, set()).add(tb)
    # keep only the immediate parent, so Models does not claim Turbo directly
    tight = {}
    for parent, kids in out.items():
        for k in kids:
            if not any(k in out.get(other, ()) for other in kids):
                tight.setdefault(parent, set()).add(k)
    return tight



def configure(tables):
    """Install a package's tables (a module namespace or dict; only UPPER_CASE names count) and load its size cache."""
    global S, SIZES, ESTIMATED
    src = tables if isinstance(tables, dict) else vars(tables)
    S = types.SimpleNamespace(**DEFAULTS)
    for k, v in src.items():
        if k.isupper() and k in DEFAULTS: setattr(S, k, v)
    if S.PKG_DIR is None and isinstance(src.get("__file__"), str): S.PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(src["__file__"])))
    if S.SIZES_PATH is None and S.PKG_DIR: S.SIZES_PATH = os.path.join(S.PKG_DIR, "_build", "sizes.json")
    SIZES = load_sizes(os.environ.get("BASE_SIZES") or S.SIZES_PATH); ESTIMATED.clear()
    return S


def configure_from_pkg(pkg_dir):
    """Load `<pkg>/_build/layout.py` as the tables module (without running it) and install it."""
    p = os.path.join(os.path.abspath(pkg_dir), "_build", "layout.py")
    spec = importlib.util.spec_from_file_location("pkg_layout_tables", p); mod = importlib.util.module_from_spec(spec)
    mod.__dict__["__name__"] = "pkg_layout_tables"; spec.loader.exec_module(mod)
    return configure(mod)


def default_workflow():
    from canvas import package_files
    return os.environ.get("BASE_WF") or package_files(S.PKG_DIR)["workflow"]


def run(tables, argv=None):
    """Entry point for a package's _build/layout.py: `run(globals())`."""
    configure(tables); main(argv)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--structure-sha" in argv:
        with open(default_workflow(), encoding="utf-8") as f: print(structure_sha(json.load(f)))
        return
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--wf", default=default_workflow())
    ap.add_argument("--verify", action="store_true", help="prove only geometry differs from git HEAD")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--strict-sizes", action="store_true", help="fail when _build/sizes.json is missing or stale (run _build/snapshot.py)")
    a = ap.parse_args(argv)

    with open(a.wf, encoding="utf8") as fh:
        raw = fh.read()
    before = json.loads(raw)
    wf = json.loads(raw)

    for nid, t in S.NODE_TITLES.items():                 # titles the package pins (bookmarks named for their keys)
        for n in wf["nodes"]:
            if n["id"] == nid: n["title"] = t
    renamed = rename_groups(wf)
    new_notes = add_notes(wf)
    for nid, gt in new_notes.items():
        S.ADOPT[nid] = gt
    global NESTED_IN
    NESTED_IN = derive_nesting(wf)
    # new frames for the nodes that had none
    have = {g["title"] for g in wf["groups"]}
    nextid = max([g.get("id", 0) for g in wf["groups"]] or [0]) + 1
    for t in S.NEW_GROUPS:
        if t not in have:
            wf["groups"].append({"id": nextid, "title": t, "bounding": [0, 0, 200, 100],
                                 "color": "#3f789e", "font_size": 24, "flags": {}})   # not #A88:
            # that colour is what the Reference Toggles panel matches on, and a frame wearing it
            # would put itself in its own list.
            nextid += 1

    before_mem = membership(wf)
    # After the snapshot, never before: widening a label can push it out of the small frame it
    # lives in -- 🎧1 and 🎬 Ref2VA are 280x110 -- and a node with no owner is never placed.
    fit_labels(wf)

    ctx, mem, owned, strip_titles, strip_blocks = build(wf, before_mem)
    tree = assemble(wf, ctx, mem, owned, strip_titles, strip_blocks)
    tree.measure(ctx)
    tree.place(ctx, 0, 0)

    # The two nodes that must touch no group get a deliberate gutter under the canvas. Leaving
    # them where they were is not "leaving them alone" -- everything else moves, so a group lands
    # on top of them, which is exactly what the guard reported on the first attempt.
    if ctx.rects:
        gy = max(r[3] for r in ctx.rects.values()) + 3 * GROUP_GAP
        gx = min(r[0] for r in ctx.rects.values())
        for k, nid in enumerate(sorted(S.KEEP_UNGROUPED)):
            if nid in ctx.node:
                ctx.put(nid, gx + k * (nsize(ctx.node[nid])[0] + GROUP_GAP), gy)

    placed = set(ctx.pos)
    missing = sorted({n["id"] for n in wf["nodes"]} - placed)
    if missing:
        sys.exit("layout placed no position for %d nodes: %s" % (len(missing), missing[:20]))

    apply(wf, ctx)
    # ---- the frontend's sizes are a floor (V6.0.0): grow what it would draw larger, then prove nothing is short
    grown = grow_sizes(wf)
    for nid, was, now in grown: print(f"  size {nid}: {was[0]:.0f}x{was[1]:.0f} -> {now[0]}x{now[1]} (the frontend draws it that big)")
    stale = SIZES is None or SIZES.get("workflow_structure_sha") != structure_sha(wf)
    msg = ("no _build/sizes.json" if SIZES is None else "stale" if stale else "current")
    print(f"sizes: cache {msg}, {len(ESTIMATED)} estimated, {len(grown)} grown")
    if a.strict_sizes and stale:
        print("sizes: refusing (--strict-sizes): run the base's snapshot.py for this package", file=sys.stderr); sys.exit(1)
    short = check_sizes(wf)
    if short:
        print("sizes: nodes still below the frontend's computed size:", short[:10], file=sys.stderr); sys.exit(1)

    # the guard that makes this safe to run: membership may not have changed
    # A group may gain only the nodes adoption declared. It may never lose one: a loss means a
    # node left the frame that toggles it, which is a behaviour change wearing a layout costume.
    allowed = {}
    for nid, pref in list(S.ADOPT.items()) + list(fold_adoptions(wf).items()):
        for t in before_mem:
            if t.startswith(pref):
                allowed.setdefault(t, set()).add(nid)
    after_mem = membership(wf)
    fold_loss = {}                                   # a homed fold instance may leave every frame that is not its home
    for nid, pref in fold_adoptions(wf).items():
        for t, ids in before_mem.items():
            if nid in ids and not t.startswith(pref): fold_loss.setdefault(t, set()).add(nid)
    drift = []
    for t, ids in before_mem.items():
        if t not in after_mem:
            continue
        lost = ids - after_mem[t] - S.ALLOWED_LOSS.get(t, set()) - fold_loss.get(t, set())
        gained = after_mem[t] - ids - allowed.get(t, set())
        if lost or gained:
            drift.append((t, sorted(lost), sorted(gained)))
    if drift:
        print("MEMBERSHIP DRIFT -- refusing to write. rgthree decides behaviour from these sets:")
        for t, lost, gained in drift[:12]:
            print("  %-52s lost %s gained %s" % (t[:52], lost, gained))
        sys.exit(1)
    adopted = sum(len(v) for v in allowed.values())
    print("membership preserved; %d declared adoptions applied" % adopted)

    changed = diff_report(before, wf)
    structural = [c for c in changed if not c.startswith("group titles changed")]
    print("non-geometry differences: %s" % (structural or "none"))
    if new_notes:
        print("added %d section notes (ids %s)" % (len(new_notes), sorted(new_notes)))
    if renamed:
        print("shortened %d group titles (longest was %d chars)"
              % (len(renamed), max(len(t) for t in renamed)))

    if a.verify:
        # Verify is two claims, not one. The first is that nothing outside geometry moved. The
        # second is that running the tool on its own output changes NOTHING -- which is the only
        # way to catch non-determinism, and the reason this exists: the feedback-arc-set pass
        # used to read a set of group titles, Python seeds string hashing per process, and two
        # runs on the same input produced different canvases. A diff against the previous run
        # would have looked like ordinary churn.
        drift = []
        bn = {n["id"]: n for n in before["nodes"]}
        for n in wf["nodes"]:
            o = bn.get(n["id"])
            if o and (list(o["pos"]) != list(n["pos"]) or list(o["size"]) != list(n["size"])):
                drift.append("node %s moved" % n["id"])
        bg = {g["title"]: g for g in before["groups"]}
        for g in wf["groups"]:
            o = bg.get(g["title"])
            if o and list(o["bounding"]) != list(g["bounding"]):
                drift.append("group %r moved" % g["title"][:28])
        if drift:
            print("NOT REPRODUCIBLE: a second run moved %d things (%s)"
                  % (len(drift), ", ".join(drift[:4])))
        sys.exit(0 if not structural and not drift else 1)
    if a.dry_run:
        print("dry run: nothing written")
        return
    with open(a.wf, "w", encoding="utf8") as fh:
        json.dump(wf, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print("wrote %s" % a.wf)

if __name__ == "__main__":
    ap0 = argparse.ArgumentParser(add_help=False); ap0.add_argument("--pkg", default=os.environ.get("BASE_PKG", os.getcwd()))
    a0, rest = ap0.parse_known_args()
    configure_from_pkg(a0.pkg); main(rest)
