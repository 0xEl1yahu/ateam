from ateam.paths import TargetPaths, home_dir, repo_id


def test_from_root_layout(tmp_path):
    p = TargetPaths.from_root(tmp_path)
    assert p.records == tmp_path / ".ateam" / "records"
    assert p.contracts == tmp_path / ".ateam" / "contracts"
    assert p.config_file == tmp_path / ".ateam" / "config.yaml"
    assert p.exists() is False
    (tmp_path / ".ateam").mkdir()
    assert p.exists() is True


def test_repo_id_stable_and_short(tmp_path):
    assert repo_id(tmp_path) == repo_id(tmp_path)
    assert len(repo_id(tmp_path)) == 12
    assert repo_id(tmp_path) != repo_id(tmp_path / "other")


def test_home_dir_honours_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ATEAM_HOME", str(tmp_path / "h"))
    h = home_dir(tmp_path)
    assert h.parent == tmp_path / "h"
    assert h.name == repo_id(tmp_path)
    assert h.is_dir()
