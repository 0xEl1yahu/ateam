import pytest

from ateam.proposal import ProposalError, ProposalHeader, parse_header, proposal_relpath, render_proposal

H = ProposalHeader(plan_id="P-0003", branch="ateam/P-0003", tip="a" * 40, target_branch="main", target_tip="b" * 40)


def _render(**kw):
    args = dict(diff_stat=" src/x.py | 2 +-\n", contracts=[{"id": "C-0002", "title": "t", "merge": "c" * 40,
                "checks": [{"name": "unit", "passed": True}], "warnings": ["verifier: minimal"]}],
                records=["R-0001 approved (bound by C-0002)"], risks=["A3 (judgment) not checked"],
                telemetry=[{"contract_id": "C-0002", "role": "build", "outcome": "completed", "cost_usd": 0.5, "turns": 7}])
    args.update(kw)
    return render_proposal(H, **args)


def test_round_trip():
    text = _render()
    assert parse_header(text) == H
    for heading in ("## Diff summary", "## Evidence per contract", "## Records", "## Unresolved risks", "## Telemetry"):
        assert heading in text
    assert proposal_relpath("P-0003") == "reports/P-0003-merge.md"


@pytest.mark.parametrize("text", ["no header here\n", "---\nplan_id: P-0003\n---\n", "---\n: [\n---\n"])
def test_parse_rejects_missing_or_bad_header(text):
    with pytest.raises(ProposalError):
        parse_header(text)


def test_render_lists_risks_and_empty_sections_say_none():
    text = _render(records=[], telemetry=[])
    assert "- A3 (judgment) not checked" in text
    assert text.count("none") >= 2


def test_total_counts_unknown_cost_at_cap():
    from ateam.tiers import load_tiers
    telemetry = [{"contract_id": "C-0002", "role": "build", "tier": "standard", "outcome": "completed", "cost_usd": 0.5, "turns": 7},
                 {"contract_id": "C-0002", "role": "test", "tier": "light", "outcome": "interrupted", "cost_usd": None, "turns": None}]
    text = _render(telemetry=telemetry, tiers=load_tiers())
    assert "| C-0002 | build | completed | 0.50 | 7 |" in text
    assert "| C-0002 | test | interrupted | unknown (cap 1.00) | 0 |" in text
    assert "Total: 1.50 USD over 2 attempt(s), 1 counted at its tier cap (cost unknown)" in text
    assert parse_header(text) == H and text.split("\n---\n")[0] == _render(telemetry=[]).split("\n---\n")[0]
    legacy = _render(telemetry=telemetry)     # without tiers, the output is what it was
    assert "| C-0002 | test | interrupted | 0.00 | 0 |" in legacy and "Total: 0.50 USD over 2 attempt(s)\n" in legacy
