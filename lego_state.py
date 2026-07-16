"""Transactional persistence for the one-new-row LEGO chain (Step 18).

Three Firestore collections:

* ``webull_lego_rows/{run_id}`` — the immutable final row + redacted provenance.
* ``webull_lego_state/{chain_key}`` — the latest pointer, monotonic version, and
  recurrence metadata (baseline ``P0``, previous price/cumulative, DNA step).
* ``webull_lego_order_audit/{event_id}`` — redacted Preview/Submit results.

Step 18 commits atomically: it verifies the anchor the run read at Step 0 is
still the chain's latest (fail-closed on a stale anchor), creates the row under
the deterministic ``run_id`` (idempotent — a duplicate click is a no-op), and
advances the state pointer in the same transaction.

The commit *decision* is a pure function (:func:`plan_commit`) shared by the real
:class:`FirestoreStateStore` and the :class:`InMemoryStateStore` used in tests,
so both honor identical idempotency and stale-anchor rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import threading
from typing import Any

from lego_one_row import ComputedRow, PipelineError, RunContext, build_final_document


STATE_COLLECTION = "webull_lego_state"
ROWS_COLLECTION = "webull_lego_rows"
ORDER_AUDIT_COLLECTION = "webull_lego_order_audit"


class StaleAnchorError(PipelineError):
    """Raised when the anchor read at Step 0 is no longer the chain's latest."""


@dataclass(frozen=True)
class CommitResult:
    """Outcome of a Step 18 commit."""

    created: bool
    idempotent: bool
    version: int
    row_id: str
    document: dict[str, Any]


@dataclass(frozen=True)
class CommitPlan:
    action: str  # "idempotent" | "create"
    row_id: str
    version: int
    row_document: dict[str, Any] | None
    state_document: dict[str, Any] | None
    existing: dict[str, Any] | None


def _state_document(document: dict[str, Any]) -> dict[str, Any]:
    """Project the row document's metadata into the chain state pointer."""

    metadata = document["metadata"]
    return {
        "chain_key": document["chain_key"],
        "environment": metadata.get("environment"),
        "account_fingerprint": metadata.get("account_fingerprint"),
        "symbol": metadata.get("symbol"),
        "strategy_config_hash": metadata.get("strategy_config_hash"),
        "version": int(document["version"]),
        "latest_row_id": document["run_id"],
        "dna_step": int(metadata["dna_step"]),
        "p0": float(metadata["p0"]),
        "prev_price": float(metadata["prev_price"]),
        "prev_actual": float(metadata["prev_actual"]),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def plan_commit(
    *,
    state: dict[str, Any] | None,
    existing_row: dict[str, Any] | None,
    ctx: RunContext,
    document: dict[str, Any],
) -> CommitPlan:
    """Decide what a Step 18 transaction must write; fail-closed on stale anchor.

    * If ``existing_row`` is present the run already committed → idempotent.
    * The anchor version the run captured at Step 0 must equal the chain's
      current ``state.version`` (or both be genesis) or the anchor is stale.
    """

    if existing_row is not None:
        return CommitPlan(
            action="idempotent",
            row_id=ctx.run_id,
            version=int(existing_row.get("version", ctx.anchor.version + 1)),
            row_document=None,
            state_document=None,
            existing=existing_row,
        )

    if state is None:
        if ctx.anchor.exists:
            raise StaleAnchorError(
                "run carries an anchor but the chain has no state — restart Step 0"
            )
        current_version = 0
    else:
        current_version = int(state.get("version", 0))
        if not ctx.anchor.exists:
            raise StaleAnchorError(
                "run is genesis but the chain already has rows — restart Step 0"
            )
        if int(ctx.anchor.version) != current_version:
            raise StaleAnchorError(
                f"anchor version {ctx.anchor.version} != chain version "
                f"{current_version}; another run advanced the chain — restart Step 0"
            )

    new_version = current_version + 1
    row_document = dict(document)
    row_document["version"] = new_version
    return CommitPlan(
        action="create",
        row_id=ctx.run_id,
        version=new_version,
        row_document=row_document,
        state_document=_state_document(row_document),
        existing=None,
    )


class InMemoryStateStore:
    """Thread-safe in-memory store mirroring the Firestore contract for tests."""

    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}
        self.rows: dict[str, dict[str, Any]] = {}
        self.order_audit: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        # Optional hook to simulate a concurrent writer between read and write.
        self.on_before_write = None

    def read_state(self, chain_key: str) -> dict[str, Any] | None:
        with self._lock:
            state = self.states.get(chain_key)
            return dict(state) if state is not None else None

    def read_row(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.rows.get(run_id)
            return dict(row) if row is not None else None

    def commit_final_row(self, ctx: RunContext, document: dict[str, Any]) -> CommitResult:
        with self._lock:
            state = self.states.get(ctx.chain_key)
            existing_row = self.rows.get(ctx.run_id)
            plan = plan_commit(
                state=state,
                existing_row=existing_row,
                ctx=ctx,
                document=document,
            )
            if plan.action == "idempotent":
                return CommitResult(
                    created=False,
                    idempotent=True,
                    version=plan.version,
                    row_id=plan.row_id,
                    document=dict(plan.existing or {}),
                )
            # Give tests a chance to inject a concurrent writer that advances the
            # chain after the read but before this write, so plan is re-checked.
            if callable(self.on_before_write):
                self.on_before_write(self, ctx)
                state = self.states.get(ctx.chain_key)
                existing_row = self.rows.get(ctx.run_id)
                plan = plan_commit(
                    state=state,
                    existing_row=existing_row,
                    ctx=ctx,
                    document=document,
                )
                if plan.action == "idempotent":
                    return CommitResult(
                        created=False,
                        idempotent=True,
                        version=plan.version,
                        row_id=plan.row_id,
                        document=dict(plan.existing or {}),
                    )
            self.rows[ctx.run_id] = dict(plan.row_document or {})
            self.states[ctx.chain_key] = dict(plan.state_document or {})
            return CommitResult(
                created=True,
                idempotent=False,
                version=plan.version,
                row_id=plan.row_id,
                document=dict(plan.row_document or {}),
            )

    def record_order_audit(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.order_audit[str(event.get("event_id"))] = dict(event)


class FirestoreStateStore:
    """Real Firestore-backed store using an atomic transaction for Step 18."""

    def __init__(self, client: Any):
        self.client = client

    def _state_ref(self, chain_key: str):
        return self.client.collection(STATE_COLLECTION).document(chain_key)

    def _row_ref(self, run_id: str):
        return self.client.collection(ROWS_COLLECTION).document(run_id)

    def read_state(self, chain_key: str) -> dict[str, Any] | None:
        snapshot = self._state_ref(chain_key).get()
        return snapshot.to_dict() if snapshot.exists else None

    def read_row(self, run_id: str) -> dict[str, Any] | None:
        snapshot = self._row_ref(run_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    def commit_final_row(self, ctx: RunContext, document: dict[str, Any]) -> CommitResult:
        from google.cloud import firestore

        state_ref = self._state_ref(ctx.chain_key)
        row_ref = self._row_ref(ctx.run_id)

        @firestore.transactional
        def _commit(transaction) -> CommitResult:
            state_snapshot = state_ref.get(transaction=transaction)
            row_snapshot = row_ref.get(transaction=transaction)
            state = state_snapshot.to_dict() if state_snapshot.exists else None
            existing_row = row_snapshot.to_dict() if row_snapshot.exists else None
            plan = plan_commit(
                state=state,
                existing_row=existing_row,
                ctx=ctx,
                document=document,
            )
            if plan.action == "idempotent":
                return CommitResult(
                    created=False,
                    idempotent=True,
                    version=plan.version,
                    row_id=plan.row_id,
                    document=dict(plan.existing or {}),
                )
            transaction.set(row_ref, plan.row_document)
            transaction.set(state_ref, plan.state_document)
            return CommitResult(
                created=True,
                idempotent=False,
                version=plan.version,
                row_id=plan.row_id,
                document=dict(plan.row_document or {}),
            )

        return _commit(self.client.transaction())

    def record_order_audit(self, event: dict[str, Any]) -> None:
        self.client.collection(ORDER_AUDIT_COLLECTION).document(
            str(event["event_id"])
        ).set(event)


def read_anchor_state(store: Any, chain_key: str) -> dict[str, Any] | None:
    """Read only the latest chain state (never the trade log) for Step 0."""

    return store.read_state(chain_key)


def finalize_row(store: Any, ctx: RunContext, computed: ComputedRow) -> CommitResult:
    """Build and transactionally append the final row for ``ctx`` (Step 18)."""

    document = build_final_document(computed, ctx)
    return store.commit_final_row(ctx, document)
