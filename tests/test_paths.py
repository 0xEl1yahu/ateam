import subprocess
from pathlib import Path

from ateam.paths import TargetPaths, home_dir, normalise_remote, origin_url, repo_id, repo_id_source


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout


def test_normalise_remote_variants():
    for url in ("git@github.com:a/b.git", "https://github.com/a/b", "ssh://git@github.com/a/b.git", "https://user:tok@GitHub.com/a/b/", "git@github.com:a/b"):
        assert normalise_remote(url) == "github.com/a/b", url


def test_repo_id_same_for_two_clones_same_origin(tmp_path):
    bare = tmp_path / "origin.git"; _git(tmp_path, "init", "-q", "--bare", str(bare))
    seed = tmp_path / "seed"; _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    _git(seed, "config", "user.email", "t@e"); _git(seed, "config", "user.name", "t")
    (seed / "f").write_text("x"); _git(seed, "add", "f"); _git(seed, "commit", "-q", "-m", "i")
    _git(seed, "remote", "add", "origin", str(bare)); _git(seed, "push", "-q", "origin", "main")
    a = tmp_path / "clone-a"; b = tmp_path / "deeper" / "clone-b"; b.parent.mkdir()
    _git(tmp_path, "clone", "-q", str(bare), str(a)); _git(tmp_path, "clone", "-q", str(bare), str(b))
    assert origin_url(a) == str(bare) and repo_id(a) == repo_id(b) and len(repo_id(a)) == 12
    assert repo_id_source(a) == "origin"


def test_repo_id_differs_for_different_origins(target_repo, tmp_path):
    other = tmp_path / "other"; _git(tmp_path, "init", "-q", "-b", "main", str(other))
    _git(target_repo, "remote", "add", "origin", "git@github.com:a/b.git")
    _git(other, "remote", "add", "origin", "git@github.com:a/c.git")
    assert repo_id(target_repo) != repo_id(other)


def test_repo_id_path_fallback_without_remote(tmp_path):
    assert origin_url(tmp_path) is None
    assert repo_id(tmp_path) == repo_id(tmp_path) and len(repo_id(tmp_path)) == 12
    assert repo_id(tmp_path) != repo_id(tmp_path / "other") and repo_id_source(tmp_path) == "path"


def test_from_root_points_at_intent(tmp_path, monkeypatch):
    monkeypatch.setenv("ATEAM_HOME", str(tmp_path / "h"))
    p = TargetPaths.from_root(tmp_path)
    assert p.root == tmp_path
    assert p.intent == home_dir(tmp_path) / "intent"
    assert p.records == p.intent / "records" and p.contracts == p.intent / "contracts"
    assert p.plans == p.intent / "plans" and p.reports == p.intent / "reports"
    assert p.config_file == p.intent / "config.yaml"
    assert p.scratch_root == home_dir(tmp_path) / "scratch"
    assert not hasattr(p, "ateam")


def test_exists_requires_git_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ATEAM_HOME", str(tmp_path / "h"))
    p = TargetPaths.from_root(tmp_path)
    assert p.exists() is False
    p.intent.mkdir(parents=True)
    assert p.exists() is False
    (p.intent / ".git").mkdir()
    assert p.exists() is True


def test_home_dir_honours_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ATEAM_HOME", str(tmp_path / "h"))
    h = home_dir(tmp_path)
    assert h.parent == tmp_path / "h" and h.name == repo_id(tmp_path) and h.is_dir()


def test_logs_dir_is_under_home_not_target_or_intent(target_repo):
    p = TargetPaths.from_root(target_repo)
    assert p.logs == p.scratch_root.parent / "logs" == home_dir(target_repo) / "logs"
    assert p.logs not in (p.intent, p.root) and p.intent not in p.logs.parents and p.root not in p.logs.parents
