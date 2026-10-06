"""Offline rehearsal engine.

The engine runs a scenario (an initial state plus a fixed list of console
operations) against a forked store, so the whole treatment line can be
exercised offline without touching live data. Every run checkpoints after
each step, replays from the frozen base, and hashes its outcome so the same
input always produces the same, verifiable result.

Layout on disk, next to the live store::

    <live-store>.rehearsal/
        root.json            scenarios, run records, audit ledger
        run-<run_id>.json    working store forked for one run

The live store is only ever read, and only when a ``snapshot`` scenario is
created. Everything a run needs is frozen into the scenario at creation
time, so replays stay reproducible after the live state has moved on.
"""

from __future__ import annotations

import json
import os
import shutil
import threading

from waterplant.audit.auditor import AUDIT_KEY
from waterplant.console.http import Request
from waterplant.console.runtime import Runtime
from waterplant.console.seed import seed_defaults
from waterplant.console.server import Server
from waterplant.store import Store, load_commands

from .model import (
    MAX_OPS,
    ON_FAILURE_POLICIES,
    ON_FAILURE_VOID,
    SOURCES,
    SOURCE_CONSTRUCTED,
    SOURCE_SNAPSHOT,
    STATUS_COMPLETED,
    STATUS_PAUSED,
    STATUS_RUNNING,
    STATUS_VOID,
    Op,
    RunRecord,
    Scenario,
    StepRecord,
    content_hash,
    run_id_for,
    scenario_identity,
    validate_base,
)
from .report import build_report

LOGICAL_EPOCH = 1_700_000_000
QUOTA_LIMIT = 100.0

SCENARIO_KEY_PREFIX = "rehearsal:scenario:"
RUN_KEY_PREFIX = "rehearsal:run:"
LEDGER_KEY = "rehearsal:ledger"


class _Counter:
    """Monotonic counter used to build deterministic clocks and id sources."""

    def __init__(self, start: int = 0) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1
        return self.value


def _dump_store(store: Store) -> dict[str, str]:
    return {key: store.get(key)[0] for key in store.keys()}


def _decode_body(body: bytes, content_type: str) -> object:
    text = body.decode("utf-8")
    if content_type.startswith("application/json"):
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def _restore_beds(runtime: Runtime, beds: list[dict[str, object]]) -> None:
    """Rebuild the in-memory filter bank from a checkpointed bed list."""

    for bed in beds:
        runtime.bank.add_bed(str(bed["id"]), int(bed["zone"]), float(bed["load"]))
    for bed in beds:
        if bed.get("closed"):
            runtime.bank.close(str(bed["id"]))
    for bed in beds:
        if bed.get("duty"):
            runtime.bank.rotate(str(bed["id"]))


def _beds_snapshot(runtime: Runtime) -> list[dict[str, object]]:
    return [bed.as_dict() for bed in runtime.bank.state().beds]


def _decision_basis(runtime: Runtime) -> dict[str, object]:
    """The readings a step sees before it runs: why the line did what it did."""

    flow, present = runtime.flow_repository.load_flow()
    return {
        "flow": flow,
        "flow_present": present,
        "ratio": runtime.coag_doser.current_ratio(),
        "residual_target": runtime.well.residual_target(),
        "level": runtime.well.level(),
        "ph": runtime.stabilizer.stabilize().as_dict(),
        "quota_remaining": runtime.accumulator.remaining(QUOTA_LIMIT),
        "bank": runtime.bank.state().as_dict(),
        "eligible": runtime.backwash.eligible(),
        "backwash_pending": runtime.backwash.command_list(),
        "audit_count": runtime.auditor.count(),
        "trend": runtime.trend.stats().as_dict(),
    }


def _digest(scenario_id: str, steps: list[StepRecord], store: Store, beds: list) -> str:
    """Hash the outcome of a run: step results plus the final forked state."""

    payload = {
        "scenario_id": scenario_id,
        "steps": [
            {"index": step.index, "status": step.status, "response": step.response}
            for step in steps
        ],
        "store": _dump_store(store),
        "beds": beds,
    }
    return content_hash(payload)


class RehearsalEngine:
    """Creates scenarios and executes, resumes, replays and reconciles runs."""

    def __init__(self, online: Runtime, root_dir: str) -> None:
        self._online = online
        self._root_dir = root_dir
        os.makedirs(root_dir, exist_ok=True)
        self._root = Store.open(os.path.join(root_dir, "root.json"))
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # scenarios

    def create_scenario(self, spec: dict[str, object]) -> tuple[dict[str, object], bool]:
        """Validate and persist a scenario. Repeat submissions are idempotent."""

        with self._lock:
            source = str(spec.get("source", ""))
            if source not in SOURCES:
                raise ValueError(f"source must be one of {', '.join(SOURCES)}")
            on_failure = str(spec.get("on_failure", ""))
            if on_failure not in ON_FAILURE_POLICIES:
                raise ValueError(
                    f"on_failure must be one of {', '.join(ON_FAILURE_POLICIES)}"
                )
            ops_raw = spec.get("ops", [])
            if not isinstance(ops_raw, list) or not ops_raw:
                raise ValueError("ops must be a non-empty list")
            if len(ops_raw) > MAX_OPS:
                raise ValueError(f"ops must not exceed {MAX_OPS}")
            ops = [Op.from_dict(item, index) for index, item in enumerate(ops_raw)]
            seed = bool(spec.get("seed", True))

            if source == SOURCE_SNAPSHOT:
                base = self._capture_online()
            else:
                base = validate_base(spec.get("base"))
            fingerprint = content_hash(base)
            identity = scenario_identity(source, on_failure, seed, base, ops)
            scenario_id = f"sc-{identity[:16]}"

            existing = self._load_scenario(scenario_id)
            if existing is not None:
                return existing.as_dict(), False

            name = str(spec.get("name", "") or f"scenario-{scenario_id[3:11]}")
            purpose = str(spec.get("purpose", ""))
            scenario = Scenario(
                scenario_id=scenario_id,
                name=name,
                purpose=purpose,
                source=source,
                on_failure=on_failure,
                seed=seed,
                base=base,
                ops=ops,
                base_fingerprint=fingerprint,
            )
            self._root.put(
                f"{SCENARIO_KEY_PREFIX}{scenario_id}",
                json.dumps(scenario.as_dict(), ensure_ascii=False),
            )
            self._ledger("scenario_created", scenario_id, f"source={source} ops={len(ops)}")
            return scenario.as_dict(), True

    def list_scenarios(self) -> list[dict[str, object]]:
        with self._lock:
            summaries = []
            for key in self._root.keys():
                if key.startswith(SCENARIO_KEY_PREFIX):
                    scenario = self._load_scenario(key[len(SCENARIO_KEY_PREFIX) :])
                    if scenario is not None:
                        summaries.append(self._scenario_summary(scenario))
            return summaries

    def find_scenario(self, scenario_id: str) -> Scenario | None:
        with self._lock:
            return self._load_scenario(scenario_id)

    # ------------------------------------------------------------------
    # runs

    def start_run(self, scenario_id: str) -> tuple[dict[str, object], bool]:
        """Start the run for a scenario.

        A scenario has exactly one run. Re-submitting the same scenario
        returns the existing record without re-executing, so repeating the
        same input is idempotent.
        """

        with self._lock:
            scenario = self._require_scenario(scenario_id)
            run_id = run_id_for(scenario_id)
            existing = self._load_run(run_id)
            if existing is not None:
                return existing.summary(), False

            record = RunRecord(
                run_id=run_id,
                scenario_id=scenario_id,
                status=STATUS_RUNNING,
                source=scenario.source,
                on_failure=scenario.on_failure,
                steps_total=len(scenario.ops),
                beds=[dict(bed) for bed in scenario.base["beds"]],
            )
            store = self._fork(scenario, self._work_path(run_id))
            self._drop_backup(run_id)
            record.base_audit_entries = self._audit_count(store)
            record.digest = _digest(scenario_id, record.steps, store, record.beds)
            self._save_run(record)
            self._ledger("run_started", run_id, f"scenario={scenario_id}")
            self._execute(scenario, record, store, 0, len(scenario.ops))
            return record.summary(), True

    def resume_run(self, run_id: str) -> tuple[dict[str, object], bool]:
        """Continue a paused (or crashed) run from its last checkpoint.

        A crash can leave a step half applied: the run record then carries a
        ``pending_step`` marker and the working store is first rolled back to
        the pre-step snapshot, so the interrupted step is re-executed exactly
        once.
        """

        with self._lock:
            record = self._require_run(run_id)
            if record.status == STATUS_COMPLETED:
                return record.summary(), False
            if record.status == STATUS_VOID:
                raise ValueError(f"run {run_id} is void and cannot be resumed")
            scenario = self._require_scenario(record.scenario_id)
            path = self._work_path(run_id)
            if not os.path.exists(path):
                raise ValueError(f"working store for run {run_id} is missing")
            if record.pending_step >= 0:
                backup = self._bak_path(run_id)
                if os.path.exists(backup):
                    os.replace(backup, path)
                record.steps = record.steps[: record.pending_step]
                record.next_index = record.pending_step
                record.pending_step = -1
            store = Store.open(path)
            record.status = STATUS_RUNNING
            self._save_run(record)
            self._ledger("run_resumed", run_id, f"next_index={record.next_index}")
            self._execute(scenario, record, store, record.next_index, len(scenario.ops))
            return record.summary(), True

    def replay_run(self, run_id: str) -> dict[str, object]:
        """Re-execute the recorded prefix from the frozen base and compare.

        Replay never touches the live store or the run's working store; it
        executes into a scratch fork and verifies that the same input
        reproduces the recorded outcome exactly.
        """

        with self._lock:
            record = self._require_run(run_id)
            scenario = self._require_scenario(record.scenario_id)
            replay_path = os.path.join(self._root_dir, f"replay-{run_id}.json")
            try:
                store = self._fork(scenario, replay_path)
                replay = RunRecord(
                    run_id=record.run_id,
                    scenario_id=record.scenario_id,
                    status=STATUS_RUNNING,
                    source=record.source,
                    on_failure=record.on_failure,
                    steps_total=record.steps_total,
                    beds=[dict(bed) for bed in scenario.base["beds"]],
                )
                replay.base_audit_entries = self._audit_count(store)
                replay.digest = _digest(record.scenario_id, [], store, replay.beds)
                self._execute(
                    scenario,
                    replay,
                    store,
                    0,
                    len(record.steps),
                    persist=False,
                    stop_on_failure=False,
                )
                consistent = (
                    replay.digest == record.digest
                    and [step.status for step in replay.steps]
                    == [step.status for step in record.steps]
                    and [step.response for step in replay.steps]
                    == [step.response for step in record.steps]
                )
            finally:
                if os.path.exists(replay_path):
                    os.remove(replay_path)
            self._ledger(
                "run_replayed",
                run_id,
                f"consistent={consistent} steps={len(record.steps)}",
            )
            return {
                "run_id": run_id,
                "consistent": consistent,
                "steps_checked": len(record.steps),
                "digest": replay.digest,
                "recorded_digest": record.digest,
            }

    def list_runs(self) -> list[dict[str, object]]:
        with self._lock:
            runs = []
            for key in self._root.keys():
                if key.startswith(RUN_KEY_PREFIX):
                    record = self._load_run(key[len(RUN_KEY_PREFIX) :])
                    if record is not None:
                        runs.append(record.summary())
            return runs

    def run_steps(self, run_id: str) -> dict[str, object]:
        """The full step-by-step record, including each step's decision basis."""

        with self._lock:
            return self._require_run(run_id).as_dict()

    def report(self) -> dict[str, object]:
        """Reconcile run records, working stores and the audit ledger."""

        with self._lock:
            return build_report(self)

    # ------------------------------------------------------------------
    # internals used by the report builder

    def online_fingerprint(self) -> str:
        """Fingerprint of the live state a snapshot scenario would fork now."""

        return content_hash(self._capture_online())

    def work_store(self, run_id: str) -> Store | None:
        path = self._work_path(run_id)
        if not os.path.exists(path):
            return None
        return Store.open(path)

    def ledger_entries(self) -> list[dict[str, object]]:
        entries = []
        for item in load_commands(self._root, LEDGER_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if isinstance(payload, dict):
                entries.append(payload)
        return entries

    def runs_and_scenarios(self) -> tuple[list[RunRecord], list[Scenario]]:
        runs = []
        scenarios = []
        for key in self._root.keys():
            if key.startswith(RUN_KEY_PREFIX):
                record = self._load_run(key[len(RUN_KEY_PREFIX) :])
                if record is not None:
                    runs.append(record)
            elif key.startswith(SCENARIO_KEY_PREFIX):
                scenario = self._load_scenario(key[len(SCENARIO_KEY_PREFIX) :])
                if scenario is not None:
                    scenarios.append(scenario)
        return runs, scenarios

    # ------------------------------------------------------------------
    # execution

    def _execute(
        self,
        scenario: Scenario,
        record: RunRecord,
        store: Store,
        start: int,
        stop: int,
        persist: bool = True,
        stop_on_failure: bool = True,
    ) -> None:
        """Run ops[start:stop] against the forked store, checkpointing per step.

        With ``persist`` false (replays) the record is only advanced in
        memory: neither the run record nor the audit ledger is touched. With
        ``stop_on_failure`` false a failed step is recorded and execution
        continues, which is how a replay reproduces a run that was resumed
        past a failure.
        """

        clock = _Counter(record.clock_tick)
        ids = _Counter(record.id_seq)
        runtime = Runtime(
            store,
            clock=lambda: LOGICAL_EPOCH + clock(),
            ids=lambda: f"rh-{ids():08d}",
        )
        _restore_beds(runtime, record.beds)
        server = Server(store, runtime=runtime)

        for index in range(start, stop):
            op = scenario.ops[index]
            basis = _decision_basis(runtime)
            if persist:
                # Write-ahead: snapshot the working store and mark the step as
                # in flight, so a crash mid-step can be rolled back and the
                # step re-executed exactly once on resume.
                shutil.copyfile(self._work_path(record.run_id), self._bak_path(record.run_id))
                record.pending_step = index
                self._save_run(record)
            request = Request(
                method=op.method, path=op.path, query={}, payload=dict(op.payload)
            )
            try:
                response = server.respond(request)
                status = response.status
                body = _decode_body(response.body, response.content_type)
                error = ""
                if status >= 400:
                    error = (
                        str(body.get("error", ""))
                        if isinstance(body, dict)
                        else str(body)
                    )
            except Exception as exc:  # noqa: BLE001 - a failed step is partial failure
                status, body, error = 500, {}, str(exc)

            record.steps.append(
                StepRecord(
                    index=index,
                    op=op.as_dict(),
                    basis=basis,
                    status=status,
                    response=body,
                    error=error,
                )
            )
            record.next_index = index + 1
            record.pending_step = -1
            record.clock_tick = clock.value
            record.id_seq = ids.value
            record.beds = _beds_snapshot(runtime)
            record.audit_entries = runtime.auditor.count()
            record.digest = _digest(
                record.scenario_id, record.steps, store, record.beds
            )
            if status >= 400:
                record.error = f"step {index} failed with status {status}: {error}"
                if not stop_on_failure:
                    continue
                if record.on_failure == ON_FAILURE_VOID:
                    record.status = STATUS_VOID
                else:
                    record.status = STATUS_PAUSED
                if persist:
                    self._ledger("step_failed", record.run_id, record.error)
                    self._save_run(record)
                    terminal = "run_voided" if record.status == STATUS_VOID else "run_paused"
                    self._ledger(terminal, record.run_id, record.error)
                return
            if persist:
                self._save_run(record)

        record.status = STATUS_COMPLETED
        record.error = ""
        if persist:
            self._save_run(record)
            self._ledger("run_completed", record.run_id, f"digest={record.digest}")
            self._drop_backup(record.run_id)

    # ------------------------------------------------------------------
    # forking and capture

    def _capture_online(self) -> dict[str, object]:
        """Freeze the live store document and filter bank into a scenario base."""

        return {
            "store": _dump_store(self._online.store),
            "beds": _beds_snapshot(self._online),
        }

    def _fork(self, scenario: Scenario, path: str) -> Store:
        """Create a fresh working store from the scenario's frozen base."""

        if os.path.exists(path):
            os.remove(path)
        store = Store.open(path)
        if scenario.source == SOURCE_CONSTRUCTED and scenario.seed:
            seed_defaults(store, event_at=LOGICAL_EPOCH)
        for key, value in scenario.base["store"].items():
            store.put(key, value)
        return store

    def _work_path(self, run_id: str) -> str:
        return os.path.join(self._root_dir, f"run-{run_id}.json")

    def _bak_path(self, run_id: str) -> str:
        return os.path.join(self._root_dir, f"run-{run_id}.bak.json")

    def _drop_backup(self, run_id: str) -> None:
        backup = self._bak_path(run_id)
        if os.path.exists(backup):
            os.remove(backup)

    @staticmethod
    def _audit_count(store: Store) -> int:
        return len(load_commands(store, AUDIT_KEY))

    # ------------------------------------------------------------------
    # persistence

    def _load_scenario(self, scenario_id: str) -> Scenario | None:
        raw, present = self._root.get(f"{SCENARIO_KEY_PREFIX}{scenario_id}")
        if not present:
            return None
        try:
            return Scenario.from_dict(json.loads(raw))
        except (ValueError, TypeError):
            return None

    def _require_scenario(self, scenario_id: str) -> Scenario:
        scenario = self._load_scenario(scenario_id)
        if scenario is None:
            raise KeyError(f"unknown scenario {scenario_id}")
        return scenario

    def _load_run(self, run_id: str) -> RunRecord | None:
        raw, present = self._root.get(f"{RUN_KEY_PREFIX}{run_id}")
        if not present:
            return None
        try:
            return RunRecord.from_dict(json.loads(raw))
        except (ValueError, TypeError):
            return None

    def _require_run(self, run_id: str) -> RunRecord:
        record = self._load_run(run_id)
        if record is None:
            raise KeyError(f"unknown run {run_id}")
        return record

    def _save_run(self, record: RunRecord) -> None:
        self._root.put(
            f"{RUN_KEY_PREFIX}{record.run_id}",
            json.dumps(record.as_dict(), ensure_ascii=False),
        )

    def _ledger(self, kind: str, run_id: str, detail: str) -> None:
        entries = load_commands(self._root, LEDGER_KEY)
        entry = {"seq": len(entries) + 1, "kind": kind, "run_id": run_id, "detail": detail}
        entries.append(json.dumps(entry, ensure_ascii=False))
        self._root.put(LEDGER_KEY, json.dumps(entries, ensure_ascii=False))

    def _scenario_summary(self, scenario: Scenario) -> dict[str, object]:
        summary = scenario.summary()
        if scenario.source == SOURCE_SNAPSHOT:
            summary["stale"] = scenario.base_fingerprint != self.online_fingerprint()
        else:
            summary["stale"] = False
        return summary
