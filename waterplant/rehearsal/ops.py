"""Operation executors: apply one rehearsal operation to a sandbox runtime.

Every executor returns ``(result, basis)`` where *basis* records the readings
the decision was derived from — flow, ratios, targets, bed loads — so a replay
can show exactly why each step did what it did. Executors reuse the same
domain validators as the live console, and any validation error aborts the
step (a partial failure of the run), never the process.
"""

from __future__ import annotations

from typing import Callable

from waterplant.chlor import validate_demand
from waterplant.clearwell import validate_level
from waterplant.filter import validate_zone
from waterplant.flow import calibrate_meter, validate_factor
from waterplant.intake import Sensor, mix, validate_flow
from waterplant.inventory import validate_quantity
from waterplant.ph import PH_BAND_HIGH, PH_BAND_LOW, validate_ph
from waterplant.quota import validate_amount
from waterplant.runtime import Runtime
from waterplant.scheduler import validate_threshold

Executor = Callable[[Runtime, dict[str, object]], tuple[dict[str, object], dict[str, object]]]


def _f(params: dict[str, object], name: str, default: float = 0.0) -> float:
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"field {name} must be a number")
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"field {name} must be a number") from exc


def _i(params: dict[str, object], name: str, default: int = 0) -> int:
    value = params.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"field {name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(f"field {name} must be an integer") from exc
    raise ValueError(f"field {name} must be an integer")


def _s(params: dict[str, object], name: str, default: str = "") -> str:
    value = params.get(name, default)
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


def _fl(params: dict[str, object], name: str) -> list[float]:
    value = params.get(name, [])
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"field {name} must be a list of numbers")
    return [_f({name: item}, name) for item in value]


def _bed_loads(rt: Runtime) -> dict[str, float]:
    return {bed.id: bed.load for bed in rt.bank.beds()}


def _cycle(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    """Walk the whole treatment line once, mirroring the live /cycle route."""

    flow = _f(params, "flow")
    samples = _fl(params, "samples")
    demand = _f(params, "demand")
    level = _f(params, "level")
    bed_id = _s(params, "bed_id")
    zone = _i(params, "zone")
    amount = _f(params, "amount")

    validate_flow(flow)
    validate_factor(rt.calibration.current())
    validate_zone(zone)
    validate_amount(amount)
    validate_level(level)

    ratio = rt.calibration.current()
    mixed = mix(samples)
    quota_before = rt.accumulator.value()
    bed_loads = _bed_loads(rt)

    rt.flow_repository.record(Sensor(flow=flow, turbidity=mixed))
    rt.trend.record(flow)
    coag_dose = rt.coag_doser.update_flow_and_dose(flow)
    turb_dose = rt.sampler.judge(samples)
    ph_verdict = rt.stabilizer.stabilize()
    rt.well.update_residual_demand(demand)
    chlor_dose = rt.chlor_doser.apply_residual() if ph_verdict.stable else 0.0
    min_level = rt.well.adjust_level(level, rt.inlet, rt.outlet)
    rotation = rt.backwash.order_rotation()
    duty = rt.bank.on_duty()
    if bed_id:
        rt.backwash.start(bed_id)
    quota_value = rt.accumulator.add(amount)

    result = {
        "coag_dose": coag_dose,
        "turb_dose": turb_dose,
        "chlor_dose": chlor_dose,
        "min_level": min_level,
        "level": rt.well.level(),
        "quota": quota_value,
        "rotation": rotation,
        "on_duty": duty or "",
        "backwash": bed_id,
        "drains": rt.backwash.drain_count(),
        "audit_count": rt.auditor.count(),
        "ph_stable": ph_verdict.stable,
        "ph_adjustment": ph_verdict.adjustment,
    }
    basis = {
        "flow": flow,
        "mixed_turbidity": mixed,
        "calibration_ratio": ratio,
        "ph_value": ph_verdict.value,
        "ph_stable": ph_verdict.stable,
        "ph_direction": ph_verdict.direction,
        "residual_target": rt.well.residual_target(),
        "bed_loads": bed_loads,
        "rotation_rule": "dirtiest bed first",
        "quota_before": quota_before,
        "amount_added": amount,
    }
    return result, basis


def _intake_flow(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    sensor = Sensor(flow=_f(params, "flow"), turbidity=_f(params, "turbidity"))
    validate_flow(sensor.flow)
    rt.flow_repository.record(sensor)
    rt.trend.record(sensor.flow)
    return (
        {"flow": sensor.flow, "turbidity": sensor.turbidity},
        {"flow": sensor.flow, "turbidity": sensor.turbidity},
    )


def _coag_dose(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    flow = _f(params, "flow")
    validate_flow(flow)
    ratio = rt.coag_doser.current_ratio()
    dose = rt.coag_doser.update_flow_and_dose(flow)
    return {"dose": dose}, {"flow": flow, "calibration_ratio": ratio}


def _coag_turbidity(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    samples = _fl(params, "samples")
    mixed = mix(samples)
    ratio = rt.coag_doser.current_ratio()
    dose = rt.sampler.judge(samples)
    return {"dose": dose}, {"samples": samples, "mixed_turbidity": mixed, "calibration_ratio": ratio}


def _chlor_target(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    demand = _f(params, "demand")
    validate_demand(demand)
    target = rt.well.update_residual_demand(demand)
    return {"target": target}, {"demand": demand, "rule": "target = 0.3 + demand * 0.7"}


def _chlor_dose(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    target = rt.chlor_doser.current_target()
    dose = rt.chlor_doser.apply_residual()
    return {"dose": dose}, {"residual_target": target}


def _clearwell_level(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    target = _f(params, "target")
    validate_level(target)
    level_before = rt.well.level()
    minimum = rt.well.adjust_level(target, rt.inlet, rt.outlet)
    return (
        {"min_level": minimum, "level": rt.well.level()},
        {"level_before": level_before, "target": target},
    )


def _flow_replace(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    factor = _f(params, "factor")
    validate_factor(factor)
    factor_before = rt.calibration.current()
    meter = calibrate_meter(_s(params, "serial"), factor)
    rt.calibration.replace(meter.factor)
    return {"meter": meter.as_dict()}, {"factor_before": factor_before, "factor": meter.factor}


def _filter_add(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    zone = _i(params, "zone")
    validate_zone(zone)
    load = _f(params, "load")
    bed = rt.bank.add_bed(_s(params, "id"), zone, load)
    return (
        {"id": bed.id, "zone": bed.zone, "load": bed.load},
        {"bed_count_before": rt.bank.count() - 1},
    )


def _filter_close(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    bed_id = _s(params, "id")
    bed = rt.bank.bed(bed_id)
    closed_before = bed.closed if bed is not None else None
    rt.bank.close(bed_id)
    return {"closed": bed_id}, {"closed_before": closed_before}


def _filter_open(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    bed_id = _s(params, "id")
    bed = rt.bank.bed(bed_id)
    closed_before = bed.closed if bed is not None else None
    rt.bank.open(bed_id)
    return {"opened": bed_id}, {"closed_before": closed_before}


def _filter_remove(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    bed_id = _s(params, "id")
    existed = rt.bank.bed(bed_id) is not None
    rt.bank.remove_bed(bed_id)
    return {"removed": bed_id}, {"existed_before": existed}


def _filter_load(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    bed_id = _s(params, "id")
    load = _f(params, "load")
    if load < 0:
        raise ValueError("load must be non-negative")
    bed = rt.bank.bed(bed_id)
    load_before = bed.load if bed is not None else None
    rt.bank.set_load(bed_id, load)
    return {"id": bed_id, "load": load}, {"load_before": load_before}


def _filter_renumber(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    zone = _i(params, "zone")
    validate_zone(zone)
    bed_id = _s(params, "id")
    zone_before, present = rt.bank.bed_zone(bed_id)
    rt.bank.renumber(bed_id, zone)
    return {"renumbered": bed_id, "zone": zone}, {"zone_before": zone_before if present else None}


def _filter_reset(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    closed_before = [bed.id for bed in rt.bank.beds() if bed.closed]
    rt.bank.reset_closed()
    return {"reset": True}, {"closed_before": closed_before}


def _backwash_start(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    bed_id = _s(params, "id")
    bed = rt.bank.bed(bed_id)
    closed_before = bed.closed if bed is not None else None
    rt.backwash.start(bed_id)
    return {"started": bed_id}, {"closed_before": closed_before, "sequence": "close then drain"}


def _backwash_enqueue(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    bed_id = _s(params, "id")
    queue_before = rt.backwash.command_list()
    rt.backwash.enqueue(bed_id)
    return {"enqueued": bed_id}, {"queue_before": queue_before}


def _backwash_replay(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    queue_before = rt.backwash.command_list()
    replayed = rt.backwash.replay()
    return {"replayed": replayed}, {"queue_before": queue_before}


def _backwash_recover(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    queue_before = rt.backwash.command_list()
    rt.backwash.recover()
    return {"recovered": True}, {"queue_before": queue_before}


def _quota_add(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    amount = _f(params, "amount")
    validate_amount(amount)
    quota_before = rt.accumulator.value()
    value = rt.accumulator.add(amount)
    return {"value": value}, {"quota_before": quota_before, "amount": amount, "epoch_cap": 100.0}


def _ph_read(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    value = _f(params, "value")
    validate_ph(value)
    ph_before = rt.stabilizer.current()
    verdict = rt.stabilizer.read(value)
    return verdict.as_dict(), {"ph_before": ph_before, "band": [PH_BAND_LOW, PH_BAND_HIGH]}


def _schedule_threshold(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    value = _f(params, "threshold")
    validate_threshold(value)
    threshold_before = rt.scheduler.threshold()
    applied = rt.scheduler.set_threshold(value)
    return {"threshold": applied}, {"threshold_before": threshold_before}


def _inventory_receive(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    amount = _f(params, "amount")
    validate_quantity(amount)
    chemical = _s(params, "chemical")
    balance_before = rt.inventory.balance(chemical)
    lot = rt.inventory.receive(chemical, _s(params, "lot_id"), amount)
    return {"lot": lot.as_dict()}, {"balance_before": balance_before}


def _inventory_consume(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    amount = _f(params, "amount")
    validate_quantity(amount)
    chemical = _s(params, "chemical")
    balance_before = rt.inventory.balance(chemical)
    consumed = rt.inventory.consume(chemical, amount)
    return (
        {"consumed": consumed, "balance": rt.inventory.balance(chemical)},
        {"balance_before": balance_before, "rule": "oldest lot first"},
    )


def _inventory_reorder_level(rt: Runtime, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    level = _f(params, "level")
    validate_quantity(level, "level")
    level_before = rt.inventory.reorder_level()
    applied = rt.inventory.set_reorder_level(level)
    return {"reorder_level": applied}, {"level_before": level_before}


EXECUTORS: dict[str, Executor] = {
    "cycle": _cycle,
    "intake_flow": _intake_flow,
    "coag_dose": _coag_dose,
    "coag_turbidity": _coag_turbidity,
    "chlor_target": _chlor_target,
    "chlor_dose": _chlor_dose,
    "clearwell_level": _clearwell_level,
    "flow_replace": _flow_replace,
    "filter_add": _filter_add,
    "filter_close": _filter_close,
    "filter_open": _filter_open,
    "filter_remove": _filter_remove,
    "filter_load": _filter_load,
    "filter_renumber": _filter_renumber,
    "filter_reset": _filter_reset,
    "backwash_start": _backwash_start,
    "backwash_enqueue": _backwash_enqueue,
    "backwash_replay": _backwash_replay,
    "backwash_recover": _backwash_recover,
    "quota_add": _quota_add,
    "ph_read": _ph_read,
    "schedule_threshold": _schedule_threshold,
    "inventory_receive": _inventory_receive,
    "inventory_consume": _inventory_consume,
    "inventory_reorder_level": _inventory_reorder_level,
}


def known_ops() -> set[str]:
    return set(EXECUTORS)


def execute_op(rt: Runtime, op: str, params: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    executor = EXECUTORS.get(op)
    if executor is None:
        raise ValueError(f"unknown rehearsal op {op!r}")
    return executor(rt, params)
