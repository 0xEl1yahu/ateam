from ateam.config import NamedCheck, TargetConfig, load_config, save_config
from ateam.paths import TargetPaths


def test_defaults_when_missing(target_repo):
    cfg = load_config(TargetPaths.from_root(target_repo))
    assert cfg.target_branch == "main"
    assert cfg.max_workers == 2
    assert cfg.protected_paths == [".claude/**"]
    assert cfg.checks == [] and cfg.setup == [] and cfg.env == {} and cfg.tiers == {}
    assert cfg.serialize_checks is False
    assert cfg.global_record_cap == 5


def test_roundtrip(target_repo):
    p = TargetPaths.from_root(target_repo)
    save_config(
        p,
        TargetConfig(
            target_branch="develop",
            checks=[NamedCheck(name="unit", cmd="pytest")],
            serialize_checks=True,
            env={"X": "1"},
        ),
    )
    cfg = load_config(p)
    assert cfg.target_branch == "develop"
    assert cfg.checks[0].cmd == "pytest"
    assert cfg.serialize_checks is True
    assert cfg.env == {"X": "1"}
