"""Timestamped console events appended to the store."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .commands import append_command, load_commands
from .store import Store


@dataclass(frozen=True)
class Event:
    """One operator visible event."""

    at: int
    kind: str
    value: str

    def as_dict(self) -> dict[str, object]:
        return {"at": self.at, "kind": self.kind, "value": self.value}


def append_event(store: Store, key: str, kind: str, value: str, at: int | None = None) -> None:
    """Record one event under the supplied list key.

    ``at`` defaults to the wall clock; offline rehearsals pass a logical
    timestamp so a replayed run records identical events.
    """

    stamp = int(time.time()) if at is None else int(at)
    event = Event(at=stamp, kind=kind, value=value)
    append_command(store, key, json.dumps(event.as_dict(), ensure_ascii=False))


def list_events(store: Store, key: str) -> list[Event]:
    """Return every well formed event stored under ``key``."""

    events: list[Event] = []
    for item in load_commands(store, key):
        try:
            payload = json.loads(item)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        events.append(
            Event(
                at=int(payload.get("at", 0)),
                kind=str(payload.get("kind", "")),
                value=str(payload.get("value", "")),
            )
        )
    return events


def event_count(store: Store, key: str) -> int:
    """Count the raw event records stored under ``key``."""

    return len(load_commands(store, key))
