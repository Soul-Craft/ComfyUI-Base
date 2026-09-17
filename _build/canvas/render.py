#!/usr/bin/env python3
"""Draw a workflow package's canvas the way the frontend draws it, straight from the JSON.

    python3 "base/comfyui-base/_build/canvas/render.py" --pkg "<package dir>" --review

The ground truth is snapshot.py beside this file (a screenshot of the real frontend). This is the stand-in
for when no server is up -- CI, the pod, a review -- and it now draws what a person reads on the
canvas: widget rows with their values, slot names, bypassed rows in the frontend's magenta at 20 %,
muted at 40 %, notes with their text, subgraph instances with their promoted inputs, and the
rectangle the opening screen covers. Sizes come from the frontend's own computeSize() through
layout.py (sizes.json), so a box here is the box the pod shows.

    render.py --pkg <dir>                                       # report only
    render.py --pkg <dir> --review                              # numbered reading path, every budget, exit 1 on any FAIL
    render.py --pkg <dir> --out x.svg --subgraphs               # plus one SVG per subgraph definition
    render.py --pkg <dir> --against <pkg>/_build/snapshots/view.json   # orange = frontend draws it bigger than stored

Budgets and the reading order come from the package's `CANVAS = CanvasSpec(...)` (canvas.load_spec, by ast) and its
layout tables, the size floor from its _build/sizes.json through the layout engine, so picture and tests agree.
"""
import argparse, ast, json, math, os, sys
from collections import defaultdict
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "py"))
import canvaslayout as E                                                                     # noqa: E402  the layout engine: size floor, boxes
from canvaslayout import nsize, nbound, gbound, computed_min, SELF_DRAWN                     # noqa: E402
from canvas import TITLE_H, GRID, structure_sha, load_spec, package_files                    # noqa: E402
COCKPIT_ORDER = []            # the package's reading order (its layout tables), set in main()

SLOT_H, WIDGET_H, TEXT_SIZE, SUBTEXT = 20, 20, 14, 12
MIN_FONT_FOR_LOD = 8
FONT = "Inter, Helvetica, Arial, sans-serif"
CANVAS_BG, NODE_BG, NODE_TITLE, WIDGET_BG, TEXT, SUBTEXT_COLOR = "#1a1a1a", "#353535", "#333", "#222", "#ddd", "#aaa"
BYPASS_BG, BYPASS_ALPHA, MUTE_ALPHA, ERROR = "#FF00FF", 0.2, 0.4, "#E00"
LINK_COLORS = {"CLIP": "#FFD500", "CLIP_VISION": "#A8DADC", "CLIP_VISION_OUTPUT": "#ad7452", "CONDITIONING": "#FFA931",
               "CONTROL_NET": "#6EE7B7", "IMAGE": "#64B5F6", "LATENT": "#FF9CF9", "MASK": "#81C784", "MODEL": "#B39DDB",
               "STYLE_MODEL": "#C2FFAE", "VAE": "#FF6E6E", "NOISE": "#B0B0B0", "GUIDER": "#66FFFF", "SAMPLER": "#ECB4B4",
               "SIGMAS": "#CDFFCD", "TAESD": "#DCC274", "AUDIO": "#D6A7E8", "VIDEO": "#7FB8D8"}
LINK_DEFAULT = "#9A9A9A"

def lod_cliff(dpr=1): return MIN_FONT_FOR_LOD / (TEXT_SIZE * math.sqrt(dpr))

# ---------------------------------------------------------------- budgets: the suite's own numbers
def budgets(spec):
    """The package's declaration as the flat table the checks read."""
    b = spec.budgets
    return {"LOD_CLIFF_DESIGN": spec.lod_cliff_design, "DESIGN_DPR": spec.dpr, "CANVAS_MAX_H": b.canvas_max_h, "CANVAS_MAX_MPX_PER_NODE": b.canvas_max_mpx_per_node,
            "FILL_MIN": b.fill_min, "GROUP_BLOAT_MAX": b.group_bloat_max, "FLOW_MAX": b.flow_max, "GRID": b.grid, "TITLE_MAX": b.title_max,
            "NOTES_MIN": b.notes_min, "PALETTE_MAX": b.palette_max, "USER_FACING": tuple(spec.user_facing), "CUDA_ONLY": tuple(spec.cuda_only),
            "UNGROUPED_BY_DESIGN": set(spec.ungrouped_by_design)}
B = {}                        # filled in main()


# ---------------------------------------------------------------- geometry
def size_of(n): w, h = nsize(n); return w, h - TITLE_H
def overlaps(a, b): return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]
def contains(o, i): return o[0] <= i[0] and o[1] <= i[1] and o[2] >= i[2] and o[3] >= i[3]
def area(r): return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])
def intersect_area(a, b): return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))

def resolve_links(wf):
    """Top-level links as rows [id, src, sslot, dst, dslot, type], both ends on this canvas."""
    virtual = {"Label (rgthree)", "Bookmark (rgthree)", "Fast Groups Bypasser (rgthree)", "Fast Groups Muter (rgthree)", "Fast Muter (rgthree)", "Fast Bypasser (rgthree)", "MarkdownNote", "Note", "Reroute", "PrimitiveNode"}
    ids = {n["id"] for n in wf["nodes"] if n["type"] not in virtual}      # rgthree's own wiring (the cuts muter) is not dataflow; same rule as the suite
    out = []
    for row in wf.get("links") or []:
        if isinstance(row, list) and len(row) >= 5 and row[1] in ids and row[3] in ids and row[1] != row[3]: out.append(row)
    return out

def slot_y(node, i): return float(node["pos"][1]) + 10 + SLOT_H * i

# ---------------------------------------------------------------- report
def measure(wf, dpr=1):
    nodes, groups = wf["nodes"], wf.get("groups") or []
    boxes = {n["id"]: nbound(n) for n in nodes}
    gb = [(g, gbound(g)) for g in groups]
    xs0 = min(b[0] for b in boxes.values()); ys0 = min(b[1] for b in boxes.values())
    xs1 = max(b[2] for b in boxes.values()); ys1 = max(b[3] for b in boxes.values())
    for _, b in gb:
        xs0, ys0 = min(xs0, b[0]), min(ys0, b[1]); xs1, ys1 = max(xs1, b[2]), max(ys1, b[3])
    W, H = xs1 - xs0, ys1 - ys0
    node_area = sum(area(b) for b in boxes.values())
    def union(rects):
        xs = sorted({v for r in rects for v in (r[0], r[2])}); tot = 0.0
        for i in range(len(xs) - 1):
            x0, x1 = xs[i], xs[i + 1]
            spans = sorted((r[1], r[3]) for r in rects if r[0] <= x0 and r[2] >= x1)
            cy, h = None, 0.0
            for a0, b0 in spans:
                if cy is None or a0 > cy: h += b0 - a0; cy = b0
                elif b0 > cy: h += b0 - cy; cy = b0
            tot += (x1 - x0) * h
        return tot
    group_area = union([b for _, b in gb])
    ns = list(nodes); collisions = []
    for i, a in enumerate(ns):
        for b in ns[i + 1:]:
            if overlaps(boxes[a["id"]], boxes[b["id"]]): collisions.append((a["id"], b["id"]))
    partial, nested = [], []
    for i, (ga, ba) in enumerate(gb):
        for gc, bc in gb[i + 1:]:
            if not overlaps(ba, bc): continue
            (nested if contains(ba, bc) or contains(bc, ba) else partial).append((ga["title"], gc["title"], intersect_area(ba, bc)))
    orphans = [n["id"] for n in nodes if not any(contains(b, boxes[n["id"]]) for _, b in gb)]
    offgrid = [n["id"] for n in nodes if float(n["pos"][0]) % GRID or float(n["pos"][1]) % GRID]
    pos = {n["id"]: float(n["pos"][0]) for n in nodes}
    links = resolve_links(wf)
    back = [r for r in links if pos[r[1]] > pos[r[3]]]
    # the suite's measure (test_uiux_links_flow_forward): a backward link is genuine only between
    # two groups that HAVE a one-way order; toggle sets that feed each other are unorderable, not misplaced
    owner = {}
    for n in nodes:
        holding = [g for g, b in gb if contains(b, boxes[n["id"]])]
        if holding: owner[n["id"]] = min(holding, key=lambda g: g["bounding"][2] * g["bounding"][3])["title"]
    edges = {(owner[r[1]], owner[r[3]]) for r in links if r[1] in owner and r[3] in owner and owner[r[1]] != owner[r[3]]}
    genuine_all = [r for r in back if r[1] in owner and r[3] in owner and owner[r[1]] != owner[r[3]] and (owner[r[3]], owner[r[1]]) not in edges]
    # V6.0.0: a link between the cockpit and the machinery runs whichever way the reading order puts it; only a
    # back edge with BOTH ends on the same side of that gutter is a placement defect (suite.py, same rule)
    uf = tuple(B.get("USER_FACING") or ())
    def cockpit(t): return any(t.startswith(p) for p in uf)
    genuine = [r for r in genuine_all if cockpit(owner[r[1]]) == cockpit(owner[r[3]])]
    ds = ((wf.get("extra") or {}).get("ds") or {})
    notes = [n for n in nodes if n.get("type") in ("MarkdownNote", "Note")]
    titles = [g["title"] for g in groups]
    short = []
    for n in nodes:
        cm = computed_min(n)
        if cm is None or (n.get("flags") or {}).get("collapsed"): continue
        s = n["size"]; s = [s.get("0"), s.get("1")] if isinstance(s, dict) else s
        if float(s[0]) < cm[0] - 0.5 or float(s[1]) < cm[1] - 0.5: short.append(n["id"])
    return dict(nodes=len(nodes), groups=len(groups), width=W, height=H, mpx=W * H / 1e6,
                fill=node_area / (W * H) if W * H else 0.0, group_bloat=group_area / node_area if node_area else 0.0,
                collisions=collisions, partial=partial, nested=nested, orphans=orphans, offgrid=offgrid,
                links=len(links), backward=len(back), backward_frac=len(back) / len(links) if links else 0.0,
                genuine=len(genuine), genuine_frac=len(genuine) / len(links) if links else 0.0,
                genuine_all=len(genuine_all), genuine_all_frac=len(genuine_all) / len(links) if links else 0.0,
                scale=ds.get("scale"), offset=ds.get("offset"), lod_cliff_dpr1=lod_cliff(1), lod_cliff_dpr2=lod_cliff(2),
                low_quality=(ds.get("scale") is not None and ds["scale"] < lod_cliff(dpr)), notes=len(notes),
                title_max=max((len(t) for t in titles), default=0),
                title_med=sorted(len(t) for t in titles)[len(titles) // 2] if titles else 0,
                colors=len({g.get("color") for g in groups}), bounds=(xs0, ys0, xs1, ys1), short=short,
                sizes_state=("none" if E.SIZES is None else "stale" if E.SIZES.get("workflow_structure_sha") != structure_sha(wf) else "current"))

def checks():
    """The uiux budgets, read from suite.py. A budget the suite does not define is not asserted here."""
    g = B.get
    out = [("opens above the LOD cliff (designed display)", lambda m: m["scale"] is not None and m["scale"] >= g("LOD_CLIFF_DESIGN", g("LOD_CLIFF", 8 / 14)), f"dpr {g('DESIGN_DPR', 1)}: widget text is drawn at all")]
    if "CANVAS_MAX_H" in B: out.append((f"canvas height <= {B['CANVAS_MAX_H']:,}", lambda m: m["height"] <= B["CANVAS_MAX_H"], "tallest official template is 5,770"))
    if "CANVAS_MAX_MPX_PER_NODE" in B: out.append((f"canvas <= {B['CANVAS_MAX_MPX_PER_NODE']} Mpx per node", lambda m: m["mpx"] / m["nodes"] <= B["CANVAS_MAX_MPX_PER_NODE"], "corpus runs 0.25-0.37"))
    if "FILL_MIN" in B: out.append((f"fill >= {B['FILL_MIN']}", lambda m: m["fill"] >= B["FILL_MIN"], "0.055 before V5.7"))
    if "GROUP_BLOAT_MAX" in B: out.append((f"frames <= {B['GROUP_BLOAT_MAX']}x node area", lambda m: m["group_bloat"] <= B["GROUP_BLOAT_MAX"], "4.6x before V5.7"))
    if "FLOW_MAX" in B: out.append((f"genuine back edges <= {B['FLOW_MAX']:.0%}", lambda m: m["genuine_frac"] <= B["FLOW_MAX"], "same side of the cockpit gutter, between groups that have an order; the suite's own measure"))
    out += [("no node collisions", lambda m: not m["collisions"], ""),
            ("every node in a frame", lambda m: len(m["orphans"]) <= len(g("UNGROUPED_BY_DESIGN", (1, 2))), "two switches are exempt by rule"),
            ("every node on the grid", lambda m: not m["offgrid"], ""),
            ("no node below the frontend's size", lambda m: not m["short"], "sizes.json from _build/snapshot.py")]
    if "TITLE_MAX" in B: out.append((f"titles <= {B['TITLE_MAX']} chars", lambda m: m["title_max"] <= B["TITLE_MAX"], "91 before V5.7"))
    if "NOTES_MIN" in B: out.append((f">= {B['NOTES_MIN']} section notes", lambda m: m["notes"] >= B["NOTES_MIN"], ""))
    if "PALETTE_MAX" in B: out.append((f"<= {B['PALETTE_MAX']} frame colours", lambda m: m["colors"] <= B["PALETTE_MAX"], ""))
    return out
CHECKS = []                   # filled in main()

def print_checks(m):
    print(f"\nvisual review   (budgets from CANVAS: {', '.join(sorted(k for k in B if k not in ('USER_FACING', 'CUDA_ONLY', 'UNGROUPED_BY_DESIGN')))})")
    for name, fn, why in CHECKS: print(f"  {'PASS' if fn(m) else 'FAIL'}  {name:<40} {why}")
    return all(fn(m) for _, fn, _ in CHECKS)

def print_report(m):
    print(f"canvas         {m['width']:,.0f} x {m['height']:,.0f} px  ({m['mpx']:.1f} Mpx)")
    print(f"nodes/groups   {m['nodes']} nodes, {m['groups']} groups, {m['links']} top-level links")
    print(f"fill ratio     {m['fill']:.3f}   (official templates: median 0.602, min 0.192)")
    print(f"group bloat    {m['group_bloat']:.1f}x node area")
    print(f"flow           {m['backward']}/{m['links']} links run right-to-left ({m['backward_frac']:.1%}); {m['genuine_all']} genuine ({m['genuine_all_frac']:.1%}) between groups that have an order, "
          f"{m['genuine']} of them on one side of the cockpit gutter ({m['genuine_frac']:.1%})")
    print(f"orphans        {len(m['orphans'])} nodes in no group;  off-grid {len(m['offgrid'])};  collisions {len(m['collisions'])}")
    print(f"group overlap  {len(m['partial'])} partial, {len(m['nested'])} nested")
    print(f"notes          {m['notes']} MarkdownNote/Note;  titles median {m['title_med']} chars, longest {m['title_max']};  palette {m['colors']}")
    print(f"sizes          cache {m['sizes_state']}, {len(m['short'])} nodes below the frontend's computed size")
    print(f"saved view     scale {m['scale']}  offset {m['offset']};  LOD cliff {m['lod_cliff_dpr1']:.3f} at dpr 1, {m['lod_cliff_dpr2']:.3f} at dpr 2"
          f"   -> opens {'BELOW the cliff, widgets blank' if m['low_quality'] else 'above the cliff'}")

# ---------------------------------------------------------------- drawing
def esc(s): return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
def clip(s, w, px=7.2): n = max(1, int(w / px)); s = str(s); return s if len(s) <= n else s[:max(1, n - 1)] + "…"

def wrap(text, w, px=7.2):
    cpl = max(8, int((w - 24) / px)); out = []
    for para in str(text).split("\n"):
        line = ""
        for word in para.split(" "):
            if len(line) + len(word) + 1 > cpl and line: out.append(line); line = word
            else: line = (line + " " + word).strip()
        out.append(line)
    return out

def widget_rows(n):
    """(name, value) rows as the frontend lists them: named values when the file carries them,
    positional otherwise. A widget converted to an input is drawn as a slot, not a row."""
    wvn = n.get("widgets_values_named")
    linked = {i.get("widget", {}).get("name") for i in n.get("inputs", []) or [] if "widget" in i and i.get("link") is not None}
    if isinstance(wvn, dict): return [(k, v) for k, v in wvn.items() if k not in linked]
    wv = n.get("widgets_values")
    if isinstance(wv, dict): return list(wv.items())
    if isinstance(wv, list): return [("", v) for v in wv]
    return []

def fmt_val(v):
    if isinstance(v, float): return f"{v:g}"
    if isinstance(v, (dict, list)): return json.dumps(v, ensure_ascii=False)
    return str(v)

def draw_node(o, n, T, sizes_rec=None, cuda_only=()):
    bx0, by0, bx1, by1 = nbound(n)
    px, py = T(bx0, by0); w, h = bx1 - bx0, by1 - by0
    mode = n.get("mode", 0); collapsed = (n.get("flags") or {}).get("collapsed")
    typ = n.get("type") or ""
    if typ == "Label (rgthree)":
        pr = n.get("properties") or {}
        o.append(f'<text x="{px:.0f}" y="{py + TITLE_H + float(pr.get("fontSize", 12)) * 0.9:.0f}" fill="{pr.get("fontColor", "#ffffff")}" '
                 f'font-size="{pr.get("fontSize", 12)}" font-family="{FONT}">{esc(n.get("title") or "")}</text>')
        return
    alpha = BYPASS_ALPHA if mode == 4 else MUTE_ALPHA if mode == 2 else 1.0
    body_bg = BYPASS_BG if mode == 4 else (n.get("bgcolor") or NODE_BG)
    title_bg = n.get("color") or NODE_TITLE
    o.append(f'<g opacity="{alpha}">')
    if typ in cuda_only: o.append(f'<rect x="{px-5:.0f}" y="{py-5:.0f}" width="{w+10:.0f}" height="{h+10:.0f}" fill="none" stroke="{ERROR}" stroke-width="10" rx="12" opacity="0.6"/>')
    o.append(f'<rect x="{px:.0f}" y="{py:.0f}" width="{w:.0f}" height="{h:.0f}" fill="{body_bg}" stroke="#000" stroke-opacity="0.35" rx="8"/>')
    o.append(f'<rect x="{px:.0f}" y="{py:.0f}" width="{w:.0f}" height="{TITLE_H}" fill="{title_bg}" rx="8"/>')
    if not collapsed: o.append(f'<rect x="{px:.0f}" y="{py + TITLE_H - 8:.0f}" width="{w:.0f}" height="8" fill="{title_bg}"/>')
    sg = n.get("_subgraph")
    glyph = "⧉ " if sg else ""
    o.append(f'<text x="{px + 10:.0f}" y="{py + 20:.0f}" fill="{TEXT}" font-size="{TEXT_SIZE}" font-family="{FONT}">{esc(glyph + clip(n.get("title") or typ, w - 20, 8.2))}</text>')
    if collapsed: o.append("</g>"); return
    yb = py + TITLE_H
    ins = [i for i in n.get("inputs", []) or []]
    outs = n.get("outputs", []) or []
    slot_rows = [i for i in ins if "widget" not in i or i.get("link") is not None]
    for k, i in enumerate(ins):
        cy = yb + 10 + SLOT_H * k
        col = LINK_COLORS.get(i.get("type"), LINK_DEFAULT); linked = i.get("link") is not None
        o.append(f'<circle cx="{px + 8:.0f}" cy="{cy:.0f}" r="4" fill="{col if linked else "none"}" stroke="{col}" stroke-width="1.5"/>')
        o.append(f'<text x="{px + 18:.0f}" y="{cy + 4:.0f}" fill="{TEXT if linked else SUBTEXT_COLOR}" font-size="{SUBTEXT}" font-family="{FONT}">{esc(clip(i.get("label") or i.get("name") or "", w / 2 - 20, 6.5))}</text>')
    for k, out in enumerate(outs):
        cy = yb + 10 + SLOT_H * k
        col = LINK_COLORS.get(out.get("type"), LINK_DEFAULT)
        o.append(f'<circle cx="{px + w - 8:.0f}" cy="{cy:.0f}" r="4" fill="{col if out.get("links") else "none"}" stroke="{col}" stroke-width="1.5"/>')
        o.append(f'<text x="{px + w - 18:.0f}" y="{cy + 4:.0f}" fill="{TEXT}" font-size="{SUBTEXT}" font-family="{FONT}" text-anchor="end">{esc(clip(out.get("label") or out.get("name") or "", w / 2 - 20, 6.5))}</text>')
    rows_h = max(len(ins), len(outs), 0) * SLOT_H
    if typ in ("MarkdownNote", "Note"):
        text = (n.get("widgets_values") or [""])[0] if isinstance(n.get("widgets_values"), list) else ""
        y = yb + 18
        for line in wrap(text, w)[: max(1, int((h - TITLE_H - 10) / 18))]:
            bold = line.startswith("#")
            o.append(f'<text x="{px + 12:.0f}" y="{y:.0f}" fill="{TEXT}" font-size="{13 if not bold else 15}" font-weight="{"bold" if bold else "normal"}" font-family="{FONT}">{esc(line.lstrip("# "))}</text>')
            y += 18
        o.append("</g>"); return
    y = yb + rows_h + 4
    for name, val in widget_rows(n):
        v = fmt_val(val)
        multiline = ("\n" in v) or (len(v) > 60 and (typ.endswith("Multiline") or "Text" in typ))
        if multiline:
            hh = max(WIDGET_H * 3, min(h - (y - py) - 6, 18 * len(wrap(v, w - 20)) + 12))
            if y + hh > py + h: hh = max(0, py + h - y - 4)
            if hh <= 0: break
            o.append(f'<rect x="{px + 10:.0f}" y="{y:.0f}" width="{w - 20:.0f}" height="{hh:.0f}" fill="{WIDGET_BG}" rx="4"/>')
            yy = y + 15
            for line in wrap(v, w - 20)[: max(1, int((hh - 6) / 16))]:
                o.append(f'<text x="{px + 16:.0f}" y="{yy:.0f}" fill="{TEXT}" font-size="{SUBTEXT}" font-family="monospace">{esc(clip(line, w - 32, 6.6))}</text>'); yy += 16
            y += hh + 4
        else:
            if y + WIDGET_H > py + h + 1: break
            o.append(f'<rect x="{px + 10:.0f}" y="{y:.0f}" width="{w - 20:.0f}" height="{WIDGET_H}" fill="{WIDGET_BG}" rx="10"/>')
            if name: o.append(f'<text x="{px + 20:.0f}" y="{y + 14:.0f}" fill="{SUBTEXT_COLOR}" font-size="{SUBTEXT}" font-family="{FONT}">{esc(clip(name, w / 2 - 24, 6.5))}</text>')
            o.append(f'<text x="{px + w - 20:.0f}" y="{y + 14:.0f}" fill="{TEXT}" font-size="{SUBTEXT}" font-family="{FONT}" text-anchor="end">{esc(clip(v, w / 2 - 12 if name else w - 40, 6.5))}</text>')
            y += WIDGET_H + 4
    o.append("</g>")

def draw_links(o, wf, nodes, T, flow):
    pos = {i: float(n["pos"][0]) for i, n in nodes.items()}
    for row in resolve_links(wf):
        _, s, ss, d, dsl, *rest = row
        a, b = nodes[s], nodes[d]
        aw, _ = size_of(a)
        ax, ay = T(float(a["pos"][0]) + aw, slot_y(a, ss if isinstance(ss, int) else 0))
        bx, by = T(float(b["pos"][0]), slot_y(b, dsl if isinstance(dsl, int) else 0))
        typ = rest[0] if rest else None
        backward = pos[s] > pos[d]
        col = LINK_COLORS.get(typ, LINK_DEFAULT)
        wdt, op = (2.5, 0.8) if (backward and flow) else (1.6, 0.55)
        if backward and flow: col = "#e0533d"
        dx = max(40.0, abs(bx - ax) * 0.4)
        o.append(f'<path d="M{ax:.0f},{ay:.0f} C{ax+dx:.0f},{ay:.0f} {bx-dx:.0f},{by:.0f} {bx:.0f},{by:.0f}" fill="none" stroke="{col}" stroke-width="{wdt}" stroke-opacity="{op}"/>')

def render(wf, m, overlays, view=None, viewport=None, cuda_only=(), against=None, mark_subgraphs=True):
    x0, y0, x1, y1 = m["bounds"]
    pad = 120
    W, H = (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad
    T = lambda x, y: (x - x0 + pad, y - y0 + pad)
    scale = m["scale"] or 1.0
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W * scale:.0f}" height="{H * scale:.0f}" viewBox="0 0 {W:.0f} {H:.0f}" font-family="{FONT}">',
         f'<rect width="{W:.0f}" height="{H:.0f}" fill="{CANVAS_BG}"/>']
    for g in wf.get("groups") or []:
        gx, gy, gx1, gy1 = gbound(g); px, py = T(gx, gy); col = g.get("color") or "#3f789e"; fs = g.get("font_size") or 24
        o.append(f'<rect x="{px:.0f}" y="{py:.0f}" width="{gx1-gx:.0f}" height="{gy1-gy:.0f}" fill="{col}" fill-opacity="0.25" stroke="{col}" stroke-width="1.5" rx="4"/>')
        o.append(f'<rect x="{px:.0f}" y="{py:.0f}" width="{gx1-gx:.0f}" height="{fs + 6:.0f}" fill="{col}" fill-opacity="0.25"/>')
        o.append(f'<text x="{px + fs / 2:.0f}" y="{py + fs - 2:.0f}" fill="#ddd" font-size="{fs}" font-family="{FONT}">{esc(g["title"])}</text>')
    nodes = {n["id"]: n for n in wf["nodes"]}
    if mark_subgraphs:
        ids = {sg["id"] for sg in wf.get("definitions", {}).get("subgraphs", []) or []}
        for n in nodes.values():
            if n.get("type") in ids: n["_subgraph"] = True
    draw_links(o, wf, nodes, T, "flow" in overlays)
    for n in wf["nodes"]: draw_node(o, n, T, cuda_only=cuda_only)
    if against:
        for n in wf["nodes"]:
            cm = computed_min(n)
            if cm is None: continue
            s = n["size"]; s = [s.get("0"), s.get("1")] if isinstance(s, dict) else s
            if float(s[0]) < cm[0] - 0.5 or float(s[1]) < cm[1] - 0.5:
                px, py = T(float(n["pos"][0]), float(n["pos"][1]) - TITLE_H)
                o.append(f'<rect x="{px:.0f}" y="{py:.0f}" width="{cm[0]:.0f}" height="{cm[1] + TITLE_H:.0f}" fill="none" stroke="#ff9f1c" stroke-width="4" stroke-dasharray="10 6"/>')
    if "overlap" in overlays:
        gb = [(g, gbound(g)) for g in wf.get("groups") or []]
        for i, (ga, ba) in enumerate(gb):
            for gc, bc in gb[i + 1:]:
                if not overlaps(ba, bc) or contains(ba, bc) or contains(bc, ba): continue
                ix0, iy0 = max(ba[0], bc[0]), max(ba[1], bc[1]); ix1, iy1 = min(ba[2], bc[2]), min(ba[3], bc[3]); px, py = T(ix0, iy0)
                o.append(f'<rect x="{px:.0f}" y="{py:.0f}" width="{ix1-ix0:.0f}" height="{iy1-iy0:.0f}" fill="none" stroke="#e0533d" stroke-width="3" stroke-dasharray="12 8"/>')
    # the opening viewport: what the person sees first
    vp = None
    if view and view.get("viewports"):
        r = view["viewports"][0]; vp = r.get("usable_units") or r.get("visible_units"); label = f"opening screen {r['viewport'][0]}x{r['viewport'][1]}@{r['dpr']} (measured)"
    elif viewport and m["offset"]:
        vw, vh = viewport; L, Tt = 76, 52
        vp = [L / scale - m["offset"][0], Tt / scale - m["offset"][1], (vw - L) / scale, (vh - 67 - Tt) / scale]; label = f"opening screen {vw}x{vh} (assumed chrome)"
    if vp:
        px, py = T(vp[0], vp[1])
        o.append(f'<rect x="{px:.0f}" y="{py:.0f}" width="{vp[2]:.0f}" height="{vp[3]:.0f}" fill="none" stroke="#ffffff" stroke-width="4" stroke-dasharray="24 12" opacity="0.8"/>')
        o.append(f'<text x="{px + 12:.0f}" y="{py + vp[3] - 14:.0f}" fill="#fff" font-size="28" opacity="0.8">{esc(label)}</text>')
    if "path" in overlays:
        step = {}
        for g in wf.get("groups") or []:
            for i, pref in enumerate(COCKPIT_ORDER):
                if g["title"].startswith(pref): step.setdefault(i, []).append(g); break
        k = 0
        for i in sorted(step):
            for g in sorted(step[i], key=lambda g: (g["bounding"][0], g["bounding"][1])):
                k += 1; gx, gy, _, _ = gbound(g); px, py = T(gx, gy)
                o.append(f'<circle cx="{px+34:.0f}" cy="{py+34:.0f}" r="30" fill="#e8a33a"/>')
                o.append(f'<text x="{px+34:.0f}" y="{py+46:.0f}" fill="#111" font-size="34" text-anchor="middle" font-weight="bold">{k}</text>')
    y = 46
    o.append('<rect x="24" y="20" width="1240" height="260" fill="#000" fill-opacity="0.72" rx="8"/>')
    for line in [f'{m["width"]:,.0f} x {m["height"]:,.0f} px   {m["mpx"]:.1f} Mpx   {m["nodes"]} nodes   {m["groups"]} groups   sizes {m["sizes_state"]}',
                 f'fill {m["fill"]:.3f}   groups {m["group_bloat"]:.1f}x their contents   {len(m["orphans"])} orphans   {len(m["offgrid"])} off-grid   {len(m["short"])} short',
                 f'flow {m["backward"]}/{m["links"]} links right-to-left ({m["backward_frac"]:.0%}), {m["genuine"]} genuine ({m["genuine_frac"]:.1%})',
                 f'saved scale {m["scale"]}   LOD cliff {m["lod_cliff_dpr1"]:.3f} (dpr1) / {m["lod_cliff_dpr2"]:.3f} (dpr2)',
                 ('OPENS BELOW THE CLIFF - widget text is not drawn' if m["low_quality"] else 'opens above the cliff')]:
        o.append(f'<text x="44" y="{y}" fill="#eee" font-size="28">{esc(line)}</text>'); y += 44
    if "checks" in overlays:
        y2 = 340
        o.append(f'<rect x="24" y="300" width="1240" height="{28+46*len(CHECKS)}" fill="#000" fill-opacity="0.72" rx="8"/>')
        for name, fn, why in CHECKS:
            ok = fn(m)
            o.append(f'<text x="44" y="{y2}" fill="{"#7fd67f" if ok else "#e0533d"}" font-size="28">{"PASS" if ok else "FAIL"}  {esc(name)}{("   " + esc(why)) if why else ""}</text>'); y2 += 46
    o.append("</svg>")
    return "\n".join(o)

def render_subgraph(sg, wf, cuda_only=()):
    """One definition as the frontend shows it when entered: its nodes, its IO nodes, its links."""
    nodes = list(sg.get("nodes", []) or [])
    fake = {"nodes": nodes, "groups": sg.get("groups", []) or [], "links": [[lk["id"], lk["origin_id"], lk["origin_slot"], lk["target_id"], lk["target_slot"], lk["type"]] for lk in sg.get("links", []) or []], "extra": {"ds": {"scale": 1.0, "offset": [0, 0]}}}
    ionodes = []
    for key, iid, slots in (("inputNode", -10, sg.get("inputs", [])), ("outputNode", -20, sg.get("outputs", []))):
        b = (sg.get(key) or {}).get("bounding") or [0, 0, 148, 60]
        ionodes.append({"id": iid, "type": "Subgraph " + ("inputs" if iid == -10 else "outputs"), "title": sg["name"] + (" ▸ in" if iid == -10 else " ▸ out"),
                        "pos": [b[0], b[1] + TITLE_H], "size": [b[2], max(b[3], 20 * len(slots) + 20)], "mode": 0, "flags": {},
                        "inputs": [] if iid == -10 else [{"name": s["name"], "type": s["type"], "link": 1} for s in slots],
                        "outputs": [{"name": s["name"], "type": s["type"], "links": [1]} for s in slots] if iid == -10 else [], "color": "#223", "bgcolor": "#335"})
    fake["nodes"] = nodes + ionodes
    m = measure(fake)
    return render(fake, m, set(), cuda_only=cuda_only, mark_subgraphs=False)

# ---------------------------------------------------------------- main
def main():
    global B, CHECKS, COCKPIT_ORDER
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pkg", default=os.environ.get("BASE_PKG", os.getcwd()), help="the package folder (default: the cwd)")
    ap.add_argument("--wf", default=None, help="the workflow (default: the package's '* Workflow.json', or $BASE_WF)")
    ap.add_argument("--out")
    ap.add_argument("--overlay", default="none", help="none | all | comma list of: flow, overlap, path, checks")
    ap.add_argument("--review", action="store_true", help="the standard visual check: numbered reading path, every budget, exit 1 on any FAIL")
    ap.add_argument("--against", help="_build/snapshots/view.json: draw the measured opening screen and every box the frontend draws bigger than stored")
    ap.add_argument("--viewport", default=None, help="assumed screen when no view.json is given (css px); default CANVAS.display")
    ap.add_argument("--subgraphs", action="store_true", help="also write review-sg-<id8>.svg per definition")
    ap.add_argument("--dpr", type=int, default=None, help="default CANVAS.display's dpr")
    a = ap.parse_args()
    spec, files = load_spec(a.pkg)
    a.wf = a.wf or os.environ.get("BASE_WF") or files["workflow"]
    a.viewport = a.viewport or spec.display.partition("@")[0]; a.dpr = a.dpr or spec.dpr
    B = budgets(spec); CHECKS = checks()
    if os.path.exists(files["layout"]): E.configure_from_pkg(files["dir"]); COCKPIT_ORDER = list(E.S.COCKPIT_ORDER) or list(spec.user_facing)
    else: E.configure({"PKG_DIR": files["dir"]}); COCKPIT_ORDER = list(spec.user_facing)
    with open(a.wf, encoding="utf8") as fh: wf = json.load(fh)
    view = None
    vpath = a.against or os.path.join(files["snapshots"], "view.json")
    if os.path.exists(vpath):
        try: view = json.loads(Path(vpath).read_text(encoding="utf-8"))
        except ValueError: view = None
    m = measure(wf, a.dpr)
    print(os.path.basename(a.wf)); print_report(m)
    ok = print_checks(m) if a.review else True
    if a.review and not a.out: a.out = os.path.join(files["dir"], "_build", "review.svg")
    if a.review: a.overlay = "all"
    cuda_only = tuple(B.get("CUDA_ONLY") or ())
    if a.out:
        ov = ({"flow", "overlap", "path", "checks"} if a.overlay == "all" else set() if a.overlay == "none" else {s.strip() for s in a.overlay.split(",")})
        vw, vh = (int(v) for v in a.viewport.lower().split("x"))
        with open(a.out, "w", encoding="utf8") as fh: fh.write(render(wf, m, ov, view=view, viewport=(vw, vh), cuda_only=cuda_only, against=bool(a.against) or view is not None))
        print(f"\nwrote {a.out}  ({os.path.getsize(a.out)/1024:.0f} KB)" + (f"   opening screen from {vpath}" if view else "   opening screen assumed"))
        if a.subgraphs:
            for sg in wf.get("definitions", {}).get("subgraphs", []) or []:
                p = os.path.join(os.path.dirname(a.out), f"review-sg-{sg['id'][:8]}.svg")
                with open(p, "w", encoding="utf8") as fh: fh.write(render_subgraph(sg, wf, cuda_only))
            print(f"wrote {len(wf['definitions']['subgraphs'])} subgraph SVGs beside it")
    if a.review and not ok: sys.exit(1)

if __name__ == "__main__": main()
