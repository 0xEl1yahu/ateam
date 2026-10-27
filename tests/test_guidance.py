from datetime import UTC, datetime

from ateam.guidance import GuidanceEntry, append_guidance, live_guidance, load_guidance


def test_append_load_and_live(tmp_path):
    e1 = GuidanceEntry(at=datetime(2026, 10, 27, tzinfo=UTC), source="owner", contract_hash="h1", text="use the helper in x.py")
    e2 = GuidanceEntry(at=datetime(2026, 10, 28, tzinfo=UTC), source="A-42", contract_hash="h2", text="later")
    append_guidance(tmp_path, "C-0002", e1)
    p = append_guidance(tmp_path, "C-0002", e2)
    assert p == tmp_path / "C-0002.yaml" and load_guidance(tmp_path, "C-0002") == [e1, e2]
    assert live_guidance(load_guidance(tmp_path, "C-0002"), "h2") == [e2]
    assert load_guidance(tmp_path, "C-0009") == []


def test_guidance_file_names_its_contract(tmp_path):
    import yaml

    append_guidance(tmp_path, "C-0003", GuidanceEntry(at=datetime(2026, 10, 27, tzinfo=UTC), source="owner", contract_hash="h", text="t"))
    data = yaml.safe_load((tmp_path / "C-0003.yaml").read_text())
    assert data["contract"] == "C-0003" and data["entries"][0]["text"] == "t"


def test_guidance_dir_is_in_the_intent_repo(tmp_path):
    from ateam.paths import TargetPaths

    p = TargetPaths.from_root(tmp_path)
    assert p.guidance == p.intent / "guidance"
