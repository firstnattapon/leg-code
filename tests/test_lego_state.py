"""Transaction + idempotency + stale-anchor tests for the Step 18 persistence."""

from __future__ import annotations

import pytest

from lego_one_row import (
    CurrentSnapshot,
    PreviousAnchor,
    RunContext,
    StrategyParameters,
    anchor_from_state,
    compute_chain_key,
    compute_row,
    compute_run_id,
)
from lego_state import (
    InMemoryStateStore,
    StaleAnchorError,
    finalize_row,
    read_anchor_state,
)


def params() -> StrategyParameters:
    return StrategyParameters(fix_c=1500.0, diff=0.0, dna_code="bypass:100")


def snapshot(price: float, captured_at: str, holdings: float = 3.0) -> CurrentSnapshot:
    return CurrentSnapshot(
        environment="Test (UAT)",
        account_fingerprint="acct1234abcd",
        symbol="AAPL",
        price=price,
        holdings=holdings,
        captured_at=captured_at,
    )


def make_ctx(snap: CurrentSnapshot, anchor: PreviousAnchor) -> RunContext:
    p = params()
    chain_key = compute_chain_key(
        snap.environment, snap.account_fingerprint, snap.symbol, p
    )
    run_id = compute_run_id(chain_key, anchor, snap)
    return RunContext(run_id=run_id, chain_key=chain_key, snapshot=snap, anchor=anchor, params=p)


def commit(store: InMemoryStateStore, snap: CurrentSnapshot, anchor: PreviousAnchor):
    ctx = make_ctx(snap, anchor)
    result = finalize_row(store, ctx, compute_row(ctx))
    return ctx, result


# --------------------------------------------------------------------------- #
# One successful run appends exactly one document
# --------------------------------------------------------------------------- #
def test_successful_run_appends_exactly_one_row():
    store = InMemoryStateStore()
    before = len(store.rows)
    ctx, result = commit(store, snapshot(100.0, "t0"), PreviousAnchor.genesis())
    after = len(store.rows)
    assert after - before == 1
    assert result.created is True
    assert result.version == 1
    state = read_anchor_state(store, ctx.chain_key)
    assert state["version"] == 1
    assert state["latest_row_id"] == ctx.run_id


def test_duplicate_click_same_run_id_is_idempotent():
    store = InMemoryStateStore()
    snap = snapshot(100.0, "t0")
    ctx, first = commit(store, snap, PreviousAnchor.genesis())
    # Pressing Step 18 again with the same captured snapshot + anchor.
    _, second = commit(store, snap, PreviousAnchor.genesis())
    assert first.created is True
    assert second.created is False
    assert second.idempotent is True
    assert len(store.rows) == 1
    assert read_anchor_state(store, ctx.chain_key)["version"] == 1


# --------------------------------------------------------------------------- #
# A chain of anchored rows advances the recurrence
# --------------------------------------------------------------------------- #
def test_chain_advances_through_anchored_rows():
    store = InMemoryStateStore()
    ctx0, r0 = commit(store, snapshot(100.0, "t0"), PreviousAnchor.genesis())

    anchor1 = anchor_from_state(read_anchor_state(store, ctx0.chain_key))
    ctx1, r1 = commit(store, snapshot(110.0, "t1"), anchor1)
    assert r1.version == 2
    assert len(store.rows) == 2

    anchor2 = anchor_from_state(read_anchor_state(store, ctx1.chain_key))
    ctx2, r2 = commit(store, snapshot(121.0, "t2"), anchor2)
    assert r2.version == 3
    assert len(store.rows) == 3
    # DNA step advanced 0 -> 1 -> 2
    assert store.rows[ctx2.run_id]["columns_full_precision"]["DNA step"] == 2


# --------------------------------------------------------------------------- #
# Stale anchor is rejected fail-closed
# --------------------------------------------------------------------------- #
def test_genesis_anchor_rejected_when_chain_exists():
    store = InMemoryStateStore()
    commit(store, snapshot(100.0, "t0"), PreviousAnchor.genesis())
    # A second run that still thinks it is genesis (never read the new state).
    with pytest.raises(StaleAnchorError):
        commit(store, snapshot(105.0, "t1"), PreviousAnchor.genesis())
    assert len(store.rows) == 1


def test_version_mismatch_anchor_rejected():
    store = InMemoryStateStore()
    ctx0, _ = commit(store, snapshot(100.0, "t0"), PreviousAnchor.genesis())
    anchor1 = anchor_from_state(read_anchor_state(store, ctx0.chain_key))
    commit(store, snapshot(110.0, "t1"), anchor1)
    # A run holding the now-stale version-0 anchor (but exists=True).
    stale = PreviousAnchor(
        exists=True, version=0, row_id="old", dna_step=0, p0=100.0, prev_price=100.0, prev_actual=0.0
    )
    with pytest.raises(StaleAnchorError):
        commit(store, snapshot(115.0, "t2"), stale)
    assert len(store.rows) == 2


def test_concurrent_writer_between_read_and_write_is_rejected():
    store = InMemoryStateStore()
    ctx0, _ = commit(store, snapshot(100.0, "t0"), PreviousAnchor.genesis())
    anchor1 = anchor_from_state(read_anchor_state(store, ctx0.chain_key))

    # Two runs read the same anchor (version 1). The first to write wins; the
    # second must fail closed even though it planned a create.
    def advance_chain(inner_store, _ctx):
        competitor_snapshot = snapshot(111.0, "t1-competitor")
        competitor_ctx = make_ctx(competitor_snapshot, anchor1)
        # Direct write to simulate the other session committing first.
        inner_store.on_before_write = None
        finalize_row(inner_store, competitor_ctx, compute_row(competitor_ctx))

    store.on_before_write = advance_chain
    with pytest.raises(StaleAnchorError):
        commit(store, snapshot(112.0, "t1-us"), anchor1)
    # Only genesis + competitor committed; our stale write was rejected.
    assert len(store.rows) == 2


# --------------------------------------------------------------------------- #
# Order audit
# --------------------------------------------------------------------------- #
def test_order_audit_is_recorded():
    store = InMemoryStateStore()
    store.record_order_audit({"event_id": "e1", "action": "PREVIEW"})
    assert store.order_audit["e1"]["action"] == "PREVIEW"
