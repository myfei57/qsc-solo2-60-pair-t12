"""Rehearsal manager: idempotent submission, recovery and the run registry.

The manager is the only writer of the rehearsal registry and the only reader
of the live state. Live state is accessed exclusively through a read-only
view, and only at run creation in snapshot mode; the captured world is pinned
into the manifest together with the live state hash and event count, which
records the happens-before edge between the online state and the offline run.
The live store is never written by any rehearsal path.

Idempotency rule: a run id is the content hash of its scenario, so submitting
the same input always resolves to the same run. A completed run is returned
as-is; a failed run resumes from its checkpoint (``resume`` policy) or is
invalidated and restarted from step zero (``restart`` policy).
"""

from __future__ import annotations

import json
import threading

from waterplant.runtime import Runtime
from waterplant.store import Store
from waterplant.store.history import event_count

from .engine import execute_run, restart_run, verify_run
from .ops import known_ops
from .report import build_report, replay_trace
from .run import STATUS_COMPLETED, STATUS_FAILED, STATUS_RUNNING, Run
from .scenario import MODE_SNAPSHOT, POLICY_RESUME, Scenario, parse_patch, parse_scenario
from .world import ReadOnlyStore, snapshot_live, world_hash

INDEX_KEY = "rehearsal:index"
RUN_KEY_PREFIX = "rehearsal:run:"
DEFAULT_EVENT_KEY = "console:events"


class RunNotFoundError(LookupError):
    """Raised when a run id is not present in the registry."""


class RehearsalManager:
    """Owns the registry and drives runs against sandboxed worlds."""

    def __init__(
        self,
        live_runtime: Runtime,
        registry: Store,
        live_event_key: str = DEFAULT_EVENT_KEY,
    ) -> None:
        self._live = ReadOnlyStore(live_runtime.store)
        self._live_bank = live_runtime.bank
        self._registry = registry
        self._event_key = live_event_key
        self._lock = threading.RLock()

    # -- registry helpers -------------------------------------------------

    def _save(self, run: Run) -> None:
        self._registry.put(
            f"{RUN_KEY_PREFIX}{run.run_id}", json.dumps(run.as_dict(), ensure_ascii=False)
        )

    def _load(self, run_id: str) -> Run | None:
        raw, present = self._registry.get(f"{RUN_KEY_PREFIX}{run_id}")
        if not present:
            return None
        return Run.from_dict(json.loads(raw))

    def _require(self, run_id: str) -> Run:
        run = self._load(run_id)
        if run is None:
            raise RunNotFoundError(f"rehearsal run {run_id!r} not found")
        return run

    def _index(self, run_id: str) -> None:
        raw, present = self._registry.get(INDEX_KEY)
        entries = json.loads(raw) if present else []
        if run_id not in entries:
            entries.append(run_id)
            self._registry.put(INDEX_KEY, json.dumps(entries, ensure_ascii=False))

    # -- run creation -----------------------------------------------------

    def _pinned_world(self, scenario: Scenario) -> tuple[dict[str, object], dict[str, object]]:
        if scenario.mode == MODE_SNAPSHOT:
            world = snapshot_live(self._live, self._live_bank)
            provenance = {
                "source": MODE_SNAPSHOT,
                "live_state_hash": world_hash(world),
                "live_event_count": event_count(self._live, self._event_key),
                "live_key_count": self._live.count(),
                "pinned_world_hash": world_hash(world),
            }
            return world, provenance
        initial = scenario.initial or {"store": {}, "beds": []}
        world = {
            "store": {str(key): str(value) for key, value in dict(initial["store"]).items()},
            "beds": [dict(bed) for bed in list(initial["beds"])],
        }
        provenance = {"source": "synthetic", "initial_hash": world_hash(world)}
        return world, provenance

    def _create(self, scenario: Scenario) -> Run:
        world, provenance = self._pinned_world(scenario)
        run = Run(
            run_id=f"run-{scenario.scenario_id[:24]}",
            scenario_id=scenario.scenario_id,
            name=scenario.name,
            mode=scenario.mode,
            failure_policy=scenario.failure_policy,
            operations=[operation.as_dict() for operation in scenario.operations],
            pinned_world=world,
            provenance=provenance,
            checkpoint={"next_index": 0, "world": world},
        )
        run.ledger_append(
            "run_created",
            f"mode={scenario.mode} policy={scenario.failure_policy} "
            f"operations={len(scenario.operations)}",
        )
        self._save(run)
        self._index(run.run_id)
        return run

    # -- public API -------------------------------------------------------

    def submit(self, payload: object) -> dict[str, object]:
        """Create-or-resolve a run for a scenario and drive it forward."""

        scenario = parse_scenario(payload, known_ops())
        run_id = f"run-{scenario.scenario_id[:24]}"
        with self._lock:
            existing = self._load(run_id)
            if existing is not None:
                return self._drive(existing, patch=None, resubmitted=True)
            run = self._create(scenario)
            execute_run(run, self._save)
            report = build_report(self._require(run.run_id))
            report["executed"] = True
            report["idempotent"] = False
            return report

    def resume(self, run_id: str, patch_payload: object | None = None) -> dict[str, object]:
        """Continue a failed run from its breakpoint, optionally patched."""

        with self._lock:
            run = self._require(run_id)
            patch = parse_patch(patch_payload, known_ops()) if patch_payload is not None else None
            return self._drive(run, patch=patch, resubmitted=False)

    def _drive(self, run: Run, patch: dict[str, object] | None, resubmitted: bool) -> dict[str, object]:
        if run.status == STATUS_COMPLETED:
            report = build_report(run)
            report["executed"] = False
            report["idempotent"] = True
            return report

        if patch is not None:
            expected = run.next_index()
            if int(patch["index"]) != expected:
                raise ValueError(
                    f"patch index {patch['index']} does not match the breakpoint {expected}"
                )
            run.patches.append(patch)
            run.ledger_append("patch_applied", f"step {patch['index']} replaced by {patch['op']}")

        if run.failure_policy == POLICY_RESUME:
            run.ledger_append("run_resumed", f"continuing from step {run.next_index()}")
        else:
            if run.steps and run.status == STATUS_RUNNING:
                run.archive_attempt("interrupted", "round restarted before completion")
            run.attempts += 1
            restart_run(run)
            run.ledger_append("run_restarted", f"attempt {run.attempts} from step zero")

        execute_run(run, self._save)
        report = build_report(self._require(run.run_id))
        report["executed"] = True
        report["idempotent"] = False
        report["resubmitted"] = resubmitted
        return report

    # -- read API ---------------------------------------------------------

    def list_runs(self) -> dict[str, object]:
        raw, present = self._registry.get(INDEX_KEY)
        run_ids = json.loads(raw) if present else []
        runs = []
        for run_id in run_ids:
            run = self._load(str(run_id))
            if run is None:
                continue
            runs.append(
                {
                    "run_id": run.run_id,
                    "scenario_id": run.scenario_id,
                    "name": run.name,
                    "mode": run.mode,
                    "failure_policy": run.failure_policy,
                    "status": run.status,
                    "attempts": run.attempts,
                    "steps_executed": len(run.steps),
                    "final_hash": run.final_hash,
                }
            )
        return {"runs": runs, "count": len(runs)}

    def report(self, run_id: str) -> dict[str, object]:
        with self._lock:
            return build_report(self._require(run_id))

    def replay(self, run_id: str) -> dict[str, object]:
        with self._lock:
            return replay_trace(self._require(run_id))

    def audit(self, run_id: str) -> dict[str, object]:
        with self._lock:
            run = self._require(run_id)
            return {"run_id": run.run_id, "entries": run.ledger, "count": len(run.ledger)}

    def verify(self, run_id: str) -> dict[str, object]:
        with self._lock:
            return verify_run(self._require(run_id))
