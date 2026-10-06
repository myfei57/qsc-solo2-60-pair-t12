"""End to end tests for the offline rehearsal subsystem."""

from __future__ import annotations

import json
import tempfile
import unittest

from waterplant.console.seed import seed_defaults
from waterplant.console.server import Server
from waterplant.store import Store


def synthetic_scenario(**overrides) -> dict:
    scenario = {
        "name": "boundary-drill",
        "mode": "synthetic",
        "failure_policy": "resume",
        "initial": {
            "store": {"flow:calibration": "1.5000", "ph:value": "7.2000"},
            "beds": [
                {"id": "b1", "zone": 1, "load": 9.0},
                {"id": "b2", "zone": 2, "load": 2.0},
            ],
        },
        "operations": [
            {
                "op": "cycle",
                "params": {
                    "flow": 800.0,
                    "samples": [2.0, 4.0],
                    "demand": 0.5,
                    "level": 10.0,
                    "bed_id": "b1",
                    "zone": 1,
                    "amount": 12.0,
                },
            },
            {"op": "quota_add", "params": {"amount": 5.0}},
        ],
    }
    scenario.update(overrides)
    return scenario


def failing_scenario(policy: str = "resume") -> dict:
    return {
        "mode": "synthetic",
        "failure_policy": policy,
        "initial": {"store": {}, "beds": [{"id": "b1", "zone": 1}]},
        "operations": [
            {"op": "quota_add", "params": {"amount": 5.0}},
            {"op": "filter_close", "params": {"id": "ghost"}},
            {"op": "quota_add", "params": {"amount": 7.0}},
        ],
    }


def patched_scenario() -> dict:
    scenario = failing_scenario()
    scenario["operations"][1] = {"op": "filter_close", "params": {"id": "b1"}}
    return scenario


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

    def run_scenario(self, scenario: dict):
        status, body = self.call("POST", "/rehearsal/run", scenario)
        self.assertEqual(status, 200, body)
        return body

    def test_synthetic_run_completes_and_reconciles(self) -> None:
        report = self.run_scenario(synthetic_scenario())
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["steps_ok"], 2)
        self.assertEqual(report["steps_failed"], 0)
        self.assertTrue(report["reconciled"])
        self.assertTrue(report["final_hash"])

        audit = report["audit"]
        self.assertEqual(audit["by_kind"]["run_created"], 1)
        self.assertEqual(audit["by_kind"]["step_ok"], 2)
        self.assertEqual(audit["by_kind"]["run_completed"], 1)
        self.assertEqual(audit["entries"], 4)

        status, listed = self.call("GET", "/rehearsal")
        self.assertEqual(status, 200)
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["runs"][0]["run_id"], report["run_id"])

    def test_same_input_is_idempotent(self) -> None:
        first = self.run_scenario(synthetic_scenario())
        second = self.run_scenario(synthetic_scenario())
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertTrue(second["idempotent"])
        self.assertFalse(second["executed"])
        self.assertEqual(first["final_hash"], second["final_hash"])

        status, audit = self.call("POST", "/rehearsal/audit", {"run_id": first["run_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(audit["count"], first["audit"]["entries"])

    def test_verify_replays_consistently(self) -> None:
        report = self.run_scenario(synthetic_scenario())
        status, verify = self.call("POST", "/rehearsal/verify", {"run_id": report["run_id"]})
        self.assertEqual(status, 200)
        self.assertTrue(verify["consistent"], verify)
        self.assertEqual(verify["steps_checked"], 2)
        self.assertEqual(verify["recorded_final_hash"], verify["recomputed_final_hash"])

    def test_replay_exposes_decision_basis(self) -> None:
        report = self.run_scenario(synthetic_scenario())
        status, replay = self.call("POST", "/rehearsal/replay", {"run_id": report["run_id"]})
        self.assertEqual(status, 200)
        self.assertTrue(replay["hash_chain_valid"])
        self.assertEqual(len(replay["steps"]), 2)

        cycle_step = replay["steps"][0]
        self.assertEqual(cycle_step["op"], "cycle")
        self.assertEqual(cycle_step["basis"]["calibration_ratio"], 1.5)
        self.assertEqual(cycle_step["basis"]["flow"], 800.0)
        self.assertEqual(cycle_step["basis"]["bed_loads"], {"b1": 9.0, "b2": 2.0})
        self.assertEqual(cycle_step["result"]["coag_dose"], 1200.0)

        quota_step = replay["steps"][1]
        self.assertEqual(quota_step["basis"]["quota_before"], 12.0)
        self.assertEqual(quota_step["result"]["value"], 17.0)
        self.assertEqual(cycle_step["after_hash"], quota_step["before_hash"])

    def test_partial_failure_resumes_from_breakpoint(self) -> None:
        report = self.run_scenario(failing_scenario())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["steps_ok"], 1)
        self.assertEqual(report["steps_failed"], 1)
        self.assertEqual(report["next_index"], 1)
        self.assertTrue(report["reconciled"])

        status, resumed = self.call(
            "POST",
            "/rehearsal/resume",
            {
                "run_id": report["run_id"],
                "patch": {"index": 1, "op": "filter_close", "params": {"id": "b1"}},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["steps_ok"], 3)
        self.assertTrue(resumed["reconciled"])
        self.assertEqual(len(resumed["patches"]), 1)

        status, audit = self.call("POST", "/rehearsal/audit", {"run_id": report["run_id"]})
        kinds = [entry["kind"] for entry in audit["entries"]]
        self.assertIn("step_failed", kinds)
        self.assertIn("run_failed", kinds)
        self.assertIn("patch_applied", kinds)
        self.assertIn("run_resumed", kinds)
        self.assertEqual(kinds[-1], "run_completed")

    def test_resumed_run_matches_uninterrupted_run(self) -> None:
        failed = self.run_scenario(failing_scenario())
        self.call(
            "POST",
            "/rehearsal/resume",
            {
                "run_id": failed["run_id"],
                "patch": {"index": 1, "op": "filter_close", "params": {"id": "b1"}},
            },
        )
        status, resumed = self.call("POST", "/rehearsal/report", {"run_id": failed["run_id"]})
        self.assertEqual(status, 200)

        clean = self.run_scenario(patched_scenario())
        self.assertEqual(resumed["final_hash"], clean["final_hash"])

        status, verify = self.call("POST", "/rehearsal/verify", {"run_id": resumed["run_id"]})
        self.assertTrue(verify["consistent"], verify)

    def test_restart_policy_invalidates_round(self) -> None:
        report = self.run_scenario(failing_scenario(policy="restart"))
        self.assertEqual(report["status"], "aborted")
        self.assertEqual(report["attempts"], 1)
        self.assertEqual(report["audit"]["by_kind"]["run_aborted"], 1)

        status, replayed = self.call(
            "POST",
            "/rehearsal/resume",
            {
                "run_id": report["run_id"],
                "patch": {"index": 1, "op": "filter_close", "params": {"id": "b1"}},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(replayed["status"], "completed")
        self.assertEqual(replayed["attempts"], 2)
        self.assertEqual(replayed["attempt_log"][0]["outcome"], "aborted")
        self.assertTrue(replayed["reconciled"])

        clean = self.run_scenario(patched_scenario())
        self.assertEqual(replayed["final_hash"], clean["final_hash"])

    def test_snapshot_run_does_not_touch_live_state(self) -> None:
        self.call("POST", "/filter/add", {"id": "live-bed", "zone": 3, "load": 4.0})
        _, before = self.call("GET", "/health")
        before_data = dict(before["data"])

        scenario = {
            "mode": "snapshot",
            "failure_policy": "resume",
            "operations": [
                {"op": "coag_dose", "params": {"flow": 500.0}},
                {"op": "chlor_dose", "params": {}},
                {"op": "quota_add", "params": {"amount": 9.0}},
            ],
        }
        report = self.run_scenario(scenario)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["provenance"]["source"], "snapshot")
        self.assertEqual(report["provenance"]["live_key_count"], before["size"])
        self.assertGreaterEqual(report["provenance"]["live_event_count"], 1)

        _, after = self.call("GET", "/health")
        self.assertEqual(dict(after["data"]), before_data)
        _, audit = self.call("GET", "/audit/summary")
        self.assertEqual(audit["count"], 0)

    def test_synthetic_boundary_conditions(self) -> None:
        # Constructed edge case: ph below the stable band must gate chlorine.
        scenario = synthetic_scenario()
        scenario["initial"]["store"]["ph:value"] = "5.0000"
        report = self.run_scenario(scenario)
        self.assertEqual(report["status"], "completed")

        status, replay = self.call("POST", "/rehearsal/replay", {"run_id": report["run_id"]})
        cycle_step = replay["steps"][0]
        self.assertFalse(cycle_step["basis"]["ph_stable"])
        self.assertEqual(cycle_step["basis"]["ph_direction"], "raise")
        self.assertEqual(cycle_step["result"]["chlor_dose"], 0.0)

    def test_failed_run_replays_consistently(self) -> None:
        report = self.run_scenario(failing_scenario())
        self.assertEqual(report["status"], "failed")
        status, verify = self.call("POST", "/rehearsal/verify", {"run_id": report["run_id"]})
        self.assertEqual(status, 200)
        self.assertTrue(verify["consistent"], verify)
        self.assertEqual(verify["steps_checked"], 2)

    def test_snapshot_is_pinned_against_live_drift(self) -> None:
        scenario = {
            "mode": "snapshot",
            "failure_policy": "resume",
            "operations": [{"op": "coag_dose", "params": {"flow": 100.0}}],
        }
        report = self.run_scenario(scenario)

        self.call("POST", "/flow/replace", {"serial": "fm-9", "factor": 3.0})
        self.call("POST", "/quota/add", {"amount": 50.0})

        status, verify = self.call("POST", "/rehearsal/verify", {"run_id": report["run_id"]})
        self.assertEqual(status, 200)
        self.assertTrue(verify["consistent"], verify)

        again = self.run_scenario(scenario)
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["final_hash"], report["final_hash"])

    def test_crash_recovery_via_registry(self) -> None:
        report = self.run_scenario(failing_scenario())
        self.assertEqual(report["status"], "failed")

        # A fresh server over the same store path sees the persisted checkpoint.
        other = Server(Store.open(self.store.path))
        response = other.dispatch(
            "POST",
            "/rehearsal/resume",
            {"run_id": report["run_id"], "patch": {"index": 1, "op": "filter_close", "params": {"id": "b1"}}},
        )
        resumed = json.loads(response.body.decode("utf-8"))
        self.assertEqual(resumed["status"], "completed")

        clean = self.run_scenario(patched_scenario())
        self.assertEqual(resumed["final_hash"], clean["final_hash"])

    def test_report_matches_audit_ledger(self) -> None:
        report = self.run_scenario(synthetic_scenario())
        status, audit = self.call("POST", "/rehearsal/audit", {"run_id": report["run_id"]})
        self.assertEqual(status, 200)
        self.assertEqual(audit["count"], report["audit"]["entries"])
        kinds: dict[str, int] = {}
        for entry in audit["entries"]:
            kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
        self.assertEqual(kinds, report["audit"]["by_kind"])
        self.assertEqual(
            [entry["seq"] for entry in audit["entries"]], list(range(audit["count"]))
        )

    def test_unknown_run_and_invalid_scenario_errors(self) -> None:
        status, body = self.call("POST", "/rehearsal/replay", {"run_id": "run-missing"})
        self.assertEqual(status, 404)
        self.assertIn("not found", body["error"])

        status, body = self.call("POST", "/rehearsal/run", {"mode": "synthetic"})
        self.assertEqual(status, 400)
        self.assertIn("failure_policy", body["error"])

        status, body = self.call(
            "POST", "/rehearsal/run", {"mode": "other", "failure_policy": "resume", "operations": [{"op": "quota_add"}]}
        )
        self.assertEqual(status, 400)
        self.assertIn("mode", body["error"])

        status, body = self.call(
            "POST",
            "/rehearsal/run",
            {"mode": "synthetic", "failure_policy": "resume", "operations": [{"op": "nope"}],
             "initial": {"store": {}, "beds": []}},
        )
        self.assertEqual(status, 400)
        self.assertIn("unknown op", body["error"])

    def test_scenario_source_rules_are_fixed(self) -> None:
        status, body = self.call(
            "POST",
            "/rehearsal/run",
            {"mode": "synthetic", "failure_policy": "resume", "operations": [{"op": "quota_add"}]},
        )
        self.assertEqual(status, 400)
        self.assertIn("initial", body["error"])

        status, body = self.call(
            "POST",
            "/rehearsal/run",
            {
                "mode": "snapshot",
                "failure_policy": "resume",
                "initial": {"store": {}, "beds": []},
                "operations": [{"op": "quota_add"}],
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("must not carry an initial world", body["error"])

        status, body = self.call(
            "POST",
            "/rehearsal/run",
            {"mode": "synthetic", "failure_policy": "undo", "initial": {"store": {}, "beds": []},
             "operations": [{"op": "quota_add"}]},
        )
        self.assertEqual(status, 400)
        self.assertIn("failure_policy", body["error"])

    def test_patch_must_match_breakpoint(self) -> None:
        report = self.run_scenario(failing_scenario())
        status, body = self.call(
            "POST",
            "/rehearsal/resume",
            {"run_id": report["run_id"], "patch": {"index": 0, "op": "quota_add", "params": {"amount": 1.0}}},
        )
        self.assertEqual(status, 400)
        self.assertIn("breakpoint", body["error"])

    def test_run_id_is_stable_across_servers(self) -> None:
        report = self.run_scenario(synthetic_scenario())
        other = Server(Store.open(self.store.path))
        response = other.dispatch("POST", "/rehearsal/run", synthetic_scenario())
        again = json.loads(response.body.decode("utf-8"))
        self.assertEqual(again["run_id"], report["run_id"])
        self.assertTrue(again["idempotent"])


if __name__ == "__main__":
    unittest.main()
