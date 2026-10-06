"""Deterministic execution engine for rehearsal runs.

The engine restores the checkpointed world, rebuilds the component runtime
over an in-memory sandbox store wired to a logical clock, and applies the
effective operations one step at a time. After every step the checkpoint is
persisted, so a crash or a step failure always leaves a resumable manifest.
Because the world, the clock and the id source are all derived from the
checkpoint, resuming a run produces exactly the outcome an uninterrupted run
would have produced.
"""

from __future__ import annotations

from typing import Callable

from waterplant.audit import Auditor
from waterplant.runtime import Runtime

from .ops import execute_op
from .run import (
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    Run,
)
from .scenario import POLICY_RESUME
from .world import capture_world, logical_id, logical_time, next_tick, restore_world, world_hash

SaveHook = Callable[[Run], None]


def build_sandbox_runtime(world: dict[str, object], run_id: str) -> Runtime:
    """Rebuild the full component wiring over a restored world."""

    store, bank = restore_world(world)
    auditor = Auditor(
        store,
        clock=lambda: logical_time(store),
        idgen=lambda: logical_id(store, run_id),
    )
    return Runtime(store, auditor=auditor, unix_clock=lambda: next_tick(store), bank=bank)


def _record_step(
    run: Run,
    index: int,
    operation: dict[str, object],
    before_hash: str,
    result: dict[str, object] | None,
    basis: dict[str, object] | None,
    error: str,
    after_hash: str,
) -> None:
    run.steps.append(
        {
            "index": index,
            "op": operation["op"],
            "params": operation.get("params", {}),
            "status": "failed" if error else "ok",
            "basis": basis or {},
            "result": result or {},
            "error": error,
            "before_hash": before_hash,
            "after_hash": after_hash,
        }
    )


def execute_run(run: Run, save: SaveHook) -> Run:
    """Run the effective operations from the checkpoint to a terminal state."""

    runtime = build_sandbox_runtime(run.checkpoint_world(), run.run_id)
    operations = run.effective_operations()
    run.status = STATUS_RUNNING

    index = run.next_index()
    while index < len(operations):
        operation = operations[index]
        before_hash = world_hash(capture_world(runtime.store, runtime.bank))
        try:
            result, basis = execute_op(runtime, str(operation["op"]), dict(operation.get("params", {})))
        except Exception as exc:  # noqa: BLE001 - any step failure is a partial failure
            error = str(exc)
            _record_step(run, index, operation, before_hash, None, None, error, before_hash)
            run.ledger_append("step_failed", f"step {index} {operation['op']}: {error}")
            if run.failure_policy == POLICY_RESUME:
                run.status = STATUS_FAILED
                run.ledger_append("run_failed", f"paused at step {index}; resume to continue")
            else:
                run.status = STATUS_ABORTED
                run.archive_attempt(STATUS_ABORTED, error)
                run.ledger_append("run_aborted", f"round invalidated at step {index}")
            save(run)
            return run

        _record_step(
            run, index, operation, before_hash, result, basis, "",
            world_hash(capture_world(runtime.store, runtime.bank)),
        )
        run.ledger_append("step_ok", f"step {index} {operation['op']}")
        run.checkpoint = {
            "next_index": index + 1,
            "world": capture_world(runtime.store, runtime.bank),
        }
        save(run)
        index += 1

    run.status = STATUS_COMPLETED
    run.final_hash = world_hash(capture_world(runtime.store, runtime.bank))
    run.ledger_append("run_completed", f"{len(operations)} operations applied")
    save(run)
    return run


def restart_run(run: Run) -> None:
    """Reset a run to its pinned world so an attempt starts from step zero."""

    run.steps = []
    run.checkpoint = {"next_index": 0, "world": dict(run.pinned_world)}
    run.final_hash = ""
    run.status = STATUS_RUNNING


def _effective_records(steps: list[dict[str, object]]) -> list[dict[str, object]]:
    """Keep the last record per step index.

    A failed step that was later replaced by a patch leaves its record behind
    for the audit trail; the effective trajectory a replay can reproduce is
    the last record written for each index.
    """

    last_position: dict[int, int] = {}
    for position, step in enumerate(steps):
        last_position[int(step["index"])] = position
    return [step for position, step in enumerate(steps) if last_position[int(step["index"])] == position]


def verify_run(run: Run) -> dict[str, object]:
    """Re-execute the recorded input in a fresh sandbox and compare hashes.

    Verification is read-only: it never touches the registry, so verifying a
    run any number of times always yields the same verdict.
    """

    runtime = build_sandbox_runtime(run.pinned_world, run.run_id)
    operations = run.effective_operations()
    recorded = _effective_records(run.steps)
    mismatches: list[dict[str, object]] = []
    checked = 0

    for index, operation in enumerate(operations):
        if index >= len(recorded):
            break
        before_hash = world_hash(capture_world(runtime.store, runtime.bank))
        expected = recorded[index]
        error = ""
        try:
            execute_op(runtime, str(operation["op"]), dict(operation.get("params", {})))
        except Exception as exc:  # noqa: BLE001 - compared against the record
            error = str(exc)
        after_hash = world_hash(capture_world(runtime.store, runtime.bank))
        checked += 1
        if expected["before_hash"] != before_hash:
            mismatches.append({"index": index, "field": "before_hash"})
        if expected["status"] == "failed":
            if not error:
                mismatches.append({"index": index, "field": "status", "detail": "expected failure"})
            elif error != expected["error"]:
                mismatches.append({"index": index, "field": "error"})
        elif error:
            mismatches.append({"index": index, "field": "status", "detail": error})
        if expected["after_hash"] != after_hash:
            mismatches.append({"index": index, "field": "after_hash"})
        if error:
            break

    if len(recorded) != checked:
        mismatches.append(
            {"field": "step_count", "recorded": len(recorded), "recomputed": checked}
        )

    recomputed_final = world_hash(capture_world(runtime.store, runtime.bank))
    final_matches = True
    if run.status == STATUS_COMPLETED and run.final_hash != recomputed_final:
        final_matches = False
        mismatches.append({"field": "final_hash"})

    return {
        "run_id": run.run_id,
        "consistent": not mismatches,
        "steps_checked": checked,
        "first_mismatch": mismatches[0] if mismatches else None,
        "mismatches": mismatches,
        "recorded_final_hash": run.final_hash,
        "recomputed_final_hash": recomputed_final,
        "final_matches": final_matches,
    }
