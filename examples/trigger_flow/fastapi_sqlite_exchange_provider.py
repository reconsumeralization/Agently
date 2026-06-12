import asyncio
import importlib
import json
import os
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from agently import TriggerFlow, TriggerFlowRuntimeData

def _load_fastapi():
    try:
        fastapi = importlib.import_module("fastapi")
    except ImportError as exc:
        raise RuntimeError("Install fastapi and uvicorn to run this example as an API server.") from exc
    fastapi_app = getattr(fastapi, "FastAPI", None)
    http_exception = getattr(fastapi, "HTTPException", None)
    if fastapi_app is None or http_exception is None:
        raise RuntimeError("The installed fastapi package does not expose FastAPI/HTTPException.")
    return fastapi_app, http_exception


class SQLiteExecutionSnapshotStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_snapshots (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    step_id TEXT,
                    state_version INTEGER,
                    state_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_snapshot_heads (
                    run_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL
                )
                """
            )

    def _latest_state_version(self, conn: sqlite3.Connection, run_id: str) -> int | None:
        row = conn.execute(
            """
            SELECT s.state_version
            FROM execution_snapshot_heads h
            JOIN execution_snapshots s ON s.id = h.snapshot_id
            WHERE h.run_id = ?
            """,
            (run_id,),
        ).fetchone()
        return None if row is None else row["state_version"]

    async def put_snapshot(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ):
        snapshot_id = str(state.get("snapshot_id") or uuid.uuid4().hex)
        state_version = int(state.get("state_version", 0))
        created_at = time.time()
        with self._connect() as conn:
            latest_version = self._latest_state_version(conn, run_id)
            if expected_state_version is not None and latest_version != expected_state_version:
                raise RuntimeError(
                    f"Snapshot state version conflict for run '{ run_id }': "
                    f"expected { expected_state_version }, got { latest_version }."
                )
            conn.execute(
                """
                INSERT OR REPLACE INTO execution_snapshots(
                    id, run_id, step_id, state_version, state_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (snapshot_id, run_id, step_id, state_version, json.dumps(state), created_at),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO execution_snapshot_heads(run_id, snapshot_id)
                VALUES (?, ?)
                """,
                (run_id, snapshot_id),
            )
        return {
            "id": snapshot_id,
            "collection": "execution_snapshots",
            "scope": {"run_id": run_id, "step_id": step_id},
            "meta": {"state_version": state_version},
            "source": {"type": "sqlite_snapshot_store", "path": str(self.db_path)},
        }

    async def get_snapshot(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT s.state_json
                FROM execution_snapshot_heads h
                JOIN execution_snapshots s ON s.id = h.snapshot_id
                WHERE h.run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["state_json"])

    async def list_snapshot_steps(self, run_id: str) -> list[str | None]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT step_id
                FROM execution_snapshots
                WHERE run_id = ?
                ORDER BY created_at ASC
                """,
                (run_id,),
            ).fetchall()
        return [row["step_id"] for row in rows]


class SQLiteExecutionExchangeProvider:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_exchange_requests (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    exchange_id TEXT NOT NULL,
                    exchange_kind TEXT,
                    dispatch_state TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    interrupt_json TEXT NOT NULL,
                    response_json TEXT,
                    created_at REAL NOT NULL,
                    completed_at REAL
                )
                """
            )

    def publish_request(self, execution_id: str, request: dict[str, Any], *, interrupt: dict[str, Any]):
        raw_audit_metadata = request.get("audit_metadata")
        audit_metadata: dict[str, Any] = raw_audit_metadata if isinstance(raw_audit_metadata, dict) else {}
        run_id = str(audit_metadata.get("run_id") or execution_id)
        exchange_id = str(request.get("exchange_id") or audit_metadata.get("exchange_id") or uuid.uuid4().hex)
        request_ref = {
            "id": uuid.uuid4().hex,
            "collection": "execution_exchange_requests",
            "scope": {"run_id": run_id, "request_id": request.get("request_id")},
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO execution_exchange_requests(
                    id, run_id, execution_id, request_id, exchange_id, exchange_kind,
                    dispatch_state, request_json, interrupt_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_ref["id"],
                    run_id,
                    execution_id,
                    str(request.get("request_id") or ""),
                    exchange_id,
                    request.get("exchange_kind"),
                    str(request.get("dispatch_state") or "persisted"),
                    json.dumps(request),
                    json.dumps(interrupt),
                    time.time(),
                ),
            )
        return {
            "exchange_id": exchange_id,
            "request_ref": request_ref,
            "provider_metadata": {"provider": "sqlite_execution_exchange"},
        }

    def latest_request(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM execution_exchange_requests
                WHERE run_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "execution_id": row["execution_id"],
            "request_id": row["request_id"],
            "exchange_id": row["exchange_id"],
            "exchange_kind": row["exchange_kind"],
            "dispatch_state": row["dispatch_state"],
            "request": json.loads(row["request_json"]),
            "response": json.loads(row["response_json"]) if row["response_json"] else None,
        }

    def mark_completed(self, exchange_id: str, response: dict[str, Any]):
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE execution_exchange_requests
                SET dispatch_state = ?, response_json = ?, completed_at = ?
                WHERE exchange_id = ?
                """,
                ("completed", json.dumps(response), time.time(), exchange_id),
            )


discount_approval_flow = TriggerFlow(name="fastapi-sqlite-discount-approval")


@discount_approval_flow.chunk
async def request_approval(data: TriggerFlowRuntimeData):
    request = dict(data.value or {})
    await data.async_set_state("request", request, emit=False)
    run_id = data.execution.run_context.run_id
    return await data.async_pause_for(
        type="exchange",
        exchange_kind="approval",
        payload={
            "customer": request.get("customer"),
            "requested_discount": request.get("discount_percent"),
        },
        interrupt_id="discount-approval",
        resume_to="next",
        channel_id="fastapi",
        provider_id="sqlite-exchange",
        wait_mode="disconnected",
        cold_persistence_policy="persist",
        request_payload_schema={
            "type": "object",
            "required": ["customer", "requested_discount"],
        },
        response_payload_schema={
            "type": "object",
            "required": ["approved", "approved_discount"],
        },
        audit_metadata={"run_id": run_id, "exchange_id": f"discount-approval:{ run_id }"},
    )


@discount_approval_flow.chunk
async def finalize(data: TriggerFlowRuntimeData):
    request = dict(data.get_state("request") or {})
    approval = dict(data.value or {})
    final = {
        "customer": request.get("customer"),
        "status": "approved" if approval.get("approved") else "denied",
        "approved_discount": approval.get("approved_discount", 0),
    }
    await data.async_set_state("final", final, emit=False)
    await data.async_emit("DISCOUNT_APPROVAL_FINALIZED", final)


@discount_approval_flow.chunk
async def record_audit(data: TriggerFlowRuntimeData):
    final = dict(data.value or {})
    await data.async_set_state(
        "audit",
        {
            "event": data.event,
            "status": final.get("status"),
            "approved_discount": final.get("approved_discount"),
        },
        emit=False,
    )


discount_approval_flow.to(request_approval).to(finalize)
discount_approval_flow.when("DISCOUNT_APPROVAL_FINALIZED").to(record_audit)


def _bind_providers(execution: Any, snapshot_store: SQLiteExecutionSnapshotStore, exchange_provider: SQLiteExecutionExchangeProvider):
    execution.update_runtime_resources(
        {
            "snapshot_store": snapshot_store,
            "execution_exchange_provider": exchange_provider,
        }
    )
    return execution


async def start_discount_run(
    request: dict[str, Any],
    *,
    snapshot_store: SQLiteExecutionSnapshotStore,
    exchange_provider: SQLiteExecutionExchangeProvider,
):
    execution = _bind_providers(
        discount_approval_flow.create_execution(auto_close=False),
        snapshot_store,
        exchange_provider,
    )
    await execution.async_start(request)
    snapshot_ref = await execution.async_save(step_id="waiting-approval")
    exchange_request = exchange_provider.latest_request(execution.run_context.run_id)
    return {
        "run_id": execution.run_context.run_id,
        "execution_id": execution.id,
        "snapshot_ref": snapshot_ref,
        "pending_exchange": exchange_request,
    }


async def approve_discount_run(
    run_id: str,
    decision: dict[str, Any],
    *,
    snapshot_store: SQLiteExecutionSnapshotStore,
    exchange_provider: SQLiteExecutionExchangeProvider,
):
    saved_snapshot = await snapshot_store.get_snapshot(run_id)
    if saved_snapshot is None:
        raise KeyError(f"No snapshot found for run '{ run_id }'.")
    exchange_request = exchange_provider.latest_request(run_id)
    if exchange_request is None:
        raise KeyError(f"No exchange request found for run '{ run_id }'.")
    execution = _bind_providers(
        discount_approval_flow.create_execution(auto_close=False),
        snapshot_store,
        exchange_provider,
    )
    load = await execution.async_load(saved_snapshot)
    resume_request_id = str(
        decision.get("resume_request_id")
        or f"fastapi:{ exchange_request['exchange_id'] }:decision"
    )
    response = {
        "approved": bool(decision.get("approved", True)),
        "approved_discount": int(decision.get("approved_discount", 0)),
    }
    await execution.async_continue_with(
        "discount-approval",
        response,
        resume_request_id=resume_request_id,
        actor=str(decision.get("actor") or "api"),
    )
    close_snapshot = await execution.async_close()
    snapshot_ref = await execution.async_save(step_id="approved")
    exchange_provider.mark_completed(exchange_request["exchange_id"], response)
    return {
        "run_id": run_id,
        "load_ready": load["ready"],
        "snapshot_ref": snapshot_ref,
        "final": close_snapshot.get("final"),
        "audit": close_snapshot.get("audit"),
    }


def create_app(db_path: str | Path | None = None):
    FastAPI, HTTPException = _load_fastapi()
    resolved_db_path = Path(
        db_path
        or os.environ.get("AGENTLY_TRIGGERFLOW_SQLITE_DB", ".agently/examples/triggerflow-fastapi.sqlite3")
    )
    snapshot_store = SQLiteExecutionSnapshotStore(resolved_db_path)
    exchange_provider = SQLiteExecutionExchangeProvider(resolved_db_path)
    app = FastAPI(title="Agently TriggerFlow SQLite Exchange Example")

    @app.post("/runs")
    async def create_run(payload: dict[str, Any]):
        return await start_discount_run(
            payload,
            snapshot_store=snapshot_store,
            exchange_provider=exchange_provider,
        )

    @app.post("/runs/{run_id}/approve")
    async def approve_run(run_id: str, payload: dict[str, Any]):
        try:
            return await approve_discount_run(
                run_id,
                payload,
                snapshot_store=snapshot_store,
                exchange_provider=exchange_provider,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


async def smoke():
    with tempfile.TemporaryDirectory(prefix="agently-triggerflow-sqlite-") as temp_dir:
        db_path = Path(temp_dir) / "runtime.sqlite3"
        snapshot_store = SQLiteExecutionSnapshotStore(db_path)
        exchange_provider = SQLiteExecutionExchangeProvider(db_path)
        started = await start_discount_run(
            {"customer": "Acme", "discount_percent": 18},
            snapshot_store=snapshot_store,
            exchange_provider=exchange_provider,
        )
        approved = await approve_discount_run(
            started["run_id"],
            {"approved": True, "approved_discount": 18, "actor": "sales-ops"},
            snapshot_store=snapshot_store,
            exchange_provider=exchange_provider,
        )
        steps = await snapshot_store.list_snapshot_steps(started["run_id"])
        key_output = {
            "pending_exchange_kind": started["pending_exchange"]["exchange_kind"],
            "load_ready": approved["load_ready"],
            "final_status": approved["final"]["status"],
            "approved_discount": approved["final"]["approved_discount"],
            "audit_status": approved["audit"]["status"],
            "stored_snapshot_steps": steps,
        }
        print(key_output)


if __name__ == "__main__":
    asyncio.run(smoke())

# From a source checkout, run the API server with:
# PYTHONPATH=. uvicorn examples.trigger_flow.fastapi_sqlite_exchange_provider:create_app --factory
#
# Expected key output from:
# PYTHONPATH=. python examples/trigger_flow/fastapi_sqlite_exchange_provider.py
# {
#     'pending_exchange_kind': 'approval',
#     'load_ready': True,
#     'final_status': 'approved',
#     'approved_discount': 18,
#     'audit_status': 'approved',
#     'stored_snapshot_steps': ['waiting-approval', 'approved'],
# }
#
# How it works:
# discount_approval_flow is the module-level flow definition. The module body
# registers chunks with @discount_approval_flow.chunk and declares the graph with
# .to(...) plus .when(...). The first worker starts an execution from the
# imported flow, the SQLite exchange provider records the approval request, and
# the SQLite snapshot store persists the top-level execution snapshot. A later
# worker imports the same module, reads only the serialized snapshot from
# SQLite, loads, and resumes the exchange with a stable API resume_request_id.
