"""A worker cannot start without a frozen, unchanged, runnable contract."""

import pytest

from ateam.contracts.select import freeze
from ateam.records.lifecycle import approve
from ateam.runner.binding import ContractNotBinding, require_binding
from ateam.state.db import StateDB


def _db(tmp_path):
    db = StateDB(tmp_path / "s.db"); db.init_schema(); return db


def _frozen(sample_contract, records_three):
    rec = records_three["R-0001"]
    return freeze(sample_contract, [rec]), {"R-0001": rec}


def test_requires_freeze_in_state(tmp_path, sample_contract, records_three):
    db = _db(tmp_path)
    frozen, recs = _frozen(sample_contract, records_three)
    with pytest.raises(ContractNotBinding, match="not frozen"):
        require_binding(frozen, recs, db)
    db.upsert_contract(frozen.id, frozen.plan, frozen.status, frozen.content_hash())
    require_binding(frozen, recs, db)  # ok


def test_rejects_draft_and_hand_edit(tmp_path, sample_contract, records_three):
    db = _db(tmp_path)
    frozen, recs = _frozen(sample_contract, records_three)
    db.upsert_contract(frozen.id, frozen.plan, frozen.status, frozen.content_hash())
    with pytest.raises(ContractNotBinding, match="draft"):
        require_binding(frozen.model_copy(update={"status": "draft"}), recs, db)
    with pytest.raises(ContractNotBinding, match="hand edit"):
        require_binding(frozen.model_copy(update={"title": "edited"}), recs, db)


def test_rejects_drifted_or_unapproved_records(tmp_path, sample_contract, records_three):
    db = _db(tmp_path)
    frozen, recs = _frozen(sample_contract, records_three)
    db.upsert_contract(frozen.id, frozen.plan, frozen.status, frozen.content_hash())
    with pytest.raises(ContractNotBinding, match="drift.*R-0001"):
        require_binding(frozen, {"R-0001": recs["R-0001"].model_copy(update={"statement": "changed"})}, db)
    with pytest.raises(ContractNotBinding, match="R-0001"):
        require_binding(frozen, {}, db)


def test_rejects_non_runnable_status(tmp_path, sample_contract, records_three):
    db = _db(tmp_path)
    frozen, recs = _frozen(sample_contract, records_three)
    db.upsert_contract(frozen.id, frozen.plan, "integrated", frozen.content_hash())
    with pytest.raises(ContractNotBinding, match="integrated"):
        require_binding(frozen.model_copy(update={"status": "integrated"}), recs, db)
