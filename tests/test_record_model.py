from datetime import UTC, datetime

from ateam.records.model import Evidence, HistoryEntry, Status, compute_fingerprint


def test_fingerprint_is_order_independent():
    a = [Evidence(type="doc", ref="d.md"), Evidence(type="test", ref="t.py::x")]
    assert compute_fingerprint(a) == compute_fingerprint(list(reversed(a)))
    assert compute_fingerprint(a) != compute_fingerprint(a[:1])


def test_content_hash_ignores_history(sample_record):
    h = sample_record.content_hash()
    sample_record.history.append(HistoryEntry(at=datetime.now(UTC), by="owner", to=Status.approved))
    assert sample_record.content_hash() == h
    assert sample_record.model_copy(update={"statement": "x"}).content_hash() != h


def test_slug_and_global(sample_record):
    assert sample_record.slug() == "reading-state-is-only-ever-set"
    assert sample_record.is_global() is False
