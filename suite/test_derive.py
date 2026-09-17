"""unit: the derivation library (py/derive.py), the asserted replacements one brand's package is derived
from another's sources with. Every test runs against strings or a throwaway tree under tmp_path."""
import os, pathlib, sys
import pytest

BASE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "py"))
import derive                                                          # noqa: E402
from derive import DeriveError, Derivation, cut_block, replace_all, replace_once   # noqa: E402


# --------------------------------------------------------------------------- replace_once
@pytest.mark.unit
def test_replace_once_success():
    assert replace_once("a PKG=old b", "PKG=old", "PKG=new", "f.sh") == "a PKG=new b"


@pytest.mark.unit
def test_replace_once_zero_matches_raises():
    with pytest.raises(DeriveError) as e:
        replace_once("nothing here", "PKG=old", "PKG=new", "script.sh")
    assert "script.sh" in str(e.value) and "'PKG=old'" in str(e.value) and "found 0" in str(e.value)


@pytest.mark.unit
def test_replace_once_two_matches_raises():
    with pytest.raises(DeriveError) as e:
        replace_once("x x", "x", "y", "handbook.md")
    assert "handbook.md" in str(e.value) and "found 2" in str(e.value)


# --------------------------------------------------------------------------- replace_all
@pytest.mark.unit
def test_replace_all_at_least_met():
    assert replace_all("a.zip b.zip c.zip", ".zip", ".tar", "h.md", at_least=3) == "a.tar b.tar c.tar"
    assert replace_all("one", "one", "two", "h.md") == "two"


@pytest.mark.unit
def test_replace_all_at_least_not_met_raises():
    with pytest.raises(DeriveError) as e:
        replace_all("a.zip b.zip", ".zip", ".tar", "h.md", at_least=3)
    assert "h.md" in str(e.value) and "at least 3" in str(e.value) and "found 2" in str(e.value)
    with pytest.raises(DeriveError) as e:
        replace_all("none", "absent", "x", "h.md")
    assert "found 0" in str(e.value)


# --------------------------------------------------------------------------- cut_block
@pytest.mark.unit
def test_cut_block_removes_start_through_end():
    assert cut_block("keep [S]gone[E] tail", "[S]", "[E]", "f") == "keep  tail"


@pytest.mark.unit
def test_cut_block_keep_end():
    assert cut_block("keep [S]gone[E] tail", "[S]", "[E]", "f", keep_end=True) == "keep [E] tail"


@pytest.mark.unit
def test_cut_block_missing_marker_raises():
    with pytest.raises(DeriveError) as e:
        cut_block("keep gone[E]", "[S]", "[E]", "profiles.yaml")
    assert "profiles.yaml" in str(e.value) and "block start" in str(e.value) and "found 0" in str(e.value)
    with pytest.raises(DeriveError) as e:
        cut_block("keep [S]gone", "[S]", "[E]", "profiles.yaml")
    assert "block end" in str(e.value) and "found 0" in str(e.value)
    with pytest.raises(DeriveError) as e:                              # two of a marker is as bad as none
        cut_block("[S]a[E]b[E]", "[S]", "[E]", "profiles.yaml")
    assert "block end" in str(e.value) and "found 2" in str(e.value)


@pytest.mark.unit
def test_cut_block_end_before_start_raises():
    with pytest.raises(DeriveError) as e:
        cut_block("[E] then [S]", "[S]", "[E]", "f")
    assert "comes before" in str(e.value)


# --------------------------------------------------------------------------- Derivation.write
@pytest.mark.unit
def test_write_creates_parent_dirs_and_both_content_kinds(tmp_path):
    d = Derivation({tmp_path / "a" / "b" / "t.md": "text\n", tmp_path / "a" / "raw.bin": b"\x00\x01"})
    d.write()
    assert (tmp_path / "a" / "b" / "t.md").read_text(encoding="utf-8") == "text\n"
    assert (tmp_path / "a" / "raw.bin").read_bytes() == b"\x00\x01"
    assert sorted(d.paths) == sorted([tmp_path / "a" / "b" / "t.md", tmp_path / "a" / "raw.bin"])


@pytest.mark.unit
def test_write_preserves_existing_executable_bit(tmp_path):
    exe, plain = tmp_path / "run.sh", tmp_path / "note.md"
    exe.write_text("#!/bin/sh\nold\n"); exe.chmod(0o755)
    plain.write_text("old\n"); plain.chmod(0o644)
    Derivation().emit(exe, "#!/bin/sh\nnew\n").emit(plain, "new\n").write()
    assert exe.read_text() == "#!/bin/sh\nnew\n" and os.access(exe, os.X_OK)
    assert plain.read_text() == "new\n" and not os.access(plain, os.X_OK)


@pytest.mark.unit
def test_write_sets_executable_bit_from_mode(tmp_path):
    exe, plain = tmp_path / "new" / "run.sh", tmp_path / "new" / "note.md"
    Derivation().emit(exe, "#!/bin/sh\n", mode=0o755).emit(plain, "md\n").write()
    assert os.access(exe, os.X_OK) and (exe.stat().st_mode & 0o777) == 0o755
    assert not os.access(plain, os.X_OK)


# --------------------------------------------------------------------------- Derivation.check
@pytest.mark.unit
def test_check_reports_nothing_when_tree_matches(tmp_path):
    d = Derivation({tmp_path / "x" / "a.txt": "A\n", tmp_path / "b.bin": b"B"})
    d.write()
    assert d.check() == []
    d.check_or_die()                                                   # no raise


@pytest.mark.unit
def test_check_reports_stale_and_missing_paths_without_writing(tmp_path):
    stale, missing, fresh = tmp_path / "stale.txt", tmp_path / "deep" / "missing.txt", tmp_path / "fresh.txt"
    stale.write_text("on disk\n"); fresh.write_text("same\n")
    d = Derivation({stale: "derived\n", missing: "m\n", fresh: "same\n"})
    assert d.check() == [stale, missing]
    assert stale.read_text() == "on disk\n" and not missing.exists()   # check() writes nothing
    with pytest.raises(DeriveError) as e:
        d.check_or_die()
    assert str(stale) in str(e.value) and str(missing) in str(e.value) and str(fresh) not in str(e.value)


@pytest.mark.unit
def test_module_is_stdlib_only_and_carries_no_em_dash():
    src = pathlib.Path(derive.__file__).read_text(encoding="utf-8")
    assert chr(0x2014) not in src
    imports = [ln.split()[1] for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    assert set(imports) <= {"__future__", "pathlib", "stat"}, imports
