"""The merge proposal: reports/<plan-id>-merge.md in the intent repo, a YAML header plus the owner's evidence.

`ateam merge` trusts only the header of the committed file; the sections are for the owner to read."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

import yaml

from ateam.roles import Role
from ateam.tiers import TierSpec, attempt_cost, spend_total


class ProposalError(ValueError):
    pass


@dataclass(frozen=True)
class ProposalHeader:
    plan_id: str
    branch: str
    tip: str
    target_branch: str
    target_tip: str


def proposal_relpath(plan_id: str) -> str:
    return f"reports/{plan_id}-merge.md"


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines) if lines else "none"


def _telemetry(telemetry: list[dict], tiers: dict[str, dict[Role, TierSpec]] | None) -> str:
    """The attempts table. With tiers, an unknown (NULL) cost shows as unknown with its cap and the Total counts it at
    that cap; without, an unknown cost counts as 0, as before."""
    if not telemetry:
        return "none"

    def cost(t: dict) -> str:
        if tiers is None or t.get("cost_usd") is not None:
            return f"{t.get('cost_usd') or 0:.2f}"
        return f"unknown (cap {attempt_cost(t, tiers)[0]:.2f})"

    rows = [f"| {t['contract_id']} | {t['role']} | {t.get('outcome') or 'unfinished'} | {cost(t)} | {t.get('turns') or 0} |"
            for t in telemetry]
    if tiers is None:
        total = f"Total: {sum(t.get('cost_usd') or 0 for t in telemetry):.2f} USD over {len(telemetry)} attempt(s)"
    else:
        usd, capped = spend_total(telemetry, tiers)
        total = (f"Total: {usd:.2f} USD over {len(telemetry)} attempt(s), "
                 f"{capped} counted at its tier cap (cost unknown)")
    return "| contract | role | outcome | cost USD | turns |\n|---|---|---|---|---|\n" + "\n".join(rows) + f"\n\n{total}"


def render_proposal(
    header: ProposalHeader, *, diff_stat: str, contracts: list[dict], records: list[str], risks: list[str],
    telemetry: list[dict], final_checks: list[dict] = (), tiers: dict[str, dict[Role, TierSpec]] | None = None,
) -> str:
    evidence = []
    for c in contracts:
        checks = ", ".join(f"{k['name']} {'passed' if k.get('passed') else 'FAILED'}" for k in c.get("checks", [])) or "no checks recorded"
        evidence.append(f"### {c['id']}: {c['title']}\n\n- merged as {c['merge']}\n- verify checks: {checks}"
                        + "".join(f"\n- warning: {w}" for w in c.get("warnings", [])))
    table = _telemetry(telemetry, tiers)
    return (
        "---\n" + yaml.safe_dump(asdict(header), sort_keys=False) + "---\n\n"
        f"# Merge proposal {header.plan_id}\n\n"
        f"`ateam merge {header.plan_id}` fast-forwards {header.target_branch} to {header.branch} at {header.tip}.\n\n"
        f"## Diff summary\n\n```\n{diff_stat.rstrip() or 'no changes'}\n```\n\n"
        f"## Final checks on the integration tip\n\n"
        + _bullets([f"{k['name']}: {'passed' if k.get('passed') else 'FAILED'}" for k in final_checks]) + "\n\n"
        f"## Evidence per contract\n\n" + ("\n\n".join(evidence) or "none") + "\n\n"
        f"## Records\n\n{_bullets(records)}\n\n"
        f"## Unresolved risks\n\n{_bullets(risks)}\n\n"
        f"## Telemetry\n\n{table}\n"
    )


def parse_header(text: str) -> ProposalHeader:
    if not text.startswith("---\n") or "\n---\n" not in text[3:]:
        raise ProposalError("the merge proposal has no header")
    raw = text[4:text.index("\n---\n", 3) + 1]
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ProposalError(f"the merge proposal header is not YAML: {str(e).splitlines()[0]}") from None
    names = [f.name for f in fields(ProposalHeader)]
    if not isinstance(data, dict) or any(not isinstance(data.get(n), str) or not data.get(n) for n in names):
        raise ProposalError(f"the merge proposal header must name {', '.join(names)}")
    return ProposalHeader(**{n: data[n] for n in names})
