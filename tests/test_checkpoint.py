from ateam.checkpoint import Checkpoint, collect, load_handoff
from ateam.gitops import GitRepo


def test_load_handoff_variants(tmp_path):
    (tmp_path / ".ateam-scratch").mkdir()
    notes, warnings = load_handoff(tmp_path)
    assert notes is None and warnings
    f = tmp_path / ".ateam-scratch/handoff.yaml"
    f.write_text(": not yaml [")
    assert load_handoff(tmp_path)[0] is None and load_handoff(tmp_path)[1]
    f.write_text("- a list\n")
    assert load_handoff(tmp_path)[0] is None
    f.write_text("")
    assert load_handoff(tmp_path)[0] is None
    f.write_text("progress: half\nnext: tests\n")
    notes, warnings = load_handoff(tmp_path)
    assert notes == {"progress": "half", "next": "tests"} and warnings == []


def test_collect_facts(target_repo, sample_contract, records_three):
    g = GitRepo(target_repo)
    base = g.head()
    (target_repo / "src/api/x.py").write_text("ROUTES = ['books']\n")
    g.run("add", "src/api/x.py"); g.run("commit", "-q", "-m", "add route")
    (target_repo / "notes.txt").write_text("x\n")
    cp = collect(g, target_repo, base, sample_contract, records_three, session_id="s1", checks=[{"name": "unit", "passed": True}])
    assert isinstance(cp, Checkpoint)
    assert cp.base == base and cp.head == g.head() and cp.commits and "add route" in cp.commits[0]
    assert cp.diff_names == ["src/api/x.py"] and ("??", "notes.txt") in cp.dirty
    assert cp.contract_hash == sample_contract.content_hash() and cp.record_hashes["R-0001"] == records_three["R-0001"].content_hash()
    assert cp.session_id == "s1" and cp.checks[0]["name"] == "unit" and cp.notes is None and cp.warnings
    assert cp.to_dict()["head"] == cp.head
