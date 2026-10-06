"""Persistent manifest of one rehearsal run.

The manifest carries everything needed to resume, replay and audit a run:
the pinned starting world, the effective operations, per-step records with
their decision basis and hash chain, the resumable checkpoint, the append-only
audit ledger and the archived attempts. It is plain JSON so the registry can
store it as a single document.
"""

from __future__ import annotations

from dataclasses import dataclass, field

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_ABORTED = "aborted"
STATUSES = (STATUS_RUNNING, STATUS_COMPLETED, STATUS_FAILED, STATUS_ABORTED)

TERMINAL_LEDGER_KIND = {
    STATUS_COMPLETED: "run_completed",
    STATUS_FAILED: "run_failed",
    STATUS_ABORTED: "run_aborted",
}


@dataclass
class Run:
    """One rehearsal round from creation to its terminal state."""

    run_id: str
    scenario_id: str
    name: str
    mode: str
    failure_policy: str
    operations: list[dict[str, object]]
    pinned_world: dict[str, object]
    provenance: dict[str, object]
    status: str = STATUS_RUNNING
    steps: list[dict[str, object]] = field(default_factory=list)
    checkpoint: dict[str, object] = field(default_factory=dict)
    patches: list[dict[str, object]] = field(default_factory=list)
    attempts: int = 1
    attempt_log: list[dict[str, object]] = field(default_factory=list)
    ledger: list[dict[str, object]] = field(default_factory=list)
    final_hash: str = ""

    def effective_operations(self) -> list[dict[str, object]]:
        """Scenario operations with every accepted patch applied in order."""

        operations = [dict(operation) for operation in self.operations]
        for patch in self.patches:
            index = int(patch["index"])
            if 0 <= index < len(operations):
                operations[index] = {"op": patch["op"], "params": patch["params"]}
        return operations

    def next_index(self) -> int:
        return int(self.checkpoint.get("next_index", 0))

    def checkpoint_world(self) -> dict[str, object]:
        world = self.checkpoint.get("world")
        return dict(world) if isinstance(world, dict) else dict(self.pinned_world)

    def ledger_append(self, kind: str, detail: str) -> None:
        self.ledger.append({"seq": len(self.ledger), "kind": kind, "detail": detail})

    def archive_attempt(self, outcome: str, error: str) -> None:
        self.attempt_log.append(
            {
                "attempt": self.attempts,
                "outcome": outcome,
                "steps_completed": sum(1 for step in self.steps if step["status"] == "ok"),
                "failed_index": self.next_index(),
                "error": error,
            }
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "name": self.name,
            "mode": self.mode,
            "failure_policy": self.failure_policy,
            "operations": self.operations,
            "pinned_world": self.pinned_world,
            "provenance": self.provenance,
            "status": self.status,
            "steps": self.steps,
            "checkpoint": self.checkpoint,
            "patches": self.patches,
            "attempts": self.attempts,
            "attempt_log": self.attempt_log,
            "ledger": self.ledger,
            "final_hash": self.final_hash,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Run":
        return cls(
            run_id=str(payload["run_id"]),
            scenario_id=str(payload["scenario_id"]),
            name=str(payload.get("name", "")),
            mode=str(payload["mode"]),
            failure_policy=str(payload["failure_policy"]),
            operations=list(payload.get("operations", [])),
            pinned_world=dict(payload.get("pinned_world", {})),
            provenance=dict(payload.get("provenance", {})),
            status=str(payload.get("status", STATUS_RUNNING)),
            steps=list(payload.get("steps", [])),
            checkpoint=dict(payload.get("checkpoint", {})),
            patches=list(payload.get("patches", [])),
            attempts=int(payload.get("attempts", 1)),
            attempt_log=list(payload.get("attempt_log", [])),
            ledger=list(payload.get("ledger", [])),
            final_hash=str(payload.get("final_hash", "")),
        )
