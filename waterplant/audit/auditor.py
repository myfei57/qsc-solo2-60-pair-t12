"""Append only audit entries for every chemical dose."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

from .report import AuditState

AUDIT_KEY = "audit:entries"


def _wall_clock() -> int:
    return int(time.time())


def _uuid_ids() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class Entry:
    """One recorded dosing decision."""

    id: str
    time: str
    kind: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "time": self.time, "kind": self.kind, "detail": self.detail}


class Auditor:
    """Records and queries dosing entries.

    The clock and id source default to the wall clock and random uuids.
    Offline rehearsals inject deterministic substitutes so a replayed run
    produces byte identical entries.
    """

    def __init__(
        self,
        store: Store,
        clock: Callable[[], int] | None = None,
        ids: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or _wall_clock
        self._ids = ids or _uuid_ids

    def record(self, kind: str, detail: str) -> Entry:
        entry = Entry(
            id=self._ids(),
            time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock())),
            kind=kind,
            detail=detail,
        )
        append_command(self._store, AUDIT_KEY, json.dumps(entry.as_dict(), ensure_ascii=False))
        return entry

    def entries(self) -> list[Entry]:
        entries: list[Entry] = []
        for item in load_commands(self._store, AUDIT_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            entries.append(
                Entry(
                    id=str(payload.get("id", "")),
                    time=str(payload.get("time", "")),
                    kind=str(payload.get("kind", "")),
                    detail=str(payload.get("detail", "")),
                )
            )
        return entries

    def last(self) -> Entry | None:
        entries = self.entries()
        if not entries:
            return None
        return entries[-1]

    def count(self) -> int:
        return len(self.entries())

    def count_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries():
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        return counts

    def filter(self, kind: str) -> list[Entry]:
        return [entry for entry in self.entries() if entry.kind == kind]

    def kinds(self) -> list[str]:
        seen: list[str] = []
        for entry in self.entries():
            if entry.kind not in seen:
                seen.append(entry.kind)
        return seen

    def totals(self) -> dict[str, float]:
        """Sum the numeric dose recorded for each chemical."""

        totals: dict[str, float] = {}
        for entry in self.entries():
            try:
                amount = float(entry.detail)
            except ValueError:
                continue
            totals[entry.kind] = totals.get(entry.kind, 0.0) + amount
        return totals

    def state(self) -> AuditState:
        entries = self.entries()
        return AuditState(entries=[entry.as_dict() for entry in entries], count=len(entries))

    def describe(self) -> str:
        return f"audit entries={len(self.entries())}"
