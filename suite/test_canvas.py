"""unit: the canvas library (py/canvas.py) on a synthetic workflow -- the pieces every package's suite and tool lean on."""
import json, os, pathlib, sys
import pytest

BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "py"))
import canvas                                                          # noqa: E402

pytestmark = pytest.mark.unit


def _wf():
    """Two frames, a bypassed passthrough node, a subgraph with one instance, a note and a label."""
    return {
        "last_node_id": 9, "last_link_id": 4,
        "nodes": [
            {"id": 1, "type": "LoadImage", "pos": [20, 60], "size": [210, 100], "mode": 0, "flags": {}, "properties": {},
             "inputs": [], "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [1]}], "widgets_values": ["a.png", "image"]},
            {"id": 2, "type": "ImageScale", "pos": [300, 60], "size": [210, 100], "mode": 4, "flags": {}, "properties": {},
             "inputs": [{"name": "image", "type": "IMAGE", "link": 1}, {"name": "mask", "type": "MASK", "link": None}],
             "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [2]}, {"name": "MASK", "type": "MASK", "links": []}], "widgets_values": []},
            {"id": 3, "type": "SaveImage", "pos": [600, 60], "size": [210, 100], "mode": 0, "flags": {}, "properties": {},
             "inputs": [{"name": "images", "type": "IMAGE", "link": 2}], "outputs": [], "widgets_values": ["out"]},
            {"id": 4, "type": "MarkdownNote", "pos": [20, 260], "size": [300, 120], "mode": 0, "flags": {}, "properties": {}, "inputs": [], "outputs": [],
             "widgets_values": ["## Start\n**1** load a picture\n**2** queue"]},
            {"id": 5, "type": "Label (rgthree)", "pos": [20, 420], "size": [200, 30], "mode": 0, "flags": {}, "properties": {"fontSize": 14}, "inputs": [], "outputs": [], "title": "hello"},
            {"id": 6, "type": "sg-1", "pos": [600, 260], "size": [240, 100], "mode": 0, "flags": {}, "properties": {},
             "inputs": [{"name": "image", "type": "IMAGE", "link": 3}], "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": []}], "widgets_values": []},
        ],
        "links": [[1, 1, 0, 2, 0, "IMAGE"], [2, 2, 0, 3, 0, "IMAGE"], [3, 1, 0, 6, 0, "IMAGE"]],
        "groups": [{"id": 1, "title": "🖼️ inputs", "bounding": [0, 0, 260, 200], "color": "#A88", "font_size": 24},
                   {"id": 2, "title": "⚙️ machinery", "bounding": [280, 0, 600, 200], "color": "#3f789e", "font_size": 24}],
        "definitions": {"subgraphs": [{"id": "sg-1", "name": "Prep", "inputs": [{"name": "image", "type": "IMAGE", "linkIds": [3]}], "outputs": [{"name": "IMAGE", "type": "IMAGE"}],
                                       "nodes": [{"id": 7, "type": "ImageInvert", "pos": [0, 0], "size": [200, 60], "mode": 0, "flags": {}, "properties": {},
                                                  "inputs": [{"name": "image", "type": "IMAGE", "link": 4}], "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [5]}]}],
                                       "links": [{"id": 4, "origin_id": -10, "origin_slot": 0, "target_id": 7, "target_slot": 0, "type": "IMAGE"},
                                                 {"id": 5, "origin_id": 7, "origin_slot": 0, "target_id": -20, "target_slot": 0, "type": "IMAGE"}], "groups": []}]},
        "extra": {"ds": {"scale": 0.58, "offset": [50, 50]}, "version": "1.0.0"},
    }


def test_unit_byid_sees_through_subgraphs_and_annotates_interiors():
    wf = _wf(); B = canvas.byid(wf)
    assert 7 in B and B[7]["_canvas_pos"] == [600, 260] and B[7]["_canvas_size"] == [240, 100]
    assert canvas.nbound(B[7]) == (600, 230, 840, 360)                 # the instance's box, title bar included


def test_unit_links_resolve_subgraph_boundaries_to_one_hop():
    L = canvas.links(_wf())
    assert L[3][1:5] == [1, 0, 7, 0]                                   # outer link into the instance reads as producer -> interior consumer
    assert L.owner[7] == 6 and "sg-1" in L.inst[6]["id"]


def test_unit_bypass_slot_index_is_the_frontends():
    """ExecutableNodeDTO._getBypassSlotIndex: same index if compatible, else first exact type, else first compatible, else -1."""
    n = {"inputs": [{"name": "image", "type": "IMAGE"}, {"name": "mask", "type": "MASK"}], "outputs": [{"name": "IMAGE", "type": "IMAGE"}, {"name": "MASK", "type": "MASK"}, {"name": "S", "type": "STRING"}]}
    assert canvas.bypass_slot_index(n, 0, "IMAGE") == 0
    assert canvas.bypass_slot_index(n, 1, "MASK") == 1
    assert canvas.bypass_slot_index(n, 2, "STRING") == -1              # a STRING output with no STRING input dies
    assert canvas.bypass_slot_index(n, 2, "*") == 0                    # a wildcard consumer takes input 0 (frontend: slot if it exists, else 0)


def test_unit_resolve_frontend_passes_a_bypassed_node_through():
    wf = _wf(); B = canvas.byid(wf); L = canvas.links(wf)
    assert canvas.resolve(B, L, 2, 0) == (1, 0)                        # SaveImage's images come from LoadImage through the bypassed scaler
    B[2]["mode"] = 2
    assert canvas.resolve(B, L, 2, 0) is None                          # muted: nothing


def test_unit_membership_is_by_centre():
    wf = _wf(); m = canvas.membership(wf)
    assert m["🖼️ inputs"] == [1] and m["⚙️ machinery"] == [2, 3]      # the note (centre y 305) sits below the 200-tall inputs frame
    wf["nodes"][3]["pos"] = [20, 100]                                  # move its centre inside: now a member
    assert 4 in canvas.membership(wf)["🖼️ inputs"]
    wf["nodes"][5]["flags"] = {"collapsed": True}; wf["nodes"][5]["pos"] = [150, 190]; wf["nodes"][5]["size"] = [800, 800]   # stub centre (190, 175): inside
    assert 6 not in m["🖼️ inputs"] and 6 in canvas.membership(wf)["🖼️ inputs"]   # collapsed: the 80 px stub decides, not the stored size


def test_unit_structure_sha_ignores_geometry():
    wf = _wf(); a = canvas.structure_sha(wf)
    wf["nodes"][0]["pos"] = [999, 999]; wf["extra"]["ds"]["scale"] = 0.3
    assert canvas.structure_sha(wf) == a
    wf["nodes"][0]["title"] = "renamed"
    assert canvas.structure_sha(wf) != a


def test_unit_note_height_matches_the_suites_wrap_estimate():
    txt = "## Start\n**1** load a picture\n**2** queue"
    h = canvas.note_height(txt, 300)
    assert h % canvas.GRID == 0 and h >= int((1.6 + 1 + 1) * 18 + 14)


def test_unit_lod_cliff_is_the_frontends_formula():
    assert abs(canvas.lod_cliff(1) - 0.5714) < 1e-3 and abs(canvas.lod_cliff(2) - 0.4041) < 1e-3


def test_unit_load_spec_reads_a_declaration_by_ast(tmp_path):
    pkg = tmp_path / "Demo Package"; pkg.mkdir()
    (pkg / "Demo Package Workflow.json").write_text(json.dumps(_wf()), encoding="utf-8")
    (pkg / "suite.py").write_text('''"""Test suite for Demo (V1.0.0)."""
USER_FACING = ("🖼️", "⚙️")
MARKERS = ("⚙️",)
CANVAS = CanvasSpec(user_facing=USER_FACING, markers=MARKERS, start_card=4, display="1728x1117@2", budgets=Budgets(fill_min=0.1))
''', encoding="utf-8")
    spec, files = canvas.load_spec(pkg)
    assert spec.user_facing == ("🖼️", "⚙️") and spec.start_card == 4 and spec.dpr == 2 and spec.viewport == (1728, 1117)
    assert spec.budgets.fill_min == 0.1 and spec.budgets.title_max == 28
    assert files["workflow"].endswith("Demo Package Workflow.json") and files["sizes"].endswith(os.path.join("_build", "sizes.json"))


def test_unit_load_spec_refuses_a_name_that_is_not_a_literal(tmp_path):
    pkg = tmp_path / "P"; pkg.mkdir(); (pkg / "P Workflow.json").write_text("{}")
    (pkg / "suite.py").write_text("import os\nX = os.getcwd()\nCANVAS = CanvasSpec(user_facing=X)\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        canvas.load_spec(pkg)


def test_unit_structure_sha_handles_subgraph_scoped_ids():
    """A ProGrade-derived graph carries string node ids ("2682:0") beside integers. The sort
    key stringified only the FIRST tuple element, so hashing raised TypeError and every test
    that depends on the structure hash was unreachable — which is what blocked a package
    from adopting the kit at all."""
    wf = {"nodes": [{"id": 1, "type": "A", "inputs": [], "outputs": []},
                    {"id": "2682:0", "type": "B", "inputs": [], "outputs": []}],
          "links": [], "groups": []}
    first = canvas.structure_sha(wf)
    wf["nodes"].reverse()
    assert canvas.structure_sha(wf) == first, "the hash must not depend on node order"


def test_unit_a_package_may_declare_extra_zip_members():
    """Not every package ships the same five files. One also ships a trainer script and a
    models.py, both put in the zip by the base's own packager, so the shipped-pair check has
    to be told about them rather than failing on a correct zip."""
    assert canvas.CanvasSpec().extra_zip_members == ()
    assert canvas.CanvasSpec(extra_zip_members=("models.py",)).extra_zip_members == ("models.py",)


def test_unit_the_frontend_only_list_is_one_list():
    """smoke.py and canvas.py each kept a copy, and they drifted: smoke.py learned about
    KJNodes' Get/Set and the LC123 bypassers in 2.0.20, after a red pod run, and the canvas
    copy never did — so every canvas tool refused a ProGrade-derived graph as "not this
    package's testbed". One list now, and this is what keeps it that way."""
    import smoke
    assert smoke.FRONTEND_ONLY is canvas.FRONTEND_ONLY_TYPES
    for t in ("GetNode", "SetNode", "LC Bypasser", "LC Bypasser Panel", "LC Groups Bypasser"):
        assert t in canvas.FRONTEND_ONLY_TYPES, t
