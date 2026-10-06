"""Wiring of every control component behind the console."""

from __future__ import annotations

from typing import Callable

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


class Runtime:
    """Owns one instance of each control component for a single store.

    The auditor and the unix clock are injectable so an offline rehearsal can
    run the exact same component wiring against a deterministic logical clock.
    """

    def __init__(
        self,
        store: Store,
        auditor: Auditor | None = None,
        unix_clock: "Callable[[], int] | None" = None,
        bank: Bank | None = None,
    ) -> None:
        auditor = auditor if auditor is not None else Auditor(store)
        bank = bank if bank is not None else Bank()
        coag_doser = CoagDoser(store, auditor=auditor)
        self.store = store
        self.flow_repository = FlowRepository(store)
        self.inlet = InletController()
        self.outlet = InletController()
        self.coag_doser = coag_doser
        self.chlor_doser = ChlorDoser(store, auditor=auditor)
        self.bank = bank
        self.backwash = Controller(bank, store, clock=unix_clock)
        self.sampler = Sampler(coag_doser)
        self.calibration = Calibration(store)
        self.well = Well(store)
        self.accumulator = Accumulator(store)
        self.auditor = auditor
        self.stabilizer = Stabilizer(store)
        self.scheduler = Scheduler(store)
        self.trend = Trend(store)
        self.inventory = Inventory(store)
