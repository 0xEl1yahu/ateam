from ateam.discovery import DiscoveryResult, candidates_to_records, render_discovery_report
from ateam.records.lifecycle import reject
from ateam.records.model import Basis, Evidence, Origin, Status, compute_fingerprint


def cand(**over):
    base = dict(kind="invariant", statement="Routes never read environment variables directly", rationale="testability",
                origin="documented", basis="requirement",
                evidence=[{"type": "doc", "ref": "README.md#rules"}], paths=["src/api/**"], serves=["testable handlers"], confidence="high")
    base.update(over)
    return base


def test_candidates_dead_glob_dropped_and_reported(target_repo, target_paths):
    r = candidates_to_records([cand(paths=["src/nothing/**"])], [], {}, target_repo, target_paths.records, cap_global=5)
    assert r.proposed == [] and len(r.dropped) == 1 and "dead glob" in r.dropped[0][1]
    assert "dead glob" in render_discovery_report(r)


def test_rejected_fingerprint_not_reproposed(target_repo, target_paths, sample_record):
    ev = [Evidence(type="doc", ref="README.md#rules")]
    rejected = reject(sample_record.model_copy(update={"id": "R-0003", "evidence": ev, "fingerprint": compute_fingerprint(ev)}), "no")
    r = candidates_to_records([cand()], [], {"R-0003": rejected}, target_repo, target_paths.records, cap_global=5)
    assert r.proposed == [] and r.skipped_rejected == ["R-0003"]
    assert "previously rejected" in render_discovery_report(r)


def test_existing_fingerprint_skipped_silently(target_repo, target_paths, sample_record):
    ev = [Evidence(type="doc", ref="README.md#rules")]
    existing = sample_record.model_copy(update={"evidence": ev, "fingerprint": compute_fingerprint(ev), "status": Status.approved,
                                                "statement": "Routes never read environment variables directly"})
    r = candidates_to_records([cand()], [], {"R-0001": existing}, target_repo, target_paths.records, cap_global=5)
    assert r.proposed == [] and r.skipped_rejected == [] and r.dropped == []


def test_documented_without_doc_evidence_downgraded(target_repo, target_paths):
    r = candidates_to_records([cand(origin="documented", evidence=[{"type": "code", "ref": "src/api/x.py:1"}])], [], {}, target_repo, target_paths.records, cap_global=5)
    rec = r.proposed[0]
    assert rec.origin == Origin.inferred and "downgraded" in (rec.rationale or "")


def test_code_only_evidence_is_observation(target_repo, target_paths):
    r = candidates_to_records([cand(origin="inferred", basis="requirement", evidence=[{"type": "code", "ref": "src/api/x.py:1"}, {"type": "test", "ref": "tests/test_app.py::test_greet"}])], [], {}, target_repo, target_paths.records, cap_global=5)
    assert r.proposed[0].basis == Basis.observation


def test_global_cap_and_ids_continue(target_repo, target_paths, sample_record):
    existing = {"R-0004": sample_record.model_copy(update={"id": "R-0004"})}
    from ateam.records.store import save_record
    save_record(target_paths.records, existing["R-0004"])
    cands = [cand(statement=f"Global rule {i}", paths=[], evidence=[{"type": "doc", "ref": f"docs/d{i}.md"}]) for i in range(4)]
    r = candidates_to_records(cands, ["what about ports"], existing, target_repo, target_paths.records, cap_global=2)
    assert [p.id for p in r.proposed] == ["R-0005", "R-0006"] and r.capped == 2
    assert all(p.status == Status.proposed and p.history[-1].by == "ateam-init" for p in r.proposed)
    assert r.unknowns == ["what about ports"] and "global cap" in [d[1] for d in r.dropped][0]


def test_render_report_lists_next_command(target_repo, target_paths):
    r = candidates_to_records([cand()], ["which port"], {}, target_repo, target_paths.records, cap_global=5)
    text = render_discovery_report(r)
    assert "R-0001" in text and "ateam records review" in text and "which port" in text and "README.md#rules" in text



def test_bad_enum_empty_evidence_blank_statement_and_batch_duplicates_are_dropped_not_crashes(target_repo, target_paths):
    cands = [
        cand(origin="sort-of-documented"),
        cand(statement="   ", evidence=[{"type": "doc", "ref": "README.md#a"}]),
        cand(evidence=[]),
        cand(statement="one", evidence=[{"type": "doc", "ref": "README.md#dup"}]),
        cand(statement="ONE ", evidence=[{"type": "doc", "ref": "README.md#dup"}]),
    ]
    r = candidates_to_records(cands, [], {}, target_repo, target_paths.records, cap_global=5)
    assert [p.statement for p in r.proposed] == ["one"]
    reasons = [why for _, why in r.dropped]
    assert any("origin" in w or "invalid" in w for w in reasons) and any("statement" in w for w in reasons)
    assert any("evidence" in w for w in reasons) and any("duplicate" in w for w in reasons)


def test_pending_path_record_is_kept_when_a_contract_declares_it(target_repo, target_paths):
    c = cand(paths=["src/ateam/integrator.py"])
    r = candidates_to_records([c], [], {}, target_repo, target_paths.records, cap_global=3, declared_paths=["src/ateam/integrator.py"])
    assert len(r.proposed) == 1 and not r.dropped
    assert r.proposed[0].rationale.endswith("(pending: path declared by this plan)")
    assert any("src/ateam/integrator.py" in n and "pending" in n for n in r.notes)


def test_pending_path_glob_matching_a_declared_path_is_kept(target_repo, target_paths):
    c = cand(paths=["src/ateam/new/**"])
    r = candidates_to_records([c], [], {}, target_repo, target_paths.records, cap_global=3, declared_paths=["src/ateam/new/mod.py"])
    assert len(r.proposed) == 1 and not r.dropped


def test_pending_path_record_is_dropped_without_a_declaring_contract(target_repo, target_paths):
    c = cand(paths=["src/ateam/integrator.py"])
    r = candidates_to_records([c], [], {}, target_repo, target_paths.records, cap_global=3, declared_paths=["src/other.py"])
    assert r.proposed == [] and len(r.dropped) == 1 and "dead glob" in r.dropped[0][1]
    r = candidates_to_records([c], [], {}, target_repo, target_paths.records, cap_global=3)
    assert r.proposed == [] and len(r.dropped) == 1


def test_batch_duplicate_needs_same_evidence_and_statement(target_repo, target_paths):
    same_ev = [{"type": "doc", "ref": "README.md#shared"}]
    cands = [
        cand(statement="Routes validate input", evidence=same_ev),
        cand(statement="Routes authenticate callers", evidence=same_ev),
        cand(statement="  routes   VALIDATE input ", evidence=same_ev),
    ]
    r = candidates_to_records(cands, [], {}, target_repo, target_paths.records, cap_global=5)
    assert [p.statement for p in r.proposed] == ["Routes validate input", "Routes authenticate callers"]
    assert all(p.status == Status.proposed for p in r.proposed)
    assert len(r.dropped) == 1 and "same evidence and statement" in r.dropped[0][1]


def test_stored_match_needs_same_statement(target_repo, target_paths, sample_record):
    ev = [Evidence(type="doc", ref="README.md#rules")]
    stored = sample_record.model_copy(update={"evidence": ev, "fingerprint": compute_fingerprint(ev), "status": Status.approved,
                                              "statement": "Routes never read environment variables directly"})
    same = candidates_to_records([cand(statement="routes never  read environment variables directly")], [], {"R-0001": stored},
                                 target_repo, target_paths.records, cap_global=5)
    assert same.proposed == [] and same.dropped == []
    other = candidates_to_records([cand(statement="Routes return JSON only")], [], {"R-0001": stored},
                                  target_repo, target_paths.records, cap_global=5)
    assert [p.statement for p in other.proposed] == ["Routes return JSON only"]


def test_rejected_evidence_never_returns_reworded(target_repo, target_paths, sample_record):
    ev = [Evidence(type="doc", ref="README.md#rules")]
    rejected = reject(sample_record.model_copy(update={"id": "R-0003", "evidence": ev, "fingerprint": compute_fingerprint(ev),
                                                      "statement": "Routes never read environment variables directly"}), "no")
    r = candidates_to_records([cand(statement="Handlers must not touch env vars")], [], {"R-0003": rejected},
                              target_repo, target_paths.records, cap_global=5)
    assert r.proposed == [] and r.skipped_rejected == ["R-0003"]
