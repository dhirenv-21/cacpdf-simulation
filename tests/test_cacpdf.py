"""Unit and scenario tests. Run with:  python -m unittest discover -s tests -v"""

import json
import os
import tempfile
import unittest

from cacpdf.adta import ADTA
from cacpdf.atno import Planner
from cacpdf.config import Config
from cacpdf.ctre import CTRE
from cacpdf.events import Event
from cacpdf.fusion import FusionEngine
from cacpdf.logger import save_run
from cacpdf.safety import SafetyMonitor
from cacpdf.scenarios import SCENARIOS, get_scenario
from cacpdf.simulation import Simulation


def ev(i, src, etype, step, arrival=None, seq=None, conf=0.85, zone="Z1", value=None):
    return Event(f"t{i}", src, etype, step, step if arrival is None else arrival,
                 i if seq is None else seq, conf, zone, value or {"cell": [12, 7]})


class TestFusionAndTrust(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.world = get_scenario("A").build_world()
        self.fusion = FusionEngine(self.cfg.fusion, self.world)
        self.adta = ADTA(self.cfg.adta)

    def feed(self, events, t):
        ups = []
        for o in self.fusion.ingest(events, t):
            a, d = self.fusion.relations(o, t)
            ups += self.adta.update_for_observation(o, a, d, t)
        return ups

    def test_stale_record_rejected_and_penalised(self):
        before = self.adta.trust["camera-1"]
        ups = self.feed([ev(1, "camera-1", "object_detected", 2, arrival=12)], 12)
        self.assertIn("stale", ups[0].reasons)
        self.assertLess(self.adta.trust["camera-1"], before)
        pic = self.fusion.picture("Z1", 12, self.adta.trust)
        self.assertEqual(pic.positives, [])
        self.assertEqual(len(pic.rejected), 1)

    def test_duplicate_detected_and_not_double_counted(self):
        self.feed([ev(1, "camera-1", "object_detected", 5)], 5)
        ups = self.feed([ev(2, "camera-1", "object_detected", 5, arrival=6, seq=1)], 6)
        self.assertIn("duplicate", ups[0].reasons)
        pic = self.fusion.picture("Z1", 6, self.adta.trust)
        self.assertEqual(len(pic.positives), 1)

    def test_invalid_and_implausible_values(self):
        ups = self.feed([ev(1, "door-ctrl-1", "access_anomaly", 3, conf=1.4)], 3)
        self.assertIn("invalid_value", ups[0].reasons)
        ups = self.feed([ev(2, "camera-1", "object_detected", 4, value={"cell": [11, 4]})], 4)
        self.assertIn("implausible", ups[0].reasons)   # (11,4) is an obstacle cell
        ups = self.feed([ev(3, "range-1", "range_contact", 5,
                            value={"cell": [12, 7], "distance_m": 99.0})], 5)
        self.assertIn("invalid_value", ups[0].reasons)

    def test_trust_stays_in_unit_interval(self):
        for i in range(60):
            self.feed([ev(i, "camera-1", "object_detected", i, arrival=i + 20)], i + 20)
        self.assertGreaterEqual(self.adta.trust["camera-1"], 0.0)
        for i in range(300):
            self.feed([ev(1000 + i, "range-1", "status", 100 + i, zone=None, value={})], 100 + i)
        self.assertLessEqual(self.adta.trust["range-1"], 1.0)

    def test_single_disagreement_is_not_penalised(self):
        self.feed([ev(1, "camera-1", "object_detected", 5)], 5)
        before = dict(self.adta.trust)
        ups = self.feed([ev(2, "range-1", "zone_clear", 6, value={"distance_m": 5.0})], 6)
        self.assertTrue(any("disagreement_observed" in r for r in ups[0].reasons))
        self.assertGreaterEqual(self.adta.trust["range-1"], before["range-1"])
        self.assertGreaterEqual(self.adta.trust["camera-1"], before["camera-1"])

    def test_repeated_disagreement_penalises_both_sides_equally(self):
        t0 = dict(self.adta.trust)
        for k in range(3):
            s = 5 + k
            self.feed([ev(10 + 2 * k, "camera-1", "object_detected", s),
                       ev(11 + 2 * k, "range-1", "zone_clear", s, value={"distance_m": 5.0})], s)
        d_cam = self.adta.trust["camera-1"] - t0["camera-1"]
        d_rng = self.adta.trust["range-1"] - t0["range-1"]
        self.assertLess(d_cam, 0)
        self.assertLess(d_rng, 0)
        self.assertAlmostEqual(d_cam, d_rng, delta=0.02)

    def test_missing_source_is_not_negative_evidence(self):
        for t in range(1, 20):
            self.fusion.check_missing(t)
        self.assertIn("beam-1", self.fusion.missing)
        pic = self.fusion.picture("Z1", 19, self.adta.trust)
        self.assertEqual(pic.negatives, [])
        self.assertIn("beam-1", pic.missing_sources)

    def test_corroboration_rewards(self):
        self.feed([ev(1, "camera-1", "object_detected", 5)], 5)
        ups = self.feed([ev(2, "range-1", "range_contact", 5,
                            value={"cell": [12, 7], "distance_m": 2.0})], 5)
        self.assertTrue(any(r.startswith("corroborated") for r in ups[0].reasons))


class TestCTRE(unittest.TestCase):
    def test_escalation_needs_persistence(self):
        cfg = Config()
        world = get_scenario("A").build_world()
        fusion = FusionEngine(cfg.fusion, world)
        adta = ADTA(cfg.adta)
        ctre = CTRE(cfg.ctre)
        states = []
        for t in range(1, 4):
            fusion.ingest([ev(10 * t, "camera-1", "object_detected", t),
                           ev(10 * t + 1, "range-1", "range_contact", t,
                              value={"cell": [12, 7], "distance_m": 2.0})], t)
            states.append(ctre.assess({"Z1": fusion.picture("Z1", t, adta.trust)}, t).state)
        self.assertEqual(states[0], "NORMAL")          # raw INVESTIGATE, 1/2
        self.assertEqual(states[1], "INVESTIGATE")


class TestPlanningAndSafety(unittest.TestCase):
    def test_paths_respect_obstacles_and_corners(self):
        for key in SCENARIOS:
            sim = Simulation(key, 0)
            p = sim.reference_plan
            if not p.ok:
                continue
            mon = SafetyMonitor(sim.world)
            for a, b in zip(p.path, p.path[1:]):
                self.assertIsNone(mon.check_move(a, b, set()), f"{key}: {a}->{b}")

    def test_threat_aware_avoids_exposure_zone(self):
        sim = Simulation("E", 0)
        self.assertGreater(sim.reference_plan.exposure, 0)
        self.assertEqual(sim.threat_plan_initial.exposure, 0)
        self.assertGreater(sim.threat_plan_initial.distance, sim.reference_plan.distance)

    def test_no_safe_route_reports_reason(self):
        sim = Simulation("F", 0)
        self.assertFalse(sim.threat_plan_initial.ok)
        self.assertIn("exceeds limit", sim.threat_plan_initial.reason)

    def test_no_route_when_blocked(self):
        world = get_scenario("F").build_world()
        planner = Planner(world, Config().atno, threat_aware=False)
        res = planner.plan(world.start, world.goal, lambda c: 0.0,
                           prohibited=world.zones["Z1"].cells)
        self.assertFalse(res.ok)

    def test_safety_monitor_blocks_obstacles_and_boundary(self):
        world = get_scenario("A").build_world()
        mon = SafetyMonitor(world)
        self.assertIsNotNone(mon.check_move((10, 4), (11, 4), set()))
        self.assertIsNotNone(mon.check_move((0, 0), (-1, 0), set()))
        self.assertIsNotNone(mon.check_move((2, 7), (4, 7), set()))
        self.assertIsNone(mon.check_move((2, 7), (3, 7), set()))


class TestScenarios(unittest.TestCase):
    def test_deterministic_replay(self):
        a = Simulation("C", 7)
        b = Simulation("C", 7)
        a.run_to_end()
        b.run_to_end()
        self.assertEqual(a.step_log, b.step_log)
        self.assertEqual(a.trust_log, b.trust_log)

    def test_methods_receive_identical_inputs(self):
        for key in SCENARIOS:
            self.assertEqual(Simulation(key, 3, "proposed").events_hash,
                             Simulation(key, 3, "baseline").events_hash)

    def test_reset_is_clean(self):
        s = Simulation("G", 2)
        first = s.run_to_end()
        s.reset()
        self.assertEqual(s.t, 0)
        self.assertEqual(s.run_to_end(), first)

    def test_expected_behaviour_proposed(self):
        for key in SCENARIOS:
            for seed in range(3):
                sim = Simulation(key, seed, "proposed")
                out = sim.run_to_end()
                self.assertEqual(out["collisions"], 0)
                if key == "B" and "occluded" in sim.truth["notes"]:
                    continue  # documented single-source failure case
                self.assertTrue(out["expected_behavior_met"], f"{key} seed {seed}: {out['checks']}")

    def test_manual_injection(self):
        sim = Simulation("A", 0)
        for _ in range(3):
            sim.step()
        sim.inject("stale")
        sim.step()
        self.assertTrue(any("stale" in o["flags"] for o in sim.observation_log))
        with self.assertRaises(ValueError):
            sim.inject("bogus")


class TestConfigAndLogging(unittest.TestCase):
    def test_config_override(self):
        cfg = Config.from_dict({"ctre": {"escalate_persist": 3}, "adta": {"penalties": {"stale": 0.5}}})
        self.assertEqual(cfg.ctre.escalate_persist, 3)
        self.assertEqual(cfg.adta.penalties["stale"], 0.5)
        self.assertIn("invalid_value", cfg.adta.penalties)
        with self.assertRaises(ValueError):
            Config.from_dict({"ctre": {"nope": 1}})

    def test_log_contents(self):
        sim = Simulation("B", 1)
        sim.run_to_end()
        with tempfile.TemporaryDirectory() as d:
            paths = save_run(sim, d)
            with open(paths["json"], encoding="utf-8") as fh:
                log = json.load(fh)
            for k in ("run_id", "scenario", "seed", "config", "events", "trust_updates",
                      "steps", "alerts", "routes", "outcome"):
                self.assertIn(k, log)
            self.assertTrue(os.path.exists(paths["steps_csv"]))


if __name__ == "__main__":
    unittest.main()
