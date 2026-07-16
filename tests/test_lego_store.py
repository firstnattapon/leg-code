from __future__ import annotations

import pandas as pd
import pytest

from lego_pipeline import PipelineContext, PreviousAnchor, build_snapshot_frame, final_dataframe, run_stage
from lego_store import (
    StaleAnchorError,
    _commit_final_row,
    build_final_document,
)


class Snapshot:
    def __init__(self, data=None):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class Ref:
    def __init__(self, data=None):
        self.data = data

    def get(self, transaction=None):
        return Snapshot(self.data)


class Transaction:
    def create(self, ref, data):
        if ref.data is not None:
            raise RuntimeError("already exists")
        ref.data = dict(data)

    def set(self, ref, data):
        ref.data = dict(data)


def completed():
    raw = build_snapshot_frame(
        snapshot_at="2026-07-16T12:00:00Z",
        symbol="AAPL",
        price=100,
        holdings=10,
    )
    context = PipelineContext(
        fix_c=1500,
        diff=30,
        dna_code="bypass:100",
        run_id="run-1",
        chain_key="chain-1",
        anchor=PreviousAnchor(),
    )
    previous = None
    for number in range(1, 18):
        previous = run_stage(number, raw, previous, context)
    final = final_dataframe(previous)
    document = build_final_document(
        context=context,
        final=final,
        stage_result=previous,
        environment="Test (UAT)",
        account_fingerprint="abc123",
        strategy_hash="strategy",
        snapshot_summary={"old_trade_log_reads": 0},
    )
    return context, document


def test_step18_create_then_retry_is_idempotent():
    context, document = completed()
    state_ref = Ref()
    row_ref = Ref()
    transaction = Transaction()

    first = _commit_final_row(
        transaction,
        state_ref=state_ref,
        row_ref=row_ref,
        context=context,
        document=document,
    )
    second = _commit_final_row(
        transaction,
        state_ref=state_ref,
        row_ref=row_ref,
        context=context,
        document=document,
    )

    assert first.created is True
    assert first.version == 1
    assert second.created is False
    assert second.version == 1
    assert state_ref.data["latest_row_id"] == "run-1"


def test_step18_rejects_stale_anchor():
    context, document = completed()
    state_ref = Ref({"latest_row_id": "other", "version": 1})
    with pytest.raises(StaleAnchorError):
        _commit_final_row(
            Transaction(),
            state_ref=state_ref,
            row_ref=Ref(),
            context=context,
            document=document,
        )


def test_final_document_keeps_row_and_recurrence_metadata_separate():
    context, document = completed()
    assert document["row"]["สถานะ"] == "READY_BUY"
    assert document["metadata"]["p0"] == 100.0
    assert document["metadata"]["previous_row_id"] is None
    assert document["metadata"]["snapshot"]["old_trade_log_reads"] == 0
