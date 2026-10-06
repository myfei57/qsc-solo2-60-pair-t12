"""Reconciliation report for the rehearsal ledger.

Every rehearsal run lands in the audit ledger; this report is where the
ledger, the run records and the forked working stores are checked against
each other so a drill can be tied out after the fact.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from waterplant.store import load_commands
from waterplant.audit.auditor import AUDIT_KEY

from .model import STATUS_COMPLETED, STATUS_PAUSED, STATUS_VOID, content_hash

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .engine import RehearsalEngine
    from .model import RunRecord


def _recompute_digest(engine: "RehearsalEngine", record: "RunRecord") -> str:
    """Rebuild the run digest from the persisted record and working store."""

    store = engine.work_store(record.run_id)
    if store is None:
        return ""
    payload = {
        "scenario_id": record.scenario_id,
        "steps": [
            {"index": step.index, "status": step.status, "response": step.response}
            for step in record.steps
        ],
        "store": {key: store.get(key)[0] for key in store.keys()},
        "beds": record.beds,
    }
    return content_hash(payload)


def _run_checks(engine: "RehearsalEngine", record: "RunRecord", ledger: list) -> dict:
    """Cross-check one run record against its store and the ledger trail."""

    store = engine.work_store(record.run_id)
    actual_audit = (
        len(load_commands(store, AUDIT_KEY)) if store is not None else None
    )
    trail = [entry.get("kind", "") for entry in ledger if entry.get("run_id") == record.run_id]
    terminal_event = {
        STATUS_COMPLETED: "run_completed",
        STATUS_PAUSED: "run_paused",
        STATUS_VOID: "run_voided",
    }.get(record.status)
    ledger_ok = "run_started" in trail and (
        terminal_event is None or terminal_event in trail
    )
    checks = {
        "steps_match": record.next_index == len(record.steps)
        and (record.status != STATUS_COMPLETED or record.next_index == record.steps_total),
        "audit_match": actual_audit is not None and actual_audit == record.audit_entries,
        "digest_match": _recompute_digest(engine, record) == record.digest,
        "ledger_trail": trail,
        "ledger_ok": ledger_ok,
    }
    checks["ok"] = bool(
        checks["steps_match"] and checks["audit_match"] and checks["digest_match"] and checks["ledger_ok"]
    )
    return checks


def build_report(engine: "RehearsalEngine") -> dict:
    """Tie out every scenario and run against the ledger and the stores."""

    runs, scenarios = engine.runs_and_scenarios()
    ledger = engine.ledger_entries()
    online_fingerprint = engine.online_fingerprint()

    run_rows = []
    all_ok = True
    for record in runs:
        checks = _run_checks(engine, record, ledger)
        all_ok = all_ok and checks["ok"]
        row = record.summary()
        row["checks"] = checks
        run_rows.append(row)

    scenario_rows = []
    for scenario in scenarios:
        row = scenario.summary()
        row["stale"] = (
            scenario.source == "snapshot"
            and scenario.base_fingerprint != online_fingerprint
        )
        scenario_rows.append(row)

    by_kind: dict[str, int] = {}
    for entry in ledger:
        kind = str(entry.get("kind", ""))
        by_kind[kind] = by_kind.get(kind, 0) + 1

    return {
        "ok": all_ok,
        "online_fingerprint": online_fingerprint,
        "scenarios": scenario_rows,
        "runs": run_rows,
        "totals": {
            "scenarios": len(scenario_rows),
            "runs": len(run_rows),
            "completed": sum(1 for record in runs if record.status == STATUS_COMPLETED),
            "paused": sum(1 for record in runs if record.status == STATUS_PAUSED),
            "void": sum(1 for record in runs if record.status == STATUS_VOID),
            "ledger_events": len(ledger),
        },
        "ledger": {"events": len(ledger), "by_kind": by_kind},
    }
