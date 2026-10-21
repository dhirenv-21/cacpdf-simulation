"""Repeatable scenario definitions and seeded event generators.

Each scenario builds a map and a full list of simulated event records from a
seed. The records do not depend on the rover's behaviour, so every method
(proposed / baseline) receives *identical* inputs for the same (scenario, seed).

Ground truth (whether a staged event really occurs, injected fault labels) is
returned separately and is used only for logging and evaluation.

The staged "event" is an abstract scenario label (e.g. a placed marker object
in zone Z1). It is not a person and not a real-world threat judgment.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .events import SOURCES, Event
from .world import BASE_LAYOUT, GridMap, Zone, rect

ZONE_RECT = (9, 6, 14, 8)            # monitored zone Z1 covers the centre corridor
EVENT_CELLS = [(11, 6), (11, 7), (11, 8), (12, 6), (12, 7), (12, 8), (10, 7), (13, 7)]
HEARTBEAT_PERIOD = 4
HORIZON = 160


@dataclass
class ScenarioData:
    events: List[Event]
    truth: Dict


class EventBuilder:
    """Helper that assigns ids/sequence numbers deterministically."""

    def __init__(self, key: str, seed: int):
        self.rng = random.Random(f"cacpdf-{key}-{seed}")
        self.events: List[Event] = []
        self.seq = {s: 0 for s in SOURCES}

    def add(self, source: str, etype: str, step: int, conf: float, zone: Optional[str] = None,
            value: Optional[dict] = None, arrival: Optional[int] = None,
            fault: Optional[str] = None, seq: Optional[int] = None) -> Event:
        if seq is None:
            self.seq[source] += 1
            seq = self.seq[source]
        ev = Event(event_id=f"e{len(self.events):04d}", source_id=source, event_type=etype,
                   step=step, arrival_step=step if arrival is None else arrival, seq=seq,
                   confidence=round(conf, 3), zone_id=zone, value=dict(value or {}),
                   injected_fault=fault)
        self.events.append(ev)
        return ev

    def duplicate(self, ev: Event, arrival: int, fault: str = "duplicate") -> Event:
        return self.add(ev.source_id, ev.event_type, ev.step, ev.confidence, ev.zone_id,
                        ev.value, arrival=arrival, fault=fault, seq=ev.seq)

    def heartbeats(self, suppress: Optional[Dict[str, Tuple[int, int]]] = None,
                   delay: Optional[Dict[Tuple[str, int], int]] = None) -> None:
        """Periodic status messages from every source (staggered offsets)."""
        suppress = suppress or {}
        delay = delay or {}
        for i, src in enumerate(SOURCES):
            for step in range(1 + i, HORIZON, HEARTBEAT_PERIOD):
                win = suppress.get(src)
                if win and win[0] <= step <= win[1]:
                    continue
                d = delay.get((src, step), 0)
                self.add(src, "status", step, 1.0, value={"ok": True},
                         arrival=step + d, fault="delayed" if d else None)

    def cell(self):
        return list(self.rng.choice(EVENT_CELLS))

    def finish(self, truth: Dict) -> ScenarioData:
        self.events.sort(key=lambda e: (e.arrival_step, e.event_id))
        return ScenarioData(self.events, truth)


def _truth(present=False, window=None, suppressed=None, notes=""):
    return {"event_present": present, "event_zone": "Z1", "event_window": window,
            "suppressed": suppressed or [], "notes": notes}


# --------------------------------------------------------------------- generators
def gen_a(seed: int) -> ScenarioData:
    b = EventBuilder("A", seed)
    b.heartbeats()
    notes = "no staged event"
    if b.rng.random() < 0.35:   # occasional spurious single-source low/medium-confidence blip
        s = b.rng.randint(3, 25)
        b.add("camera-1", "object_detected", s, b.rng.uniform(0.35, 0.75), "Z1",
              {"cell": b.cell(), "label": "staged-marker"}, fault="spurious_detection")
        notes = f"spurious camera blip at step {s}"
    return b.finish(_truth(False, notes=notes))


def gen_b(seed: int) -> ScenarioData:
    b = EventBuilder("B", seed)
    b.heartbeats()
    t0 = b.rng.randint(2, 6)
    t1 = t0 + b.rng.randint(8, 12)
    cell = b.cell()
    range_occluded = b.rng.random() < 0.15   # sensor never sees the event (genuine miss)
    for s in range(t0, t1 + 1):
        if b.rng.random() > 0.15:
            b.add("camera-1", "object_detected", s, b.rng.uniform(0.72, 0.95), "Z1",
                  {"cell": cell, "label": "staged-marker"})
        if not range_occluded and b.rng.random() > 0.15:
            b.add("range-1", "range_contact", s, b.rng.uniform(0.65, 0.90), "Z1",
                  {"cell": cell, "distance_m": round(b.rng.uniform(0.8, 3.5), 2)})
    notes = "range sensor occluded for whole event" if range_occluded else "two-source event"
    return b.finish(_truth(True, [t0, t1], notes=notes))


def gen_c(seed: int) -> ScenarioData:
    b = EventBuilder("C", seed)
    miss = (b.rng.randint(18, 22), b.rng.randint(32, 38))
    delayed_step = 1 + 2 + HEARTBEAT_PERIOD * b.rng.randint(1, 3)   # a beam-1 heartbeat
    b.heartbeats(suppress={"beam-1": miss}, delay={("beam-1", delayed_step): 3})
    ta = b.rng.randint(4, 8)
    replay = b.add("camera-1", "object_detected", ta - b.rng.randint(6, 10),
                   b.rng.uniform(0.85, 0.95), "Z1", {"cell": b.cell(), "label": "staged-marker"},
                   arrival=ta, fault="stale_replay")
    b.duplicate(replay, ta + b.rng.randint(1, 2))
    si = b.rng.randint(10, 16)
    if b.rng.random() < 0.5:
        b.add("door-ctrl-1", "access_anomaly", si, 1.4, "Z1", {"failed_attempts": 2},
              fault="invalid_value")
    else:
        b.add("door-ctrl-1", "access_anomaly", si, 0.9, "Z1", {"failed_attempts": -3},
              fault="invalid_value")
    b.add("range-1", "range_contact", b.rng.randint(12, 20), b.rng.uniform(0.15, 0.28), "Z1",
          {"cell": b.cell(), "distance_m": round(b.rng.uniform(0.8, 3.5), 2)},
          fault="low_confidence")
    b.add("camera-1", "object_detected", b.rng.randint(22, 28), b.rng.uniform(0.8, 0.95), "Z1",
          {"cell": [11, 4], "label": "staged-marker"}, fault="implausible_position")
    return b.finish(_truth(False, suppressed=[["beam-1", miss[0], miss[1]]],
                           notes="no real event; only faulty/unreliable records"))


def gen_d(seed: int) -> ScenarioData:
    b = EventBuilder("D", seed)
    b.heartbeats()
    t0 = b.rng.randint(2, 5)
    t1 = t0 + b.rng.randint(8, 12)
    cell = b.cell()
    for s in range(t0, t1 + 1):
        b.add("camera-1", "object_detected", s, b.rng.uniform(0.72, 0.90), "Z1",
              {"cell": cell, "label": "staged-marker"}, fault="false_detection")
        b.add("range-1", "zone_clear", s, b.rng.uniform(0.75, 0.90), "Z1", {"distance_m": 5.0})
    return b.finish(_truth(False, notes=f"camera false detection {t0}-{t1} vs explicit clear"))


def gen_quiet(key: str) -> Callable[[int], ScenarioData]:
    def gen(seed: int) -> ScenarioData:
        b = EventBuilder(key, seed)
        b.heartbeats()
        return b.finish(_truth(False, notes="navigation-only scenario"))
    return gen


def gen_g(seed: int) -> ScenarioData:
    b = EventBuilder("G", seed)
    b.heartbeats()
    t0 = b.rng.randint(2, 6)
    t1 = t0 + b.rng.randint(10, 14)
    cell = b.cell()
    for s in range(t0, t1 + 1):
        if b.rng.random() > 0.1:
            b.add("camera-1", "object_detected", s, b.rng.uniform(0.75, 0.95), "Z1",
                  {"cell": cell, "label": "staged-marker"})
        if b.rng.random() > 0.1:
            b.add("range-1", "range_contact", s, b.rng.uniform(0.70, 0.90), "Z1",
                  {"cell": cell, "distance_m": round(b.rng.uniform(0.8, 3.5), 2)})
        if (s - t0) % 2 == 0:
            b.add("door-ctrl-1", "access_anomaly", s, b.rng.uniform(0.75, 0.95), "Z1",
                  {"door_id": "D1", "failed_attempts": b.rng.randint(3, 8)})
    return b.finish(_truth(True, [t0, t1], notes="physical + cyber corroborated staged event"))


# ------------------------------------------------------------------------ registry
@dataclass
class Scenario:
    key: str
    name: str
    description: str
    expected: Dict
    expected_text: str
    generator: Callable[[int], ScenarioData]
    zone_exposure: float = 0.0
    close_north_gap: bool = False

    def build_world(self) -> GridMap:
        layout = list(BASE_LAYOUT)
        if self.close_north_gap:
            for y in range(0, 3):
                row = list(layout[y])
                row[11] = row[12] = "#"
                layout[y] = "".join(row)
        label = "exposure zone Z1" if self.zone_exposure > 0 else "monitored zone Z1"
        zone = Zone("Z1", label, rect(*ZONE_RECT), exposure=self.zone_exposure)
        return GridMap.from_layout(layout, [zone])

    def generate(self, seed: int) -> ScenarioData:
        return self.generator(seed)


SCENARIOS: Dict[str, Scenario] = {s.key: s for s in [
    Scenario("A", "Normal operation",
             "Clear route, no staged event. Occasional single low/medium-confidence camera blip.",
             {"alert": False, "goal": True},
             "Reach goal; stay NORMAL (brief UNCERTAIN allowed); no alert.", gen_a),
    Scenario("B", "Corroborated event",
             "Camera and range sensor independently report a staged marker in Z1.",
             {"alert": True, "goal": True},
             "INVESTIGATE (or higher); hold at safe point with local alert; resume to goal.", gen_b),
    Scenario("C", "Unreliable / stale evidence",
             "Stale replay, duplicate, invalid value, low-confidence, implausible, delayed and "
             "missing records; no real event.",
             {"alert": False, "no_high_alert": True},
             "Trust of faulty sources drops; no alert; may wait/request observation or stop safely.",
             gen_c),
    Scenario("D", "Conflicting evidence",
             "Camera reports an object in Z1 while the range sensor explicitly reports Z1 clear.",
             {"alert": False, "no_high_alert": True, "visit_uncertain": True},
             "Expose disagreement; UNCERTAIN; cautious WAIT; no confirmed alert.", gen_d),
    Scenario("E", "Threat-zone route choice",
             "Shortest route crosses marked exposure zone Z1; a longer northern route avoids it.",
             {"alert": False, "goal": True, "static_exposure_max": 0.0},
             "Threat-aware planner takes the alternate route; reach goal with zero exposure.",
             gen_quiet("E"), zone_exposure=1.0),
    Scenario("F", "No safe route",
             "Northern gap closed; the only corridor crosses high-exposure zone Z1.",
             {"safe_stop": True, "goal": False, "static_exposure_max": 0.0},
             "Planner finds no route within the exposure limit; rover stops and reports why.",
             gen_quiet("F"), zone_exposure=3.0, close_north_gap=True),
    Scenario("G", "Cross-domain high alert",
             "Camera, range sensor and door-controller telemetry (cyber) corroborate a staged event.",
             {"alert": True, "high_alert": True, "goal": True, "zone_entries_after_alert_max": 0},
             "HIGH_ALERT; stop + alert; Z1 becomes no-go; reroute via alternate; reach goal.", gen_g),
]}


def get_scenario(key: str) -> Scenario:
    k = (key or "").strip().upper()
    if k not in SCENARIOS:
        raise ValueError(f"Unknown scenario '{key}'. Choose one of: {', '.join(SCENARIOS)}")
    return SCENARIOS[k]
