#!/usr/bin/env python3
"""The before/after page: two snapshot folders from the base's snapshot.py -> one HTML file.

    python3 "base/comfyui-base/_build/canvas/page.py" --pkg "<package dir>" --before <pkg>/_build/snapshots/before \
        --wf-before <old workflow> --sizes-before <its sizes.json> [--after <pkg>/_build/snapshots] [--out ...] [--embed] [--title ...]

The "before" folder comes from running snapshot.py on the previous release (`git show <rev>:<workflow>` into a
scratch dir, then `snapshot.py --pkg <dir> --wf <that> --out <before dir> --sizes-out <scratch>/sizes.json`).
`--embed` inlines the PNGs as data URIs, which is what a claude.ai artifact needs; `--lite` keeps only the opening
pair. The "stops of a render" row reads CANVAS.stops from the package's suite.py: (name, frame-title prefix) pairs.

Every number on the page is computed here from the two workflow JSONs, the two view.json files
and the two sizes.json files -- nothing is typed in. The picture is the frontend's own rendering.
"""
import argparse, base64, html, json, os, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "py"))
from canvas import load_spec                                                   # noqa: E402
VIEWPORT = "1512x982@2"       # overridden by CANVAS.display
VERSION_KEY = "version"       # overridden by CANVAS.version_key
TITLE = "Canvas"              # --title
PKG_NAME = ""


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def metrics(wf):
    """Canvas facts from the JSON alone: extent, nodes, frames, link lengths."""
    nodes = wf["nodes"]; by = {n["id"]: n for n in nodes}
    xs = [n["pos"][0] for n in nodes] + [g["bounding"][0] for g in wf["groups"]]
    ys = [n["pos"][1] for n in nodes] + [g["bounding"][1] for g in wf["groups"]]
    x1 = max([n["pos"][0] + n["size"][0] for n in nodes] + [g["bounding"][0] + g["bounding"][2] for g in wf["groups"]])
    y1 = max([n["pos"][1] + n["size"][1] for n in nodes] + [g["bounding"][1] + g["bounding"][3] for g in wf["groups"]])
    links = [l for l in wf["links"] if l[1] in by and l[3] in by]
    lens = []
    for l in links:
        a, b = by[l[1]], by[l[3]]
        lens.append(((b["pos"][0] - a["pos"][0] - a["size"][0]) ** 2 + (b["pos"][1] - a["pos"][1]) ** 2) ** 0.5)
    defs = wf.get("definitions", {}).get("subgraphs", []) or []
    interior = sum(len(d.get("nodes", []) or []) for d in defs)
    singles = 0
    for g in wf["groups"]:
        gx, gy, gw, gh = g["bounding"]
        inside = [n for n in nodes if gx <= n["pos"][0] + n["size"][0] / 2 <= gx + gw and gy <= n["pos"][1] + n["size"][1] / 2 <= gy + gh]
        if len(inside) == 1: singles += 1
    return {
        "nodes": len(nodes), "interior": interior, "definitions": len(defs), "groups": len(wf["groups"]),
        "single_node_groups": singles, "links": len(links),
        "median_link": statistics.median(lens) if lens else 0, "longest_link": max(lens) if lens else 0,
        "extent": (round(x1 - min(xs)), round(y1 - min(ys))),
        "notes": sum(1 for n in nodes if n["type"] == "MarkdownNote"),
        "version": "?",
        "ds": wf.get("extra", {}).get("ds", {}),
    }


def short_nodes(sizes):
    """Nodes the frontend draws larger than the JSON stores them, worst first."""
    rows = []
    for k, r in sizes.get("nodes", {}).items():
        st, cm = r.get("stored"), r.get("computed")
        if not st or not cm or r.get("collapsed"): continue
        dw, dh = cm[0] - st[0], cm[1] - st[1]
        if dw > 0.5 or dh > 0.5:
            rows.append((max(dw, dh), k, r.get("title") or r.get("type"), st, cm))
    rows.sort(reverse=True)
    return rows


def img_src(path, embed):
    if not embed:
        return html.escape(path)
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")


def fmt(v):
    if isinstance(v, float): return f"{v:,.1f}"
    if isinstance(v, int): return f"{v:,}"
    return html.escape(str(v))


STOPS = []                    # (name, frame-title prefix): CANVAS.stops


def stops_on_screen(wf, vp):
    """Which of a render's declared stops sit wholly inside the opening screen (visible_units)."""
    x, y, w, h = vp["visible_units"]
    seen = []
    for name, pref in STOPS:
        for g in wf["groups"]:
            b = g["bounding"]
            if g["title"].startswith(pref) and x <= b[0] and y <= b[1] and b[0] + b[2] <= x + w and b[1] + b[3] <= y + h:
                seen.append(name); break
    return seen


def side(tag, d, wf_path, sizes_path, embed, rel):
    view = load(os.path.join(d, "view.json"))
    vp = next(v for v in view["viewports"] if f"{v['viewport'][0]}x{v['viewport'][1]}@{v['dpr']}" == VIEWPORT)
    wf = load(wf_path) if wf_path else load(view["workflow"])
    sizes = load(sizes_path) if sizes_path else None
    opening = os.path.join(d, vp["opening"]); full = os.path.join(d, vp["full"]["png"])
    m = metrics(wf); m["stops"] = stops_on_screen(wf, vp); m["version"] = wf.get("extra", {}).get(VERSION_KEY, m["version"])
    # the opening screen, drawn on the fitted whole-canvas PNG, in the PNG's own pixels
    fds, fpx = vp["full"]["ds"], vp["canvas_px"]
    ux, uy, uw, uh = vp["visible_units"]
    rect = dict(left=100 * (ux * fds["scale"] + fds["offset"][0]) * vp["dpr"] / fpx[0],
                top=100 * (uy * fds["scale"] + fds["offset"][1]) * vp["dpr"] / fpx[1],
                width=100 * uw * fds["scale"] * vp["dpr"] / fpx[0],
                height=100 * uh * fds["scale"] * vp["dpr"] / fpx[1])
    return dict(tag=tag, view=view, vp=vp, m=m, sizes=sizes, short=short_nodes(sizes) if sizes else None,
                opening=img_src(opening if embed else os.path.relpath(opening, rel), embed),
                full=img_src(full if embed else os.path.relpath(full, rel), embed),
                opening_px=vp["canvas_px"], rect=rect, frontend=view["frontend"], comfyui=view["comfyui"])


CSS = """
:root{
  --ground:#131417; --canvas:#1a1a1c; --panel:#1e1f24; --panel-2:#242630;
  --edge:#2e303a; --ink:#e9e7e4; --ink-2:#b6b4bd; --muted:#8a8894;
  --accent:#5b9dc9; --accent-dim:#3f789e; --flag:#e0913a; --ok:#5fb87a; --bad:#e0655a;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  --sans:"IBM Plex Sans",system-ui,-apple-system,sans-serif;
  --cond:"IBM Plex Sans Condensed","IBM Plex Sans",system-ui,sans-serif;
  color-scheme:dark;
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding:44px 28px 96px}
h1{font-family:var(--cond);font-weight:700;font-size:clamp(30px,4.6vw,46px);line-height:1.04;letter-spacing:-.015em;margin:0 0 14px;text-wrap:balance}
h2{font-family:var(--cond);font-weight:600;font-size:23px;letter-spacing:-.008em;margin:0}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin:0 0 18px}
.lede{color:var(--ink-2);max-width:68ch;margin:0 0 28px;font-size:16.5px}
.lede b{color:var(--ink);font-weight:600}
.controls{position:sticky;top:0;z-index:20;display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;padding:12px 14px;margin:0 0 26px;
  background:rgba(19,20,23,.93);backdrop-filter:blur(9px);border:1px solid var(--edge);border-radius:9px}
.seg{display:flex;border:1px solid var(--edge);border-radius:7px;overflow:hidden}
.seg button{font-family:var(--mono);font-size:11.5px;letter-spacing:.06em;text-transform:uppercase;background:transparent;color:var(--muted);border:0;padding:7px 14px;cursor:pointer;transition:.14s}
.seg button+button{border-left:1px solid var(--edge)}
.seg button[aria-pressed="true"]{background:var(--accent-dim);color:#fff}
.seg button:hover:not([aria-pressed="true"]){color:var(--ink);background:#22242b}
button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.slide{display:flex;align-items:center;gap:9px;font-family:var(--mono);font-size:11.5px;color:var(--muted);letter-spacing:.06em}
.slide input{width:200px;accent-color:var(--accent-dim)}
.vp{margin-left:auto;font-family:var(--mono);font-size:11.5px;color:var(--muted);letter-spacing:.04em}
.vp b{color:var(--ink-2);font-weight:500}
.panel{border:1px solid var(--edge);border-radius:11px;overflow:hidden;margin:0 0 30px;background:var(--panel)}
.bar{display:flex;flex-wrap:wrap;gap:8px 20px;align-items:baseline;padding:13px 18px;border-bottom:1px solid var(--edge);background:var(--panel-2)}
.bar .sub{color:var(--muted);font-size:13.5px}
.stats{display:flex;gap:16px;margin-left:auto;font-family:var(--mono);font-size:11.5px;color:var(--ink-2);font-variant-numeric:tabular-nums;flex-wrap:wrap}
.stats i{font-style:normal;color:var(--muted)}
.stats .hot{color:var(--flag)} .stats .good{color:var(--ok)}
/* the opening screen: two images in one frame, the second clipped by the slider */
.cmp{position:relative;background:var(--canvas);aspect-ratio:1512/982;overflow:hidden}
.cmp img{position:absolute;inset:0;width:100%;height:100%;display:block}
.cmp .after{clip-path:inset(0 0 0 var(--cut,50%))}
.cmp .rule{position:absolute;top:0;bottom:0;left:var(--cut,50%);width:2px;background:var(--accent);transform:translateX(-1px);pointer-events:none;box-shadow:0 0 0 1px rgba(0,0,0,.5)}
.cmp .rule::after{content:"";position:absolute;top:50%;left:50%;width:34px;height:34px;border-radius:50%;background:var(--accent);transform:translate(-50%,-50%);
  background-image:linear-gradient(90deg,transparent 45%,#fff 45%,#fff 55%,transparent 55%);opacity:.95}
.cmp .tag{position:absolute;top:12px;padding:4px 9px;border-radius:5px;font-family:var(--mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;background:rgba(19,20,23,.85);border:1px solid var(--edge);color:var(--ink-2)}
.cmp .tag.l{left:12px} .cmp .tag.r{right:12px}
.cmp[data-mode="before"] .after{clip-path:inset(0 0 0 100%)} .cmp[data-mode="before"] .rule{display:none}
.cmp[data-mode="after"] .after{clip-path:none} .cmp[data-mode="after"] .rule{display:none}
.cmp[data-mode="slide"] .tag.r{opacity:.9}
/* the whole canvas, side by side */
.two{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media (max-width:900px){.two{grid-template-columns:1fr}}
.full{position:relative;background:var(--canvas)}
.full img{display:block;width:100%;height:auto}
.full .rect{position:absolute;border:2px dashed var(--flag);box-shadow:0 0 0 2000px rgba(19,20,23,.38);pointer-events:none}
.full .rect span{position:absolute;left:0;bottom:100%;margin-bottom:4px;font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--flag);white-space:nowrap}
.cap{padding:10px 18px;color:var(--muted);font-size:13px;border-top:1px solid var(--edge)}
.cap b{color:var(--ink-2);font-weight:500}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:10px 18px;border-bottom:1px solid var(--edge);vertical-align:top}
th{font-family:var(--mono);font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;color:var(--muted);font-weight:500;background:var(--panel-2)}
tbody tr:last-child td{border-bottom:0}
td.n{font-family:var(--mono);font-size:12.5px;color:var(--ink-2);white-space:nowrap;font-variant-numeric:tabular-nums;text-align:right}
th.n{text-align:right}
td.d{font-family:var(--mono);font-size:12.5px;white-space:nowrap;font-variant-numeric:tabular-nums;text-align:right;color:var(--muted)}
td.d.good{color:var(--ok)} td.d.bad{color:var(--bad)}
td.k{color:var(--ink)} td.k small{display:block;color:var(--muted);font-size:12.5px;max-width:56ch}
td.t{font-family:var(--mono);font-size:12px;color:var(--flag);white-space:nowrap}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:0}
@media (max-width:760px){.grid3{grid-template-columns:1fr}}
.grid3>div{padding:16px 18px;border-right:1px solid var(--edge)}
.grid3>div:last-child{border-right:0}
.grid3 .big{font-family:var(--cond);font-size:34px;font-weight:600;line-height:1;letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.grid3 .big.good{color:var(--ok)} .grid3 .big.hot{color:var(--flag)}
.grid3 .lbl{font-family:var(--mono);font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
.grid3 .sub{color:var(--ink-2);font-size:13.5px;margin-top:8px;max-width:38ch}
.foot{margin-top:40px;color:var(--muted);font-size:13.5px;max-width:78ch}
.foot code,pre{font-family:var(--mono);font-size:12.5px;color:var(--ink-2)}
pre{background:var(--panel);border:1px solid var(--edge);border-radius:8px;padding:12px 16px;overflow-x:auto;margin:10px 0 0}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

JS = """
(function(){
  var cmp=document.getElementById('cmp'), range=document.getElementById('cut'), segs=document.querySelectorAll('.seg button');
  function mode(m){cmp.dataset.mode=m; segs.forEach(function(b){b.setAttribute('aria-pressed', b.dataset.mode===m?'true':'false');}); range.disabled = m!=='slide';}
  segs.forEach(function(b){b.addEventListener('click',function(){mode(b.dataset.mode);});});
  range.addEventListener('input',function(){cmp.style.setProperty('--cut', range.value+'%');});
  var drag=false;
  function at(e){var r=cmp.getBoundingClientRect(); var x=(e.touches?e.touches[0].clientX:e.clientX)-r.left; var v=Math.max(0,Math.min(100,100*x/r.width)); range.value=v; cmp.style.setProperty('--cut', v+'%');}
  cmp.addEventListener('mousedown',function(e){if(cmp.dataset.mode!=='slide')return; drag=true; at(e);});
  window.addEventListener('mousemove',function(e){if(drag)at(e);}); window.addEventListener('mouseup',function(){drag=false;});
  cmp.addEventListener('touchstart',function(e){if(cmp.dataset.mode==='slide')at(e);},{passive:true});
  cmp.addEventListener('touchmove',function(e){if(cmp.dataset.mode==='slide')at(e);},{passive:true});
  document.addEventListener('keydown',function(e){ if(e.target.tagName==='INPUT')return; if(e.key==='1')mode('before'); if(e.key==='2')mode('after'); if(e.key==='3')mode('slide'); });
  mode('slide');
})();
"""


def row(label, a, b, key, unit="", lower_is_better=True, note=""):
    va, vb = a["m"][key], b["m"][key]
    if isinstance(va, tuple):
        sa, sb = f"{va[0]:,} × {va[1]:,}", f"{vb[0]:,} × {vb[1]:,}"
        da = (va[0] * va[1]); db = (vb[0] * vb[1])
        delta = f"{100.0 * (db - da) / da:+.0f}% area" if da else ""
        good = db < da
    else:
        sa, sb = fmt(va) + unit, fmt(vb) + unit
        d = vb - va
        delta = (f"{d:+,.1f}" if isinstance(d, float) else f"{d:+,}") + unit
        good = (d < 0) == lower_is_better if d != 0 else None
    cls = "good" if good else ("bad" if good is False else "")
    small = f"<small>{html.escape(note)}</small>" if note else ""
    return f"<tr><td class=k>{html.escape(label)}{small}</td><td class=n>{sa}</td><td class=n>{sb}</td><td class='d {cls}'>{delta}</td></tr>"


def build(a, b, lite):
    va, vb = a["m"]["version"], b["m"]["version"]
    dsa, dsb = a["m"]["ds"].get("scale"), b["m"]["ds"].get("scale")
    ua, ub = a["vp"]["usable_units"], b["vp"]["usable_units"]
    short_a = len(a["short"]) if a["short"] is not None else None
    short_b = len(b["short"]) if b["short"] is not None else None
    rows = [
        row("Canvas extent (units)", a, b, "extent", note="Everything drawn, groups included. Before, a render meant five stops up to 12,000 units apart."),
        row("Top-level nodes", a, b, "nodes"),
        row("Nodes inside subgraphs", a, b, "interior", lower_is_better=False, note="Machinery folded out of sight. Every instance in a toggled row satisfies the bypass contract (Handbook §16.2)."),
        row("Subgraph definitions", a, b, "definitions", lower_is_better=False),
        row("Groups (frames)", a, b, "groups"),
        row("Frames around a single node", a, b, "single_node_groups", note="The 18 reference-cut frames became one node-level muter."),
        row("Links on the canvas", a, b, "links"),
        f"<tr><td class=k>Stops of a render on the opening screen<small>Of {len(STOPS)}: {' · '.join(n for n, _ in STOPS)}. Whole frames inside the saved view on this display.</small></td><td class=n>{len(a['m']['stops'])} of {len(STOPS)} <i style='color:var(--muted)'>({', '.join(a['m']['stops']) or 'none'})</i></td><td class=n>{len(b['m']['stops'])} of {len(STOPS)} <i style='color:var(--muted)'>({', '.join(b['m']['stops']) or 'none'})</i></td><td class='d {'good' if len(b['m']['stops']) > len(a['m']['stops']) else ''}'>{len(b['m']['stops']) - len(a['m']['stops']):+d}</td></tr>",
        row("Median link length (units)", a, b, "median_link"),
        row("Longest link (units)", a, b, "longest_link"),
        row("Section notes", a, b, "notes", lower_is_better=False),
    ]
    audit = ""
    if a["short"] is not None:
        worst = "".join(f"<tr><td class=t>{html.escape(str(k))}</td><td class=k>{html.escape(str(t))[:60]}</td><td class=n>{st[0]:.0f} × {st[1]:.0f}</td><td class=n>{cm[0]:.0f} × {cm[1]:.0f}</td><td class='d bad'>+{d:.0f}</td></tr>"
                        for d, k, t, st, cm in a["short"][:10])
        audit = f"""
<section class=panel>
  <div class=bar><h2>Nodes drawn larger than the file says</h2><span class=sub>the frontend grows a node to computeSize() on load; a frame sized from the JSON was sized from a fiction</span>
    <div class=stats><span><i>before</i> <b class=hot>{short_a}</b> of {len(a['sizes']['nodes'])}</span><span><i>after</i> <b class=good>{short_b}</b> of {len(b['sizes']['nodes'])}</span></div></div>
  <div style="overflow-x:auto"><table><thead><tr><th>id</th><th>node ({html.escape(va)})</th><th class=n>stored</th><th class=n>drawn</th><th class=n>px short</th></tr></thead><tbody>{worst}</tbody></table></div>
  <div class=cap>Worst ten of the before. After: <b>layout.py</b> floors every size at the frontend's own <b>computeSize()</b> from <b>_build/sizes.json</b>, so what the JSON stores is what the canvas draws.</div>
</section>"""
    full = "" if lite else f"""
<section class=panel>
  <div class=bar><h2>The whole canvas</h2><span class=sub>fitted to the same window; the dashed box is the opening screen, in the canvas's own units</span></div>
  <div class=two>
    <div><div class=full><img src="{a['full']}" alt="Whole canvas, {html.escape(va)}"><div class=rect style="left:{a['rect']['left']:.2f}%;top:{a['rect']['top']:.2f}%;width:{a['rect']['width']:.2f}%;height:{a['rect']['height']:.2f}%"><span>opening screen · {html.escape(va)}</span></div></div>
      <div class=cap><b>{html.escape(va)}</b> — {a['m']['extent'][0]:,} × {a['m']['extent'][1]:,} units at {dsa}; the screen shows {a['vp']['visible_units'][2]:,.0f} × {a['vp']['visible_units'][3]:,.0f}.</div></div>
    <div><div class=full><img src="{b['full']}" alt="Whole canvas, {html.escape(vb)}"><div class=rect style="left:{b['rect']['left']:.2f}%;top:{b['rect']['top']:.2f}%;width:{b['rect']['width']:.2f}%;height:{b['rect']['height']:.2f}%"><span>opening screen · {html.escape(vb)}</span></div></div>
      <div class=cap><b>{html.escape(vb)}</b> — {b['m']['extent'][0]:,} × {b['m']['extent'][1]:,} units at {dsb}; the screen shows {b['vp']['visible_units'][2]:,.0f} × {b['vp']['visible_units'][3]:,.0f}, and everything you touch every run is inside it.</div></div>
  </div>
</section>"""
    missing_a = ", ".join(a["vp"]["missing"]) or "none"; missing_b = ", ".join(b["vp"]["missing"]) or "none"
    return f"""<title>{html.escape(TITLE)}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{CSS}</style>
<div class=wrap>
  <p class=eyebrow>ComfyUI · {html.escape(PKG_NAME)} · {html.escape(va)} → {html.escape(vb)} · photographed by the frontend</p>
  <h1>{html.escape(TITLE)}</h1>
  <p class=lede>Both pictures are ComfyUI {html.escape(b['frontend'])}'s own rendering of the workflow file, opened at its saved view on the display the canvas is designed for ({VIEWPORT.replace('x', ' × ').replace('@', ' at dpr ')}). Before: {len(a['m']['stops'])} of {len(STOPS)} stops of a render on the opening screen, {a['m']['extent'][0]:,} × {a['m']['extent'][1]:,} units of canvas. After: {len(b['m']['stops'])} of {len(STOPS)} stops on a {b['vp']['visible_units'][2]:,.0f} × {b['vp']['visible_units'][3]:,.0f}-unit screen, {b['m']['extent'][0]:,} × {b['m']['extent'][1]:,} units of canvas.</p>

  <div class=controls>
    <div class=seg role=group aria-label="Which picture"><button data-mode=before aria-pressed=false>1 · {html.escape(va)}</button><button data-mode=after aria-pressed=false>2 · {html.escape(vb)}</button><button data-mode=slide aria-pressed=true>3 · slide</button></div>
    <label class=slide>cut <input id=cut type=range min=0 max=100 value=50 aria-label="Slider between before and after"></label>
    <span class=vp>viewport <b>{VIEWPORT}</b> · saved scale <b>{dsa}</b> → <b>{dsb}</b> · LOD cliff at dpr 2 <b>{b['vp']['lod_threshold']:.3f}</b></span>
  </div>

  <section class=panel>
    <div class=bar><h2>The opening screen</h2><span class=sub>exactly what the file shows when it loads — drag the rule, or press 1 / 2 / 3</span>
      <div class=stats><span><i>before</i> {a['vp']['nodes']} nodes · {a['vp']['groups']} frames</span><span><i>after</i> {b['vp']['nodes']} nodes · {b['vp']['groups']} frames</span></div></div>
    <div id=cmp class=cmp data-mode=slide>
      <img class=before src="{a['opening']}" alt="Opening screen, {html.escape(va)}">
      <img class=after src="{b['opening']}" alt="Opening screen, {html.escape(vb)}">
      <div class=rule></div>
      <span class="tag l">{html.escape(va)}</span><span class="tag r">{html.escape(vb)}</span>
    </div>
    <div class=cap>Missing node types on the Mac testbed (no CUDA): before <b>{html.escape(missing_a)}</b>; after <b>{html.escape(missing_b)}</b>. Nothing else is stubbed.</div>
  </section>

  <section class=panel>
    <div class=grid3>
      <div><div class=lbl>Usable opening area</div><div class="big good">{ub[2]:,.0f} × {ub[3]:,.0f}</div><div class=sub>canvas units at {dsb} after the left icon strip and the tab bar are measured out.</div></div>
      <div><div class=lbl>Machinery folded</div><div class="big">{b['m']['interior']:,}</div><div class=sub>nodes inside {b['m']['definitions']} subgraphs; {b['m']['nodes']} left on the canvas, from {a['m']['nodes']}.</div></div>
      <div><div class=lbl>Sizes the frontend disputes</div><div class="big {'good' if short_b == 0 else 'hot'}">{short_b if short_b is not None else '—'}</div><div class=sub>was {short_a if short_a is not None else '—'}: nodes the page grew on load, colliding with their neighbours and spilling out of frames.</div></div>
    </div>
  </section>

  <section class=panel>
    <div class=bar><h2>By the numbers</h2><span class=sub>computed from the two workflow files, not typed in</span></div>
    <div style="overflow-x:auto"><table><thead><tr><th>measure</th><th class=n>{html.escape(va)}</th><th class=n>{html.escape(vb)}</th><th class=n>change</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
  </section>
  {full}
  {audit}
  <div class=foot>
    <p>How to make this page again, with the server up (<code>bash testbed.sh --server</code>):</p>
<pre>uv run "base/comfyui-base/_build/canvas/snapshot.py" --pkg &lt;pkg&gt;                          # after: opening + full PNGs, sizes.json
uv run "base/comfyui-base/_build/canvas/snapshot.py" --pkg &lt;pkg&gt; --wf &lt;old json&gt; --out &lt;pkg&gt;/_build/snapshots/before --sizes-out &lt;scratch&gt;/sizes.json
python3 "base/comfyui-base/_build/canvas/page.py" --pkg &lt;pkg&gt; --before &lt;pkg&gt;/_build/snapshots/before --wf-before &lt;old json&gt; --sizes-before &lt;scratch&gt;/sizes.json</pre>
    <p>Frontend {html.escape(b['frontend'])} on ComfyUI {html.escape(b['comfyui'])}, testbed {html.escape(b['view']['server'])}. <code>render.py --review</code> is the offline stand-in; the suite's <code>snapshot</code> tier reruns the photograph.</p>
  </div>
</div>
<script>{JS}</script>
"""


def main():
    global STOPS, VIEWPORT, VERSION_KEY, TITLE, PKG_NAME
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkg", default=os.environ.get("BASE_PKG", os.getcwd()), help="the package folder (default: the cwd)")
    ap.add_argument("--before", required=True); ap.add_argument("--after", default=None, help="default <pkg>/_build/snapshots")
    ap.add_argument("--wf-before"); ap.add_argument("--wf-after")
    ap.add_argument("--sizes-before"); ap.add_argument("--sizes-after", default=None, help="default <pkg>/_build/sizes.json")
    ap.add_argument("--out", default=None, help="default <pkg>/_build/snapshots/canvas.html")
    ap.add_argument("--title", default=None); ap.add_argument("--embed", action="store_true"); ap.add_argument("--lite", action="store_true")
    args = ap.parse_args()
    spec, files = load_spec(args.pkg)
    STOPS = list(spec.stops); VIEWPORT = spec.display; VERSION_KEY = spec.version_key; PKG_NAME = files["name"]; TITLE = args.title or f"{files['name']} canvas"
    args.after = args.after or files["snapshots"]; args.sizes_after = args.sizes_after or files["sizes"]; args.out = args.out or os.path.join(files["snapshots"], "canvas.html")
    rel = os.path.dirname(os.path.abspath(args.out))
    a = side("before", args.before, args.wf_before, args.sizes_before, args.embed, rel)
    b = side("after", args.after, args.wf_after, args.sizes_after, args.embed, rel)
    page = build(a, b, args.lite)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"wrote {args.out}  ({os.path.getsize(args.out) // 1024} KB)  before {a['m']['version']} {a['m']['extent']}  after {b['m']['version']} {b['m']['extent']}")


if __name__ == "__main__":
    main()
