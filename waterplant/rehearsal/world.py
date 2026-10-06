"""Rehearsal world state: capture, restore, hashing and the logical clock.

A *world* is everything a run can mutate: the sandbox store document plus the
filter bed registry. Worlds are plain JSON structures so they can be pinned
inside a run manifest, hashed for the step hash chain, and restored when a
failed run resumes from its checkpoint.

The logical clock persists its sequence inside the sandbox store itself, so a
world restored from a checkpoint keeps producing the exact timestamps and ids
an uninterrupted run would have produced.
"""

from __future__ import annotations

import time
from typing import Callable

from waterplant.filter import Bank, Bed
from waterplant.store import Store, export_state
from waterplant.store.epoch import load_float, save_float

from .canonical import stable_hash

CLOCK_KEY = "rehearsal:logical-clock"
LOGICAL_EPOCH = 1_700_000_000


class ReadOnlyStore:
    """View over the live store that makes rehearsal writes impossible.

    The rehearsal manager only ever sees the live state through this view,
    which is the hard boundary guaranteeing a drill cannot touch online data.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def get(self, key: str) -> tuple[str, bool]:
        return self._store.get(key)

    def keys(self) -> list[str]:
        return self._store.keys()

    def count(self) -> int:
        return self._store.count()

    def put(self, key: str, value: str) -> None:
        raise PermissionError("rehearsal must not write the live store")

    def delete(self, key: str) -> None:
        raise PermissionError("rehearsal must not write the live store")

    def clear(self) -> None:
        raise PermissionError("rehearsal must not write the live store")


def next_tick(store: Store) -> int:
    """Advance the logical clock persisted inside the sandbox store."""

    value, present = load_float(store, CLOCK_KEY)
    sequence = int(value) if present else 0
    save_float(store, CLOCK_KEY, float(sequence + 1))
    return LOGICAL_EPOCH + sequence


def logical_time(store: Store) -> str:
    """Deterministic replacement for the auditor's wall clock."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(next_tick(store)))


def logical_id(store: Store, run_id: str) -> str:
    """Deterministic replacement for the auditor's random id source."""

    return f"{run_id[:12]}-{next_tick(store)}"


def capture_world(store: Store, bank: Bank) -> dict[str, object]:
    """Snapshot the full mutable world into a plain JSON structure."""

    return {
        "store": export_state(store),
        "beds": [bed.snapshot() for bed in bank.beds()],
    }


def restore_world(world: dict[str, object]) -> tuple[Store, Bank]:
    """Rebuild a sandbox store and filter bank from a captured world."""

    store_raw = world.get("store", {})
    store = Store("", {str(key): str(value) for key, value in dict(store_raw).items()})
    bank = Bank()
    for item in list(world.get("beds", [])):
        entry = dict(item)
        bed = bank.add_bed(
            str(entry.get("id", "")),
            int(entry.get("zone", 0)),
            float(entry.get("load", 0.0)),
        )
        bed.closed = bool(entry.get("closed", False))
        bed.duty = bool(entry.get("duty", False))
    return store, bank


def world_hash(world: dict[str, object]) -> str:
    """Content hash of a world; identical worlds always hash identically."""

    return stable_hash(world)


def snapshot_live(live: ReadOnlyStore, bank: Bank) -> dict[str, object]:
    """Capture the live world read-only for a snapshot-mode run."""

    return {
        "store": {key: live.get(key)[0] for key in live.keys()},
        "beds": [bed.snapshot() for bed in bank.beds()],
    }
