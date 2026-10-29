from ateam.config import NamedCheck, TargetConfig, load_config, save_config
from ateam.paths import TargetPaths


def test_defaults_when_missing(target_repo):
    cfg = load_config(TargetPaths.from_root(target_repo))
    assert cfg.target_branch == "main"
    assert cfg.max_workers == 8   # a guard, not a limit (owner, 2026-10-28)
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


def test_brief_docs_default_empty_and_roundtrip(target_repo):
    p = TargetPaths.from_root(target_repo)
    assert TargetConfig().brief_docs == []
    save_config(p, TargetConfig(brief_docs=["docs/design.md"]))
    assert load_config(p).brief_docs == ["docs/design.md"]


def test_log_retention_days_defaults_to_14_and_loads(target_repo):
    import pytest
    from pydantic import ValidationError

    p = TargetPaths.from_root(target_repo)
    assert load_config(p).log_retention_days == 14
    p.intent.mkdir(parents=True, exist_ok=True)
    p.config_file.write_text("log_retention_days: 3\n")
    assert load_config(p).log_retention_days == 3
    p.config_file.write_text("log_retention_days: -1\n")
    with pytest.raises(ValidationError):
        load_config(p)
