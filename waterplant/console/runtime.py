"""Wiring of every control component behind the console."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from waterplant.audit import Auditor
from waterplant.backwash import Controller
from waterplant.chlor import Doser as ChlorDoser
from waterplant.clearwell import Well
from waterplant.coag import Doser as CoagDoser
from waterplant.filter import Bank
from waterplant.flow import Calibration
from waterplant.intake import FlowRepository, InletController, Trend
from waterplant.inventory import Inventory
from waterplant.ph import Stabilizer
from waterplant.quota import Accumulator
from waterplant.scheduler import Scheduler
from waterplant.store import Store
from waterplant.turb import Sampler


def _wall_clock() -> int:
    return int(time.time())


def _uuid_ids() -> str:
    return uuid.uuid4().hex


class Runtime:
    """Owns one instance of each control component for a single store.

    The clock and id source default to the wall clock and random uuids, which
    is what the live console wants. Offline rehearsals inject deterministic
    substitutes so the same inputs always produce the same persisted state.
    """

    def __init__(
        self,
        store: Store,
        clock: Callable[[], int] | None = None,
        ids: Callable[[], str] | None = None,
    ) -> None:
        self.clock = clock or _wall_clock
        self.ids = ids or _uuid_ids
        bank = Bank()
        coag_doser = CoagDoser(store, clock=self.clock, ids=self.ids)
        self.store = store
        self.flow_repository = FlowRepository(store)
        self.inlet = InletController()
        self.outlet = InletController()
        self.coag_doser = coag_doser
        self.chlor_doser = ChlorDoser(store, clock=self.clock, ids=self.ids)
        self.bank = bank
        self.backwash = Controller(bank, store, clock=self.clock)
        self.sampler = Sampler(coag_doser)
        self.calibration = Calibration(store)
        self.well = Well(store)
        self.accumulator = Accumulator(store)
        self.auditor = Auditor(store, clock=self.clock, ids=self.ids)
        self.stabilizer = Stabilizer(store)
        self.scheduler = Scheduler(store)
        self.trend = Trend(store)
        self.inventory = Inventory(store)
