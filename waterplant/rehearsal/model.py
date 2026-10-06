"""Offline rehearsal scenarios and run records.

A scenario is the immutable input to an offline rehearsal: an initial state
plus a fixed list of console operations. A run record is the checkpointed
result of executing a scenario against a forked store. Everything here
serialises to plain JSON so the rehearsal root store can persist it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

SOURCE_SNAPSHOT = "snapshot"
SOURCE_CONSTRUCTED = "constructed"
SOURCES = (SOURCE_SNAPSHOT, SOURCE_CONSTRUCTED)

ON_FAILURE_PAUSE = "pause"
ON_FAILURE_VOID = "void"
ON_FAILURE_POLICIES = (ON_FAILURE_PAUSE, ON_FAILURE_VOID)

STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_VOID = "void"

MAX_OPS = 200
DENIED_PATH_PREFIX = "/rehearsal"


def canonical(payload: object) -> str:
    """Stable text form used for hashing and digests."""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(payload: object) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Op:
    """One console operation executed as a rehearsal step."""

    method: str
    path: str
    payload: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {"method": self.method, "path": self.path, "payload": dict(self.payload)}

    @classmethod
    def from_dict(cls, raw: object, index: int) -> "Op":
        if not isinstance(raw, dict):
            raise ValueError(f"op {index} must be an object")
        method = str(raw.get("method", "")).upper()
        if method not in ("GET", "POST"):
            raise ValueError(f"op {index} method must be GET or POST")
        path = str(raw.get("path", ""))
        if not path.startswith("/"):
            raise ValueError(f"op {index} path must start with /")
        if path.startswith(DENIED_PATH_PREFIX):
            raise ValueError(f"op {index} path {path} cannot re-enter the rehearsal API")
        payload = raw.get("payload", {})
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValueError(f"op {index} payload must be an object")
        return cls(method=method, path=path, payload=dict(payload))


@dataclass(frozen=True)
class Scenario:
    """The frozen input to one or more rehearsal runs."""

    scenario_id: str
    name: str
    purpose: str
    source: str
    on_failure: str
    seed: bool
    base: dict[str, object]
    ops: list[Op]
    base_fingerprint: str

    def as_dict(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "purpose": self.purpose,
            "source": self.source,
            "on_failure": self.on_failure,
            "seed": self.seed,
            "base": self.base,
            "ops": [op.as_dict() for op in self.ops],
            "base_fingerprint": self.base_fingerprint,
        }

    def summary(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "purpose": self.purpose,
            "source": self.source,
            "on_failure": self.on_failure,
            "ops": len(self.ops),
            "base_fingerprint": self.base_fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "Scenario":
        ops = [Op.from_dict(item, index) for index, item in enumerate(raw.get("ops", []))]
        return cls(
            scenario_id=str(raw.get("scenario_id", "")),
            name=str(raw.get("name", "")),
            purpose=str(raw.get("purpose", "")),
            source=str(raw.get("source", "")),
            on_failure=str(raw.get("on_failure", "")),
            seed=bool(raw.get("seed", True)),
            base=dict(raw.get("base", {})),
            ops=ops,
            base_fingerprint=str(raw.get("base_fingerprint", "")),
        )


def scenario_identity(source: str, on_failure: str, seed: bool, base: dict, ops: list[Op]) -> str:
    """Hash the parts of a scenario that determine its results.

    Name and purpose are labels only: two drills with the same base, policy
    and operations are the same input and must share a scenario id so repeat
    submissions stay idempotent.
    """

    identity = {
        "source": source,
        "on_failure": on_failure,
        "seed": seed,
        "base": base,
        "ops": [op.as_dict() for op in ops],
    }
    return content_hash(identity)


@dataclass(frozen=True)
class StepRecord:
    """One executed step: the op, its decision basis and the outcome."""

    index: int
    op: dict[str, object]
    basis: dict[str, object]
    status: int
    response: object
    error: str

    @property
    def failed(self) -> bool:
        return self.status >= 400

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "op": self.op,
            "basis": self.basis,
            "status": self.status,
            "response": self.response,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "StepRecord":
        return cls(
            index=int(raw.get("index", 0)),
            op=dict(raw.get("op", {})),
            basis=dict(raw.get("basis", {})),
            status=int(raw.get("status", 0)),
            response=raw.get("response"),
            error=str(raw.get("error", "")),
        )


@dataclass
class RunRecord:
    """Checkpointed state of one rehearsal run.

    The record is persisted after every step. Together with the working store
    (which persists itself on every write) it is enough to resume a paused or
    crashed run and to replay it from the frozen base.
    """

    run_id: str
    scenario_id: str
    status: str
    source: str
    on_failure: str
    steps_total: int
    next_index: int = 0
    pending_step: int = -1
    steps: list[StepRecord] = field(default_factory=list)
    beds: list[dict[str, object]] = field(default_factory=list)
    clock_tick: int = 0
    id_seq: int = 0
    base_audit_entries: int = 0
    audit_entries: int = 0
    digest: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "status": self.status,
            "source": self.source,
            "on_failure": self.on_failure,
            "steps_total": self.steps_total,
            "next_index": self.next_index,
            "pending_step": self.pending_step,
            "steps": [step.as_dict() for step in self.steps],
            "beds": self.beds,
            "clock_tick": self.clock_tick,
            "id_seq": self.id_seq,
            "base_audit_entries": self.base_audit_entries,
            "audit_entries": self.audit_entries,
            "digest": self.digest,
            "error": self.error,
        }

    def summary(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "status": self.status,
            "source": self.source,
            "on_failure": self.on_failure,
            "steps_completed": len(self.steps),
            "steps_total": self.steps_total,
            "next_index": self.next_index,
            "audit_entries": self.audit_entries,
            "digest": self.digest,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "RunRecord":
        return cls(
            run_id=str(raw.get("run_id", "")),
            scenario_id=str(raw.get("scenario_id", "")),
            status=str(raw.get("status", "")),
            source=str(raw.get("source", "")),
            on_failure=str(raw.get("on_failure", "")),
            steps_total=int(raw.get("steps_total", 0)),
            next_index=int(raw.get("next_index", 0)),
            pending_step=int(raw.get("pending_step", -1)),
            steps=[StepRecord.from_dict(item) for item in raw.get("steps", [])],
            beds=[dict(bed) for bed in raw.get("beds", [])],
            clock_tick=int(raw.get("clock_tick", 0)),
            id_seq=int(raw.get("id_seq", 0)),
            base_audit_entries=int(raw.get("base_audit_entries", 0)),
            audit_entries=int(raw.get("audit_entries", 0)),
            digest=str(raw.get("digest", "")),
            error=str(raw.get("error", "")),
        )


def run_id_for(scenario_id: str) -> str:
    """One scenario maps to exactly one run, which keeps re-submission idempotent."""

    suffix = scenario_id[3:] if scenario_id.startswith("sc-") else scenario_id
    return f"rn-{suffix}"


def validate_base(raw: object) -> dict[str, object]:
    """Normalise a scenario base: a store document plus filter beds."""

    if raw is None:
        return {"store": {}, "beds": []}
    if not isinstance(raw, dict):
        raise ValueError("base must be an object with store and beds")
    store = raw.get("store", {})
    if not isinstance(store, dict):
        raise ValueError("base.store must be an object")
    store_doc = {str(key): str(value) for key, value in store.items()}
    beds_raw = raw.get("beds", [])
    if not isinstance(beds_raw, list):
        raise ValueError("base.beds must be a list")
    beds: list[dict[str, object]] = []
    for index, item in enumerate(beds_raw):
        if not isinstance(item, dict):
            raise ValueError(f"base.beds {index} must be an object")
        bed_id = str(item.get("id", ""))
        if not bed_id:
            raise ValueError(f"base.beds {index} id is required")
        try:
            zone = int(item.get("zone", 0))
            load = float(item.get("load", 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"base.beds {index} zone and load must be numbers") from exc
        beds.append(
            {
                "id": bed_id,
                "zone": zone,
                "load": load,
                "closed": bool(item.get("closed", False)),
                "duty": bool(item.get("duty", False)),
            }
        )
    return {"store": store_doc, "beds": beds}
