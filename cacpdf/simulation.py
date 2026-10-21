"""Simulation engine: runs the CACPDF pipeline one step at a time.

    events -> fusion -> ADTA -> CTRE (or baseline) -> policy + ATNO -> safety -> motion -> log

This module has no rendering or input code. The GUI and the headless CLI both
drive it through ``reset()`` / ``step()`` and read its public state.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from . import __version__
from .adta import ADTA, TrustUpdate
from .atno import PlanResult, Planner, path_metrics
from .baseline import BaselineDetector
from .config import Config
from .ctre import ALERT_STATES, CTRE, SEVERITY, Assessment
from .events import SOURCES, Event
from .fusion import FusionEngine, Observation
from .safety import MODE_OF_ACTION, MOVING_ACTIONS, Decision, SafetyMonitor
from .scenarios import get_scenario
from .world import Cell, step_length

METHODS = ("proposed", "baseline")


@dataclass
class Rover:
    pos: Cell
    heading: float = 0.0        # degrees, 0 = east (+x), 90 = south (+y, screen down)
    mode: str = "READY"
    distance: float = 0.0
    trail: List[Cell] = field(default_factory=list)


class Simulation:
    def __init__(self, scenario: str = "A", seed: int = 0, method: str = "proposed",
                 config: Optional[Config] = None, run_tag: str = ""):
        if method not in METHODS:
            raise ValueError(f"Unknown method '{method}'. Choose one of: {', '.join(METHODS)}")
        self.scenario = get_scenario(scenario)
        self.seed = int(seed)
        self.method = method
        self.cfg = config or Config()
        self.run_tag = run_tag
        self.reset()

    # ------------------------------------------------------------------ setup
    @property
    def run_id(self) -> str:
        rid = f"{self.scenario.key}_{self.method}_seed{self.seed}"
        return f"{rid}_{self.run_tag}" if self.run_tag else rid

    def reset(self) -> None:
        cfg = self.cfg
        self.world = self.scenario.build_world()
        data = self.scenario.generate(self.seed)
        self.events: List[Event] = list(data.events)
        self.truth = data.truth
        self.events_hash = self._hash_events(self.events)
        self.manual_injections = 0
        self._ev_index = 0

        self.fusion = FusionEngine(cfg.fusion, self.world)
        self.adta = ADTA(cfg.adta)
        self.ctre = CTRE(cfg.ctre)
        self.baseline = BaselineDetector(cfg.baseline)
        self.safety = SafetyMonitor(self.world)
        self.threat_planner = Planner(self.world, cfg.atno, threat_aware=True)
        self.obstacle_planner = Planner(self.world, cfg.atno, threat_aware=False)
        self.planner = self.threat_planner if self.method == "proposed" else self.obstacle_planner

        self.t = 0
        self.rover = Rover(self.world.start, trail=[self.world.start])
        self.dynamic: Dict[str, Dict] = {}       # zone_id -> dynamic cost entry (proposed only)
        self.cost_version = 0
        self.static_prohibited: Set[Cell] = set().union(
            *[z.cells for z in self.world.zones.values() if z.prohibited])

        # reference plans from the start (for display and the planner comparison)
        self.reference_plan = self.obstacle_planner.plan(self.world.start, self.world.goal,
                                                         self.static_exposure, self.static_prohibited)
        self.threat_plan_initial = self.threat_planner.plan(self.world.start, self.world.goal,
                                                            self.exposure, self.prohibited())
        self.plan: Optional[PlanResult] = None
        self.route: List[Cell] = []
        self.route_version = -1
        self.route_history: List[dict] = []

        self.assessment: Optional[Assessment] = None
        self.shadow: Optional[Assessment] = None
        self.decision: Optional[Decision] = None
        self.prev_state = "NORMAL"
        self.uncertain_steps = 0
        self.hold_steps = 0
        self.done = False
        self.outcome: Dict = {}
        self.error: Optional[str] = None

        # logs
        self.observation_log: List[dict] = []
        self.trust_log: List[dict] = []
        self.step_log: List[dict] = []
        self.alerts: List[dict] = []
        self.recent_obs: deque = deque(maxlen=40)      # for the GUI event feed
        self.static_exposure_incurred = 0.0
        self.zone_entries_after_alert = 0
        self.event_zone_steps_during_event = 0
        self.first_alert_step: Optional[int] = None
        self.states_visited = {"NORMAL"}
        self.max_state = "NORMAL"
        self.safety_overrides = 0
        self.reroutes = 0

    @staticmethod
    def _hash_events(events: List[Event]) -> str:
        blob = json.dumps([e.to_dict() for e in events], sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    # ------------------------------------------------------------ cost model
    def static_exposure(self, c: Cell) -> float:
        return sum(z.exposure for z in self.world.zones_at(c))

    def exposure(self, c: Cell) -> float:
        """Static zone exposure plus dynamic exposure added after CTRE alerts."""
        e = self.static_exposure(c)
        for z in self.world.zones_at(c):
            d = self.dynamic.get(z.zone_id)
            if d:
                e += d["extra"]
        return e

    def prohibited(self) -> Set[Cell]:
        cells = set(self.static_prohibited)
        for zid, d in self.dynamic.items():
            if d["prohibited"]:
                cells |= self.world.zones[zid].cells
        return cells

    def _set_dynamic(self, zone_id: str, extra: float, prohibited: bool, reason: str) -> None:
        cur = self.dynamic.get(zone_id)
        until = self.t + self.cfg.atno.alert_memory_steps
        new = {"extra": max(extra, cur["extra"] if cur else 0.0),
               "prohibited": prohibited or bool(cur and cur["prohibited"]),
               "until": until, "reason": reason}
        if cur is None or new["extra"] != cur["extra"] or new["prohibited"] != cur["prohibited"]:
            self.cost_version += 1
        self.dynamic[zone_id] = new

    def _expire_dynamic(self) -> None:
        for zid in list(self.dynamic):
            if self.t > self.dynamic[zid]["until"]:
                del self.dynamic[zid]
                self.cost_version += 1

    # --------------------------------------------------------------- planning
    def _replan(self, why: str) -> PlanResult:
        prohibited = self.prohibited() if self.method == "proposed" else self.static_prohibited
        expo_fn = self.exposure if self.method == "proposed" else self.static_exposure
        old_remaining = self.route[self.route.index(self.rover.pos):] \
            if self.rover.pos in self.route else None
        res = self.planner.plan(self.rover.pos, self.world.goal, expo_fn, prohibited)
        self.plan = res
        self.route = list(res.path) if res.ok else []
        self.route_version = self.cost_version
        changed = bool(old_remaining) and res.ok and res.path != old_remaining
        self.route_history.append({"step": self.t, "why": why, "changed": changed, **res.to_dict()})
        return res

    def _route_next(self) -> Optional[Cell]:
        if self.rover.pos not in self.route:
            return None
        i = self.route.index(self.rover.pos)
        return self.route[i + 1] if i + 1 < len(self.route) else None

    def _safe_point_step(self, zone_cells: Set[Cell]) -> Optional[Cell]:
        """First step of the shortest path to the nearest passable cell outside ``zone_cells``."""
        start = self.rover.pos
        prev = {start: None}
        q = deque([start])
        while q:
            c = q.popleft()
            if c not in zone_cells and c != start:
                while prev[c] != start:
                    c = prev[c]
                return c
            for n, _ in self.world.neighbors(c, self.cfg.atno.allow_diagonal):
                if n not in prev:
                    prev[n] = c
                    q.append(n)
        return None

    # ------------------------------------------------------------ inject faults
    def inject(self, kind: str) -> str:
        """Manually inject a predefined fault record arriving at the next step (GUI demo aid)."""
        t = self.t + 1
        seq = max([e.seq for e in self.events if e.source_id == "camera-1"], default=0)
        n = len(self.events)
        cell = [12, 7]
        new: List[Event] = []
        if kind == "stale":
            new.append(Event(f"m{n:04d}", "camera-1", "object_detected", t - 8, t,
                             seq + 1000 + n, 0.9, "Z1", {"cell": cell}, "manual_stale"))
        elif kind == "duplicate":
            src = [e for e in self.events if e.arrival_step <= self.t and e.polarity != 0] or \
                  [e for e in self.events if e.arrival_step <= self.t]
            if not src:
                return "nothing to duplicate yet"
            e = src[-1]
            new.append(Event(f"m{n:04d}", e.source_id, e.event_type, e.step, t, e.seq,
                             e.confidence, e.zone_id, dict(e.value), "manual_duplicate"))
        elif kind == "invalid":
            new.append(Event(f"m{n:04d}", "door-ctrl-1", "access_anomaly", t, t, 5000 + n, 1.5,
                             "Z1", {"failed_attempts": 2}, "manual_invalid"))
        elif kind == "conflict":
            for k in range(4):
                new.append(Event(f"m{n + 2 * k:04d}", "camera-1", "object_detected", t + k, t + k,
                                 seq + 2000 + n + 2 * k, 0.85, "Z1", {"cell": cell},
                                 "manual_conflict"))
                new.append(Event(f"m{n + 2 * k + 1:04d}", "range-1", "zone_clear", t + k, t + k,
                                 3000 + n + 2 * k, 0.85, "Z1", {"distance_m": 5.0}, None))
        else:
            raise ValueError(f"Unknown fault kind '{kind}' (stale/duplicate/invalid/conflict)")
        done = self.events[:self._ev_index]
        pending = sorted(self.events[self._ev_index:] + new,
                         key=lambda e: (e.arrival_step, e.event_id))
        self.events = done + pending
        self.manual_injections += 1
        return f"injected {kind} ({len(new)} record(s)) for step {t}"

    # ------------------------------------------------------------------- step
    def _missing_label(self, source: str) -> Optional[str]:
        for src, a, b in self.truth.get("suppressed", []):
            if src == source and a <= self.t <= b + self.cfg.fusion.heartbeat_timeout + 1:
                return "missing"
        return None

    def _in_event_window(self) -> bool:
        w = self.truth.get("event_window")
        return bool(w) and w[0] <= self.t <= w[1]

    def step(self) -> None:
        if self.done:
            return
        self.t += 1
        t = self.t
        arrived: List[Event] = []
        while self._ev_index < len(self.events) and self.events[self._ev_index].arrival_step <= t:
            arrived.append(self.events[self._ev_index])
            self._ev_index += 1

        # 1-2. fusion integrity checks and ADTA trust updates
        self.adta.begin_step(t)
        observations = self.fusion.ingest(arrived, t)
        updates: List[TrustUpdate] = []
        for o in observations:
            agree, disagree = self.fusion.relations(o, t)
            ups = self.adta.update_for_observation(o, agree, disagree, t)
            updates.extend(ups)
            rec = o.to_dict()
            rec.update(agree=agree, disagree=disagree, injected_fault=o.event.injected_fault,
                       trust_after=round(self.adta.trust.get(o.source_id, 0.0), 4))
            self.observation_log.append(rec)
            self.recent_obs.appendleft(rec)
        newly, resumed = self.fusion.check_missing(t)
        for s in newly:
            updates.append(self.adta.update_missing(s, t, self._missing_label(s)))
            note = {"event_id": None, "source_id": s, "event_type": "heartbeat_timeout",
                    "zone_id": None, "step": t, "received_step": t, "age_at_arrival": 0,
                    "confidence": None, "flags": ["missing"], "accepted": False}
            self.observation_log.append(note)
            self.recent_obs.appendleft(note)
        for s in resumed:
            self.adta.last_reason[s] = "resumed after silence"
        self.trust_log.extend(u.to_dict() for u in updates)

        # 3. reasoning (proposed CTRE and fixed-threshold baseline on the same inputs)
        pictures = {zid: self.fusion.picture(zid, t, self.adta.trust)
                    for zid, z in self.world.zones.items() if z.monitored}
        proposed = self.ctre.assess(pictures, t)
        base = self.baseline.assess(arrived, t)
        if self.method == "proposed":
            self.assessment, self.shadow = proposed, base
        else:
            self.assessment, self.shadow = base, proposed
        a = self.assessment
        self.states_visited.add(a.state)
        if SEVERITY[a.state] > SEVERITY[self.max_state]:
            self.max_state = a.state
        if a.state in ALERT_STATES and self.prev_state not in ALERT_STATES:
            self.alerts.append({"step": t, "state": a.state, "zone_id": a.zone_id,
                                "rule": a.rule, "explanation": a.explanation[:6]})
            if self.first_alert_step is None:
                self.first_alert_step = t

        # 4. decision policy + ATNO, then the safety monitor
        try:
            decision = self._decide_proposed(a) if self.method == "proposed" \
                else self._decide_baseline(a)
        except Exception as exc:  # never leave the rover moving on an internal error
            decision = Decision("SAFE_STOP", f"internal error in decision module: {exc}",
                                terminal=True)
            self.error = str(exc)
        prohibited = self.prohibited() if self.method == "proposed" else self.static_prohibited
        decision = self.safety.validate(decision, self.rover.pos, prohibited)
        if decision.override:
            self.safety_overrides += 1
        self.decision = decision

        # 5. motion
        moved_from = self.rover.pos
        if decision.action in MOVING_ACTIONS and decision.next_cell is not None:
            nxt = decision.next_cell
            L = step_length(moved_from, nxt)
            self.rover.heading = math.degrees(math.atan2(nxt[1] - moved_from[1],
                                                         nxt[0] - moved_from[0]))
            self.rover.distance += L
            self.static_exposure_incurred += self.static_exposure(nxt) * L
            ev_zone = self.world.zones.get(self.truth.get("event_zone") or "")
            if ev_zone and nxt in ev_zone.cells and moved_from not in ev_zone.cells \
                    and self.first_alert_step is not None and self._in_event_window():
                self.zone_entries_after_alert += 1
            self.rover.pos = nxt
            self.rover.trail.append(nxt)
        ev_zone = self.world.zones.get(self.truth.get("event_zone") or "")
        if ev_zone and self.rover.pos in ev_zone.cells and self._in_event_window():
            self.event_zone_steps_during_event += 1
        if self.rover.pos == self.world.goal:
            decision = Decision("ARRIVED", "goal reached", decision.alert, terminal=True)
            self.decision = decision
        self.rover.mode = MODE_OF_ACTION.get(decision.action, decision.action)
        self.prev_state = a.state

        self._log_step(a, decision)
        if decision.terminal:
            self._finish(decision)
        elif t >= self.cfg.sim.max_steps:
            self.decision = Decision("SAFE_STOP", "mission time limit reached", terminal=True)
            self.rover.mode = "SAFE_STOP"
            self._finish(self.decision)

    # ---------------------------------------------------------------- policies
    def _continue(self, label: str = "CONTINUE") -> Decision:
        if not self.route or self.route_version != self.cost_version \
                or self.rover.pos not in self.route:
            res = self._replan("cost map changed" if self.route else "initial plan")
            if not res.ok:
                return Decision("SAFE_STOP", res.reason, terminal=True)
            if self.route_history[-1]["changed"]:
                label = "REROUTE"
                self.reroutes += 1
        nxt = self._route_next()
        if nxt is None:
            return Decision("SAFE_STOP", "route has no next waypoint", terminal=True)
        reason = ("following " + self.plan.planner + f" route (d={self.plan.distance:.1f}, "
                  f"exposure={self.plan.exposure:.1f})")
        return Decision(label, reason, next_cell=nxt)

    def _decide_proposed(self, a: Assessment) -> Decision:
        pcfg = self.cfg.policy
        self._expire_dynamic()
        state, zone = a.state, a.zone_id
        if state != "UNCERTAIN":
            self.uncertain_steps = 0
        if state not in ALERT_STATES:
            self.hold_steps = 0
        if state == "HIGH_ALERT" and zone:
            self._set_dynamic(zone, self.cfg.atno.investigate_zone_exposure, True,
                              "HIGH_ALERT: zone set no-go")
            zcells = self.world.zones[zone].cells
            alert = f"HIGH_ALERT in {zone} (staged scenario event)"
            if self.rover.pos in zcells:
                nxt = self._safe_point_step(zcells)
                if nxt is None:
                    return Decision("SAFE_STOP", "inside no-go zone and no exit found", alert,
                                    terminal=True)
                return Decision("MOVE_TO_SAFE_POINT", f"leaving {zone} (now no-go)", alert, nxt)
            if self.prev_state != "HIGH_ALERT":
                return Decision("STOP_ALERT", f"{zone} declared no-go; stopping before replanning",
                                alert)
            res = self._continue("REROUTE")
            if res.action == "SAFE_STOP":
                res.reason = f"no safe route while {zone} is no-go: {res.reason}"
                res.alert = alert
                return res
            res.action, res.alert = "REROUTE", alert
            res.reason = f"alternate route avoiding {zone}: " + res.reason
            return res
        if state == "INVESTIGATE" and zone:
            self.hold_steps += 1
            self._set_dynamic(zone, self.cfg.atno.investigate_zone_exposure, False,
                              "INVESTIGATE: exposure added")
            alert = f"local alert: INVESTIGATE {zone} (staged scenario event)"
            if self.hold_steps > pcfg.investigate_max_hold:
                return Decision("SAFE_STOP", "investigation unresolved within hold limit", alert,
                                terminal=True)
            zcells = self.world.zones[zone].cells
            if self.rover.pos in zcells:
                nxt = self._safe_point_step(zcells)
                if nxt is not None:
                    return Decision("MOVE_TO_SAFE_POINT", f"moving out of {zone} to observe safely",
                                    alert, nxt)
            return Decision("INVESTIGATE", f"holding at safe observation point outside {zone}",
                            alert)
        if state == "UNCERTAIN":
            self.uncertain_steps += 1
            if self.uncertain_steps > pcfg.uncertain_max_wait:
                return Decision("SAFE_STOP", f"uncertainty unresolved after "
                                f"{pcfg.uncertain_max_wait} steps ({a.rule})", terminal=True)
            return Decision("WAIT", f"{a.rule}: waiting, requesting another observation "
                            f"({self.uncertain_steps}/{pcfg.uncertain_max_wait})")
        return self._continue()

    def _decide_baseline(self, a: Assessment) -> Decision:
        if a.state in ALERT_STATES:
            self.hold_steps += 1
            alert = f"baseline alert: {a.state}"
            if self.hold_steps > self.cfg.baseline.max_hold:
                return Decision("SAFE_STOP", "alert unresolved within hold limit", alert,
                                terminal=True)
            return Decision("STOP_ALERT", "fixed-threshold alert: stop and hold", alert)
        self.hold_steps = 0
        return self._continue()

    # ----------------------------------------------------------------- logging
    def _log_step(self, a: Assessment, d: Decision) -> None:
        self.step_log.append({
            "step": self.t, "pos": list(self.rover.pos), "heading": round(self.rover.heading, 1),
            "mode": self.rover.mode, "action": d.action, "reason": d.reason, "alert": d.alert,
            "safety_override": d.override, "state": a.state, "raw_state": a.raw_state,
            "rule": a.rule, "zone_id": a.zone_id, "s_pos": round(a.s_pos, 4),
            "s_neg": round(a.s_neg, 4), "confidence": round(a.confidence, 4),
            "transition": a.transition, "explanation": a.explanation,
            "shadow_method": "baseline" if self.method == "proposed" else "proposed",
            "shadow_state": self.shadow.state if self.shadow else None,
            "trust": self.adta.snapshot(),
            "route_remaining": self._remaining_metrics(),
            "dynamic_zones": {k: dict(v) for k, v in self.dynamic.items()},
        })

    def _remaining_metrics(self) -> Optional[dict]:
        if not self.route or self.rover.pos not in self.route:
            return None
        rest = self.route[self.route.index(self.rover.pos):]
        d, e = path_metrics(rest, self.exposure if self.method == "proposed"
                            else self.static_exposure)
        return {"distance": round(d, 3), "exposure": round(e, 3), "waypoints": len(rest) - 1}

    def _finish(self, d: Decision) -> None:
        self.done = True
        self.outcome = self.summary()

    def summary(self) -> dict:
        d = self.decision
        truth = self.truth
        latency = None
        if truth.get("event_present") and self.first_alert_step is not None:
            latency = self.first_alert_step - truth["event_window"][0]
        n_uncertain = sum(1 for s in self.step_log if s["state"] == "UNCERTAIN")
        out = {
            "run_id": self.run_id, "scenario": self.scenario.key, "method": self.method,
            "seed": self.seed, "events_hash": self.events_hash,
            "manual_injections": self.manual_injections,
            "event_present": bool(truth.get("event_present")),
            "alert_raised": self.first_alert_step is not None,
            "first_alert_step": self.first_alert_step, "detection_latency": latency,
            "max_state": self.max_state, "states_visited": sorted(self.states_visited,
                                                                  key=SEVERITY.get),
            "uncertain_steps": n_uncertain,
            "goal_reached": bool(d and d.action == "ARRIVED"),
            "safe_stop": bool(d and d.action == "SAFE_STOP"),
            "final_action": d.action if d else None, "final_reason": d.reason if d else None,
            "steps": self.t, "distance_travelled": round(self.rover.distance, 3),
            "static_exposure_incurred": round(self.static_exposure_incurred, 3),
            "event_zone_steps_during_event": self.event_zone_steps_during_event,
            "zone_entries_after_alert": self.zone_entries_after_alert,
            "reroutes": self.reroutes, "safety_overrides": self.safety_overrides,
            "collisions": sum(1 for c in self.rover.trail if not self.world.passable(c)),
            "planned_shortest": {"distance": round(self.reference_plan.distance, 3),
                                 "exposure": round(self.reference_plan.exposure, 3),
                                 "ok": self.reference_plan.ok},
            "planned_threat_aware": {"distance": round(self.threat_plan_initial.distance, 3),
                                     "exposure": round(self.threat_plan_initial.exposure, 3),
                                     "ok": self.threat_plan_initial.ok,
                                     "reason": self.threat_plan_initial.reason},
            "error": self.error,
        }
        out["checks"] = self.expectation_checks(out)
        applicable = [v for v in out["checks"].values() if v is not None]
        out["expected_behavior_met"] = all(applicable) if applicable else None
        return out

    def expectation_checks(self, out: dict) -> Dict[str, Optional[bool]]:
        """Compare the run with the scenario's pre-written expected behaviour."""
        exp = self.scenario.expected
        c: Dict[str, Optional[bool]] = {}
        if "alert" in exp:
            c["alert"] = out["alert_raised"] == exp["alert"]
        if exp.get("no_high_alert"):
            c["no_high_alert"] = out["max_state"] != "HIGH_ALERT"
        if exp.get("high_alert"):
            c["high_alert"] = out["max_state"] == "HIGH_ALERT"
        if exp.get("visit_uncertain"):
            # the baseline has no UNCERTAIN state, so this check does not apply to it
            c["visit_uncertain"] = ("UNCERTAIN" in out["states_visited"]
                                    if self.method == "proposed" else None)
        if "goal" in exp:
            c["goal"] = out["goal_reached"] == exp["goal"]
        if "safe_stop" in exp:
            c["safe_stop"] = out["safe_stop"] == exp["safe_stop"]
        if "static_exposure_max" in exp:
            c["static_exposure"] = out["static_exposure_incurred"] <= exp["static_exposure_max"] + 1e-9
        if "zone_entries_after_alert_max" in exp:
            c["zone_entries_after_alert"] = (out["zone_entries_after_alert"]
                                             <= exp["zone_entries_after_alert_max"])
        c["no_collision"] = out["collisions"] == 0
        return c

    def run_to_end(self) -> dict:
        while not self.done:
            self.step()
        return self.outcome

    def to_log(self) -> dict:
        """Complete, self-describing record of the run (raw inputs + every derived output)."""
        return {
            "run_id": self.run_id, "software_version": __version__,
            "scenario": {"key": self.scenario.key, "name": self.scenario.name,
                         "description": self.scenario.description,
                         "expected_behavior": self.scenario.expected_text,
                         "expected_checks": self.scenario.expected},
            "method": self.method, "seed": self.seed, "config": self.cfg.to_dict(),
            "map": self.world.to_dict(), "ground_truth": self.truth,
            "sources": SOURCES, "events_hash": self.events_hash,
            "events": [e.to_dict() for e in self.events],
            "observations": self.observation_log, "trust_updates": self.trust_log,
            "steps": self.step_log, "alerts": self.alerts,
            "routes": self.route_history,
            "reference_plans": {"obstacle_only": self.reference_plan.to_dict(),
                                "threat_aware": self.threat_plan_initial.to_dict()},
            "outcome": self.outcome or self.summary(),
        }
