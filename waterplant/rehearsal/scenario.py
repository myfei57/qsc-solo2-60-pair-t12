"""Rehearsal scenario: the immutable input one offline run is defined by.

A scenario fixes two semantics up front so a drill can never drift:

* ``mode`` decides the data source by drill purpose: ``snapshot`` pins the
  live readings captured at creation, ``synthetic`` runs constructed boundary
  conditions carried inside the scenario itself.
* ``failure_policy`` decides what a mid-run step failure means: ``resume``
  keeps the partial progress and continues from the breakpoint, ``restart``
  invalidates the whole round and the next attempt starts from step zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from waterplant.filter import validate_zone

from .canonical import stable_hash

MODE_SNAPSHOT = "snapshot"
MODE_SYNTHETIC = "synthetic"
MODES = (MODE_SNAPSHOT, MODE_SYNTHETIC)

POLICY_RESUME = "resume"
POLICY_RESTART = "restart"
POLICIES = (POLICY_RESUME, POLICY_RESTART)

MAX_OPERATIONS = 500


@dataclass(frozen=True)
class Operation:
    """One named action plus the parameters the executor will see."""

    op: str
    params: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {"op": self.op, "params": self.params}


@dataclass(frozen=True)
class Scenario:
    """A validated, hashable rehearsal input."""

    name: str
    mode: str
    failure_policy: str
    operations: tuple[Operation, ...]
    initial: dict[str, object] | None

    @property
    def scenario_id(self) -> str:
        """Content hash of everything that influences the outcome."""

        return stable_hash(
            {
                "mode": self.mode,
                "failure_policy": self.failure_policy,
                "initial": self.initial,
                "operations": [operation.as_dict() for operation in self.operations],
            }
        )


def _require_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"scenario field {field_name} must be a non-empty string")
    return value


def _parse_bed(item: object, index: int) -> dict[str, object]:
    if not isinstance(item, dict):
        raise ValueError(f"initial bed {index} must be an object")
    bed_id = _require_str(item.get("id"), f"initial.beds[{index}].id")
    zone_raw = item.get("zone", 0)
    if isinstance(zone_raw, bool) or not isinstance(zone_raw, int):
        raise ValueError(f"initial bed {bed_id} zone must be an integer")
    validate_zone(zone_raw)
    load_raw = item.get("load", 0.0)
    if isinstance(load_raw, bool) or not isinstance(load_raw, (int, float)):
        raise ValueError(f"initial bed {bed_id} load must be a number")
    if load_raw < 0:
        raise ValueError(f"initial bed {bed_id} load must be non-negative")
    return {
        "id": bed_id,
        "zone": zone_raw,
        "load": float(load_raw),
        "closed": bool(item.get("closed", False)),
        "duty": bool(item.get("duty", False)),
    }


def _parse_initial(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("scenario initial must be an object")
    store_raw = value.get("store", {})
    if not isinstance(store_raw, dict):
        raise ValueError("scenario initial.store must be an object")
    store = {str(key): str(val) for key, val in store_raw.items()}
    beds_raw = value.get("beds", [])
    if not isinstance(beds_raw, list):
        raise ValueError("scenario initial.beds must be a list")
    beds = [_parse_bed(item, index) for index, item in enumerate(beds_raw)]
    bed_ids = [bed["id"] for bed in beds]
    if len(set(bed_ids)) != len(bed_ids):
        raise ValueError("scenario initial.beds must not repeat a bed id")
    return {"store": store, "beds": beds}


def _parse_operation(item: object, index: int, known_ops: set[str]) -> Operation:
    if not isinstance(item, dict):
        raise ValueError(f"operation {index} must be an object")
    op = _require_str(item.get("op"), f"operations[{index}].op")
    if op not in known_ops:
        raise ValueError(f"operation {index} names unknown op {op!r}")
    params = item.get("params", {})
    if not isinstance(params, dict):
        raise ValueError(f"operation {index} params must be an object")
    return Operation(op=op, params=dict(params))


def parse_scenario(payload: object, known_ops: set[str]) -> Scenario:
    """Validate a raw payload into a :class:`Scenario` or raise ValueError."""

    if not isinstance(payload, dict):
        raise ValueError("scenario must be a JSON object")

    mode = _require_str(payload.get("mode"), "mode")
    if mode not in MODES:
        raise ValueError(f"scenario mode must be one of {', '.join(MODES)}")

    failure_policy = _require_str(payload.get("failure_policy"), "failure_policy")
    if failure_policy not in POLICIES:
        raise ValueError(f"scenario failure_policy must be one of {', '.join(POLICIES)}")

    initial_raw = payload.get("initial")
    if mode == MODE_SYNTHETIC:
        if initial_raw is None:
            raise ValueError("synthetic scenario must carry an initial world")
        initial = _parse_initial(initial_raw)
    else:
        if initial_raw is not None:
            raise ValueError("snapshot scenario must not carry an initial world")
        initial = None

    operations_raw = payload.get("operations")
    if not isinstance(operations_raw, list) or not operations_raw:
        raise ValueError("scenario operations must be a non-empty list")
    if len(operations_raw) > MAX_OPERATIONS:
        raise ValueError(f"scenario operations exceed the limit of {MAX_OPERATIONS}")
    operations = tuple(
        _parse_operation(item, index, known_ops) for index, item in enumerate(operations_raw)
    )

    name = payload.get("name", "")
    if name is not None and not isinstance(name, str):
        raise ValueError("scenario name must be a string")

    return Scenario(
        name=name or "",
        mode=mode,
        failure_policy=failure_policy,
        operations=operations,
        initial=initial,
    )


def parse_patch(payload: object, known_ops: set[str]) -> dict[str, object]:
    """Validate a breakpoint patch: a replacement operation for one index."""

    if not isinstance(payload, dict):
        raise ValueError("patch must be an object")
    index_raw = payload.get("index")
    if isinstance(index_raw, bool) or not isinstance(index_raw, int) or index_raw < 0:
        raise ValueError("patch index must be a non-negative integer")
    operation = _parse_operation(payload, 0, known_ops)
    return {"index": index_raw, "op": operation.op, "params": operation.params}
