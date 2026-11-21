"""Fact store: dedup, supersession, archive/merge, forget tombstones, FTS."""

import pytest

from aurora.memory.store import FactStore, ForgottenFact
from aurora.util import new_id


@pytest.fixture
def store(make_app):
    app, _ = make_app()
    return app.store


def test_add_and_get(store):
    fact = store.add_fact("Alex's favorite editor is Vim", category="preference")
    got = store.get_fact(fact.id)
    assert got.content == fact.content
    assert got.active and got.id.startswith("f_")


def test_exact_duplicate_refused(store):
    store.add_fact("Alex works at the botanical garden")
    with pytest.raises(Exception):
        store.add_fact("Alex works at the botanical garden")


def test_jaccard_duplicate_refused(store):
    store.add_fact("Sarah has a dog named Biscuit who loves the park")
    dup = store.find_duplicate("Sarah has a dog named Biscuit who loves the park a lot")
    assert dup is not None  # heavily overlapping text -> duplicate detected


def test_similar_but_distinct_fact_allowed(store):
    store.add_fact("Sarah has a dog named Biscuit who loves the park")
    fact2 = store.add_fact("Sarah has a dog named Biscuit who hates the vet")
    assert fact2.active  # different substance -> not a duplicate


def test_different_fact_allowed(store):
    store.add_fact("Sarah has a dog named Biscuit")
    fact2 = store.add_fact("Sarah's cat is called Miso")
    assert fact2.active


def test_supersede_chain(store):
    old = store.add_fact("Alex drives a red car")
    new = store.supersede_fact(old.id, "Alex drives a blue car")
    old_after = store.get_fact(old.id)
    assert old_after.superseded_by == new.id
    assert not old_after.active
    assert new.active


def test_archive_and_restore(store):
    fact = store.add_fact("Something obsolete about printers")
    store.archive_fact(fact.id, "obsolete")
    assert not store.get_fact(fact.id).active
    store.restore_fact(fact.id)
    assert store.get_fact(fact.id).active


def test_merge_soft_deletes(store):
    a = store.add_fact("Alex is learning Japanese")
    b = store.add_fact("Alex is studying Japanese")
    merged = store.merge_fact(b.id, a.id)
    dup_after = store.get_fact(b.id)
    assert dup_after.archived and dup_after.merged_into == a.id
    assert merged.active
    assert not store.get_fact(a.id).archived


def test_pinned_protected(store):
    fact = store.add_fact("Important pinned fact", pinned=True)
    with pytest.raises(ValueError):
        store.archive_fact(fact.id, "low_value")
    store.set_pinned(fact.id, False)
    store.archive_fact(fact.id, "low_value")


def test_forget_tombstone_blocks_readd(store):
    store.add_fact("Alex had spaghetti for dinner on Friday")
    store.forget_text("spaghetti for dinner")
    with pytest.raises(ForgottenFact):
        store.add_fact("Alex had spaghetti for dinner on Saturday")


def test_unrelated_fact_not_blocked_by_tombstone(store):
    store.forget_text("spaghetti for dinner")
    fact = store.add_fact("Alex ordered pizza on Saturday")
    assert fact.active


def test_fts_search(store):
    store.add_fact("The wifi password is rosebud42", category="fact")
    store.add_fact("Alex dislikes meetings before 10am")
    hits = store.facts_fts("wifi password")
    assert any("rosebud42" in f.content for f in hits)
    hits2 = store.facts_fts("meetings")
    assert any("meetings" in f.content for f in hits2)


def test_versions_on_update(store):
    fact = store.add_fact("Project deadline is March 1")
    store.update_fact(fact.id, "Project deadline moved to April 1")
    got = store.get_fact(fact.id)
    assert got.content == "Project deadline moved to April 1"
    assert len(got.versions) == 1
    assert got.versions[0]["content"] == "Project deadline is March 1"


def test_evidence_attached(store):
    fact = store.add_fact("E Fact", evidence=[{"message_id": 1, "ts": 123, "quote": "E Fact"}])
    store.attach_evidence(fact.id, [{"message_id": 2, "ts": 456, "quote": "more"}])
    assert len(store.get_fact(fact.id).evidence) == 2


def test_export(store):
    store.add_fact("Exportable fact one")
    data = store.export()
    assert data["facts"] and data["facts"][0]["content"] == "Exportable fact one"
    assert "tombstones" in data


def test_event_expiration(store):
    fact = store.add_fact("Expires soon", kind="temporary", expires_at=1000.0)
    assert not fact.active  # expiry already past on the frozen clock
