"""Offline rehearsal: deterministic what-if runs isolated from live state.

A rehearsal run takes an initial state and an operation list, executes the
full treatment line inside a sandbox, and records every step's decision basis
behind a hash chain. Runs are idempotent (the run id is the scenario hash),
recoverable (checkpoints after every step), and audited (an append-only
ledger reconciled by the run report).
"""

from .engine import build_sandbox_runtime, execute_run, verify_run
from .manager import RehearsalManager, RunNotFoundError
from .report import build_report, reconcile, replay_trace
from .run import Run
from .scenario import Scenario, parse_scenario
from .world import ReadOnlyStore, capture_world, restore_world, world_hash

__all__ = [
    "ReadOnlyStore",
    "RehearsalManager",
    "Run",
    "RunNotFoundError",
    "Scenario",
    "build_report",
    "build_sandbox_runtime",
    "capture_world",
    "execute_run",
    "parse_scenario",
    "reconcile",
    "replay_trace",
    "restore_world",
    "verify_run",
    "world_hash",
]
