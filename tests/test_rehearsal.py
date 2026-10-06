"""End to end tests for the offline rehearsal engine."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from waterplant.console.seed import seed_defaults
from waterplant.console.server import Server
from waterplant.store import Store


def cycle_op(flow=800.0, amount=5.0, bed_id="b1"):
    return {
        "method": "POST",
        "path": "/cycle",
        "payload": {
            "flow": flow,
            "samples": [2.0, 4.0],
            "demand": 0.5,
            "level": 10.0,
            "bed_id": bed_id,
            "zone": 1,
            "amount": amount,
        },
    }


def constructed_spec(ops, on_failure="pause", name="boundary-drill"):
    return {
        "name": name,
        "purpose": "new operator training on boundary conditions",
        "source": "constructed",
        "on_failure": on_failure,
        "seed": True,
        "base": {
            "store": {"quota:chlorine-accumulator": "90.0000"},
            "beds": [
                {"id": "b1", "zone": 1, "load": 9.0},
                {"id": "b2", "zone": 2, "load": 1.0},
            ],
        },
        "ops": ops,
    }


class RehearsalCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        seed_defaults(self.store)
        self.server = Server(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None):
        response = self.server.dispatch(method, path, payload)
        body = response.body.decode("utf-8")
        parsed = json.loads(body) if response.content_type.startswith("application/json") else body
        return response.status, parsed

    def create_scenario(self, spec):
        status, body = self.call("POST", "/rehearsal/scenario", spec)
        self.assertEqual(status, 200, body)
        return body

    def start_run(self, scenario_id):
        status, body = self.call("POST", "/rehearsal/run", {"scenario_id": scenario_id})
        self.assertEqual(status, 200, body)
        return body

    def test_constructed_run_is_deterministic_and_idempotent(self) -> None:
        spec = constructed_spec(
            [
                cycle_op(),
                {"method": "POST", "path": "/coag/dose", "payload": {"flow": 1500.0}},
                {"method": "GET", "path": "/quota", "payload": {}},
            ]
        )
        created = self.create_scenario(spec)
        self.assertTrue(created["created"])
        scenario_id = created["scenario"]["scenario_id"]
        self.assertTrue(scenario_id.startswith("sc-"))
        self.assertEqual(created["scenario"]["on_failure"], "pause")

        again = self.create_scenario(spec)
        self.assertFalse(again["created"])
        self.assertEqual(again["scenario"]["scenario_id"], scenario_id)

        first = self.start_run(scenario_id)
        self.assertTrue(first["started"])
        self.assertEqual(first["run"]["status"], "completed")
        self.assertEqual(first["run"]["steps_completed"], 3)
        digest = first["run"]["digest"]
        self.assertTrue(digest)

        repeat = self.start_run(scenario_id)
        self.assertFalse(repeat["started"])
        self.assertEqual(repeat["run"]["digest"], digest)

        status, replay = self.call("POST", "/rehearsal/replay", {"run_id": first["run"]["run_id"]})
        self.assertEqual(status, 200)
        self.assertTrue(replay["consistent"])
        self.assertEqual(replay["digest"], digest)
        self.assertEqual(replay["steps_checked"], 3)

    def test_decision_basis_is_recorded_for_every_step(self) -> None:
        created = self.create_scenario(constructed_spec([cycle_op()]))
        run = self.start_run(created["scenario"]["scenario_id"])

        status, body = self.call("POST", "/rehearsal/run/steps", {"run_id": run["run"]["run_id"]})
        self.assertEqual(status, 200)
        steps = body["run"]["steps"]
        self.assertEqual(len(steps), 1)

        basis = steps[0]["basis"]
        self.assertEqual(basis["ratio"], 1.0)
        self.assertEqual(basis["flow"], 1.0)
        self.assertEqual(basis["quota_remaining"], 10.0)
        self.assertEqual([bed["id"] for bed in basis["bank"]["beds"]], ["b1", "b2"])
        self.assertTrue(basis["ph"]["stable"])

        response = steps[0]["response"]
        self.assertEqual(steps[0]["status"], 200)
        self.assertEqual(response["coag_dose"], 800.0)
        self.assertEqual(response["turb_dose"], 3.0)
        self.assertEqual(response["chlor_dose"], 0.65)
        self.assertEqual(response["quota"], 95.0)

    def test_snapshot_run_never_touches_online_state(self) -> None:
        self.call("POST", "/filter/add", {"id": "live1", "zone": 1, "load": 4.0})
        self.call("POST", "/coag/dose", {"flow": 5.0})
        online_before = {key: self.store.get(key)[0] for key in self.store.keys()}
        with open(self.store.path, "rb") as handle:
            file_before = handle.read()

        created = self.create_scenario(
            {
                "name": "live-review",
                "purpose": "post incident review",
                "source": "snapshot",
                "on_failure": "pause",
                "ops": [
                    cycle_op(flow=600.0, amount=3.0, bed_id="live1"),
                    {"method": "POST", "path": "/quota/add", "payload": {"amount": 2.0}},
                ],
            }
        )
        scenario = created["scenario"]
        self.assertEqual(scenario["source"], "snapshot")
        self.assertIn("live1", [bed["id"] for bed in scenario["base"]["beds"]])

        run = self.start_run(scenario["scenario_id"])
        self.assertEqual(run["run"]["status"], "completed")

        status, replay = self.call("POST", "/rehearsal/replay", {"run_id": run["run"]["run_id"]})
        self.assertTrue(replay["consistent"])

        online_after = {key: self.store.get(key)[0] for key in self.store.keys()}
        self.assertEqual(online_before, online_after)
        with open(self.store.path, "rb") as handle:
            self.assertEqual(file_before, handle.read())
        self.assertEqual(self.server.runtime.bank.bed_ids(), ["live1"])
        self.assertEqual(self.server.runtime.auditor.count(), 1)

        status, report = self.call("GET", "/rehearsal/report")
        self.assertEqual(status, 200)
        self.assertTrue(report["ok"])
        self.assertFalse(report["scenarios"][0]["stale"])

        self.call("POST", "/quota/add", {"amount": 1.0})
        _, report = self.call("GET", "/rehearsal/report")
        self.assertTrue(report["scenarios"][0]["stale"])

    def test_pause_policy_resumes_from_the_failed_step(self) -> None:
        created = self.create_scenario(
            constructed_spec(
                [
                    cycle_op(),
                    {"method": "POST", "path": "/filter/close", "payload": {"id": "ghost"}},
                    {"method": "GET", "path": "/quota", "payload": {}},
                ]
            )
        )
        scenario_id = created["scenario"]["scenario_id"]
        run = self.start_run(scenario_id)
        self.assertEqual(run["run"]["status"], "paused")
        self.assertEqual(run["run"]["steps_completed"], 2)
        self.assertEqual(run["run"]["next_index"], 2)
        self.assertIn("step 1 failed", run["run"]["error"])

        status, body = self.call("POST", "/rehearsal/run/steps", {"run_id": run["run"]["run_id"]})
        failed = body["run"]["steps"][1]
        self.assertEqual(failed["status"], 400)
        self.assertIn("not found", failed["error"])

        status, resumed = self.call("POST", "/rehearsal/resume", {"run_id": run["run"]["run_id"]})
        self.assertEqual(status, 200)
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["run"]["status"], "completed")
        self.assertEqual(resumed["run"]["steps_completed"], 3)

        again = self.call("POST", "/rehearsal/resume", {"run_id": run["run"]["run_id"]})[1]
        self.assertFalse(again["resumed"])

        status, replay = self.call("POST", "/rehearsal/replay", {"run_id": run["run"]["run_id"]})
        self.assertTrue(replay["consistent"])
        self.assertEqual(replay["steps_checked"], 3)

    def test_void_policy_invalidates_the_whole_run(self) -> None:
        created = self.create_scenario(
            constructed_spec(
                [
                    cycle_op(),
                    {"method": "POST", "path": "/filter/close", "payload": {"id": "ghost"}},
                    {"method": "GET", "path": "/quota", "payload": {}},
                ],
                on_failure="void",
            )
        )
        run = self.start_run(created["scenario"]["scenario_id"])
        self.assertEqual(run["run"]["status"], "void")

        status, body = self.call("POST", "/rehearsal/resume", {"run_id": run["run"]["run_id"]})
        self.assertEqual(status, 400)
        self.assertIn("void", body["error"])

        status, replay = self.call("POST", "/rehearsal/replay", {"run_id": run["run"]["run_id"]})
        self.assertTrue(replay["consistent"])
        self.assertEqual(replay["steps_checked"], 2)

        _, report = self.call("GET", "/rehearsal/report")
        self.assertEqual(report["totals"]["void"], 1)
        self.assertEqual(report["ledger"]["by_kind"]["run_voided"], 1)

    def test_run_survives_a_process_restart(self) -> None:
        created = self.create_scenario(
            constructed_spec(
                [
                    cycle_op(),
                    {"method": "POST", "path": "/filter/close", "payload": {"id": "ghost"}},
                    {"method": "GET", "path": "/quota", "payload": {}},
                ]
            )
        )
        run = self.start_run(created["scenario"]["scenario_id"])
        self.assertEqual(run["run"]["status"], "paused")
        run_id = run["run"]["run_id"]

        restarted = Server(self.store)
        response = restarted.dispatch("POST", "/rehearsal/resume", {"run_id": run_id})
        body = json.loads(response.body)
        self.assertEqual(response.status, 200)
        self.assertTrue(body["resumed"])
        self.assertEqual(body["run"]["status"], "completed")

        response = restarted.dispatch("POST", "/rehearsal/replay", {"run_id": run_id})
        self.assertTrue(json.loads(response.body)["consistent"])

    def test_run_left_running_by_a_crash_can_be_resumed(self) -> None:
        created = self.create_scenario(
            constructed_spec([cycle_op(), cycle_op(flow=500.0, amount=3.0)])
        )
        scenario_id = created["scenario"]["scenario_id"]
        run_id = "rn-" + scenario_id[3:]

        engine = self.server.rehearsal()
        original_save = engine._save_run
        calls = {"count": 0}

        def crashing_save(record):
            # Crash after the second step's effects hit the working store but
            # before its checkpoint lands: saves are initial, pending, step0,
            # pending, step1(checkpoint) - the fifth never completes.
            calls["count"] += 1
            if calls["count"] == 5:
                raise RuntimeError("simulated crash after the second step")
            original_save(record)

        engine._save_run = crashing_save
        with self.assertRaises(RuntimeError):
            engine.start_run(scenario_id)

        record = engine.run_steps(run_id)
        self.assertEqual(record["status"], "running")
        self.assertEqual(record["pending_step"], 1)
        self.assertEqual(record["next_index"], 1)
        self.assertEqual(len(record["steps"]), 1)

        restarted = Server(self.store)
        response = restarted.dispatch("POST", "/rehearsal/resume", {"run_id": run_id})
        body = json.loads(response.body)
        self.assertEqual(response.status, 200)
        self.assertEqual(body["run"]["status"], "completed")
        self.assertEqual(body["run"]["steps_completed"], 2)

        work = Store.open(f"{self.store.path}.rehearsal/run-{run_id}.json")
        quota, present = work.get("quota:chlorine-accumulator")
        self.assertTrue(present)
        self.assertEqual(float(quota), 98.0)

        response = restarted.dispatch("POST", "/rehearsal/replay", {"run_id": run_id})
        self.assertTrue(json.loads(response.body)["consistent"])

        response = restarted.dispatch("GET", "/rehearsal/report")
        self.assertTrue(json.loads(response.body)["ok"])

    def test_report_reconciles_and_detects_tampering(self) -> None:
        created = self.create_scenario(constructed_spec([cycle_op()]))
        run = self.start_run(created["scenario"]["scenario_id"])
        run_id = run["run"]["run_id"]

        status, report = self.call("GET", "/rehearsal/report")
        self.assertEqual(status, 200)
        self.assertTrue(report["ok"])
        self.assertEqual(report["totals"]["completed"], 1)
        self.assertEqual(report["totals"]["ledger_events"], 3)
        checks = report["runs"][0]["checks"]
        self.assertTrue(checks["steps_match"])
        self.assertTrue(checks["audit_match"])
        self.assertTrue(checks["digest_match"])
        self.assertEqual(
            checks["ledger_trail"], ["run_started", "run_completed"]
        )

        work = Store.open(f"{self.store.path}.rehearsal/run-{run_id}.json")
        work.put("tampered", "true")
        _, report = self.call("GET", "/rehearsal/report")
        self.assertFalse(report["ok"])
        self.assertFalse(report["runs"][0]["checks"]["digest_match"])

    def test_scenario_validation(self) -> None:
        good_ops = [cycle_op()]
        for patch, message in (
            ({"source": ""}, "source"),
            ({"source": "snapshot", "on_failure": ""}, "on_failure"),
            ({"source": "constructed", "on_failure": "pause", "ops": []}, "ops"),
            (
                {
                    "source": "constructed",
                    "on_failure": "pause",
                    "ops": [{"method": "POST", "path": "/rehearsal/runs"}],
                },
                "rehearsal",
            ),
            (
                {
                    "source": "constructed",
                    "on_failure": "pause",
                    "ops": [{"method": "DELETE", "path": "/quota"}],
                },
                "method",
            ),
        ):
            spec = {"source": "constructed", "on_failure": "pause", "ops": good_ops}
            spec.update(patch)
            status, body = self.call("POST", "/rehearsal/scenario", spec)
            self.assertEqual(status, 400, spec)
            self.assertIn(message, body["error"])

        status, _ = self.call("POST", "/rehearsal/run", {"scenario_id": "sc-missing"})
        self.assertEqual(status, 404)
        status, _ = self.call("POST", "/rehearsal/resume", {"run_id": "rn-missing"})
        self.assertEqual(status, 404)
        status, _ = self.call("POST", "/rehearsal/replay", {"run_id": "rn-missing"})
        self.assertEqual(status, 404)

    def test_run_listing_and_scenario_listing(self) -> None:
        created = self.create_scenario(constructed_spec([cycle_op()]))
        scenario_id = created["scenario"]["scenario_id"]
        self.start_run(scenario_id)

        status, body = self.call("GET", "/rehearsal/scenarios")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["scenarios"]), 1)
        self.assertEqual(body["scenarios"][0]["scenario_id"], scenario_id)
        self.assertEqual(body["scenarios"][0]["purpose"], "new operator training on boundary conditions")

        status, body = self.call("GET", "/rehearsal/runs")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["runs"]), 1)
        self.assertEqual(body["runs"][0]["status"], "completed")
        self.assertEqual(body["runs"][0]["steps_total"], 1)


if __name__ == "__main__":
    unittest.main()
