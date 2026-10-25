from ateam.records.graph import RecordGraph


def test_binding_and_globals(records_three):
    g = RecordGraph(records_three)
    assert [r.id for r in g.binding("src/api/x.py")] == ["R-0001"]
    assert [r.id for r in g.binding("src/api/x.py", approved_only=False)] == ["R-0001", "R-0003"]
    assert [r.id for r in g.globals()] == ["R-0002"]
    assert g.binding("docs/readme.md") == []


def test_chain_cycles_dangling(records_three):
    records_three["R-0001"].supersedes = ["R-0002"]
    records_three["R-0002"].supersedes = ["R-0001", "R-0099"]
    g = RecordGraph(records_three)
    assert set(g.chain("R-0001")) == {"R-0002", "R-0001"}
    assert g.cycles()
    assert g.dangling() == [("R-0002", "R-0099")]
    assert g.superseders("R-0002") == ["R-0001"]


def test_no_cycles_in_clean_set(records_three):
    assert RecordGraph(records_three).cycles() == [] and RecordGraph(records_three).dangling() == []
