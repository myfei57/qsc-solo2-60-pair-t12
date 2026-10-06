"""Run report with audit reconciliation.

Every rehearsal round lands its audit entries in the run ledger; the report
recomputes its counters from both the step records and the ledger and only
reports ``reconciled`` when the two views agree, so a report can always be
tied back to the audit trail of its round.
"""

from __future__ import annotations

from .run import STATUS_COMPLETED, TERMINAL_LEDGER_KIND, Run
from .world import world_hash


def _ledger_counts(run: Run) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in run.ledger:
        kind = str(entry["kind"])
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _current_attempt_counts(run: Run) -> dict[str, int]:
    """Step counters of the latest attempt; the ledger spans all attempts."""

    counts = {"step_ok": 0, "step_failed": 0}
    for entry in run.ledger:
        kind = str(entry["kind"])
        if kind in ("run_created", "run_restarted"):
            counts = {"step_ok": 0, "step_failed": 0}
        elif kind in counts:
            counts[kind] += 1
    return counts


def reconcile(run: Run) -> dict[str, object]:
    """Cross-check the step records, the ledger and the terminal state."""

    counts = _ledger_counts(run)
    attempt = _current_attempt_counts(run)
    steps_ok = sum(1 for step in run.steps if step["status"] == "ok")
    steps_failed = sum(1 for step in run.steps if step["status"] == "failed")

    steps_match = attempt["step_ok"] == steps_ok and attempt["step_failed"] == steps_failed

    terminal_kind = TERMINAL_LEDGER_KIND.get(run.status)
    terminal_matches = terminal_kind is None or counts.get(terminal_kind, 0) >= 1

    checkpoint = run.checkpoint.get("world")
    hash_matches = True
    if run.status == STATUS_COMPLETED and isinstance(checkpoint, dict):
        hash_matches = run.final_hash == world_hash(checkpoint)

    ledger_sequential = all(
        int(entry["seq"]) == index for index, entry in enumerate(run.ledger)
    )

    reconciled = steps_match and terminal_matches and hash_matches and ledger_sequential
    return {
        "steps_ok": steps_ok,
        "steps_failed": steps_failed,
        "attempt_step_ok": attempt["step_ok"],
        "attempt_step_failed": attempt["step_failed"],
        "ledger_step_ok": counts.get("step_ok", 0),
        "ledger_step_failed": counts.get("step_failed", 0),
        "step_records_match_ledger": steps_match,
        "terminal_event_matches_status": terminal_matches,
        "final_hash_matches_checkpoint": hash_matches,
        "ledger_sequential": ledger_sequential,
        "reconciled": reconciled,
    }


def build_report(run: Run) -> dict[str, object]:
    """The reconciled report one round files for the record."""

    counts = _ledger_counts(run)
    reconciliation = reconcile(run)
    return {
        "run_id": run.run_id,
        "scenario_id": run.scenario_id,
        "name": run.name,
        "mode": run.mode,
        "failure_policy": run.failure_policy,
        "status": run.status,
        "attempts": run.attempts,
        "operations_planned": len(run.effective_operations()),
        "steps_executed": len(run.steps),
        "steps_ok": reconciliation["steps_ok"],
        "steps_failed": reconciliation["steps_failed"],
        "next_index": run.next_index(),
        "final_hash": run.final_hash,
        "provenance": run.provenance,
        "patches": run.patches,
        "attempt_log": run.attempt_log,
        "audit": {
            "entries": len(run.ledger),
            "by_kind": counts,
        },
        "reconciliation": reconciliation,
        "reconciled": reconciliation["reconciled"],
    }


def replay_trace(run: Run) -> dict[str, object]:
    """The step-by-step trace with the decision basis of every step."""

    chain_valid = all(
        run.steps[index]["after_hash"] == run.steps[index + 1]["before_hash"]
        for index in range(len(run.steps) - 1)
    )
    return {
        "run_id": run.run_id,
        "status": run.status,
        "mode": run.mode,
        "failure_policy": run.failure_policy,
        "provenance": run.provenance,
        "steps": run.steps,
        "hash_chain_valid": chain_valid,
        "ledger": run.ledger,
        "final_hash": run.final_hash,
    }
