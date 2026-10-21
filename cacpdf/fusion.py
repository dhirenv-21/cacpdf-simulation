"""Evidence fusion: integrity checks, time alignment and per-zone evidence pictures.

Rules (all thresholds in ``FusionConfig``):

* age = arrival_step - source step. age < 0 => ``invalid_value`` (future timestamp);
  age > stale_threshold => ``stale``; age > delay_threshold => ``delayed``.
* A repeated (source, seq) pair => ``duplicate`` (never counted twice).
* Malformed values (confidence outside [0, 1], unknown type/source/zone, range
  distance outside sensor limits, negative counters) => ``invalid_value``.
* A reported cell outside the map, inside an obstacle, or outside the claimed
  zone => ``implausible``.
* Records flagged stale/duplicate/invalid/implausible are *rejected* as evidence
  but are kept (and shown) so the decision can be audited. ``delayed`` records
  are accepted with reduced freshness.
* Missing evidence is never converted into negative evidence: a silent source is
  listed as a coverage gap. Only explicit ``zone_clear`` reports count against an
  event.

The original record is always preserved alongside the derived flags.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .config import FusionConfig
from .events import ALL_TYPES, SOURCES, Event
from .world import GridMap

REJECT_FLAGS = {"stale", "duplicate", "invalid_value", "implausible"}


@dataclass
class Observation:
    event: Event
    received_step: int
    age_at_arrival: int
    flags: List[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return not (set(self.flags) & REJECT_FLAGS)

    @property
    def source_id(self) -> str:
        return self.event.source_id

    def to_dict(self) -> dict:
        return {"event_id": self.event.event_id, "source_id": self.event.source_id,
                "event_type": self.event.event_type, "zone_id": self.event.zone_id,
                "step": self.event.step, "received_step": self.received_step,
                "age_at_arrival": self.age_at_arrival, "confidence": self.event.confidence,
                "flags": list(self.flags), "accepted": self.accepted}


@dataclass
class EvidenceItem:
    """One source's contribution to a zone picture at the current step."""
    source_id: str
    source_type: str
    polarity: int
    event_type: str
    event_id: str
    confidence: float
    trust: float
    freshness: float
    weight: float
    age: int
    flags: List[str]
    since_arrival: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class ZonePicture:
    zone_id: str
    step: int
    positives: List[EvidenceItem]
    negatives: List[EvidenceItem]
    rejected: List[EvidenceItem]
    missing_sources: List[str]

    @staticmethod
    def noisy_or(weights: List[float]) -> float:
        p = 1.0
        for w in weights:
            p *= (1.0 - max(0.0, min(1.0, w)))
        return 1.0 - p

    @property
    def s_pos(self) -> float:
        return self.noisy_or([e.weight for e in self.positives])

    @property
    def s_neg(self) -> float:
        return self.noisy_or([e.weight for e in self.negatives])


class FusionEngine:
    def __init__(self, cfg: FusionConfig, world: GridMap, sources: Optional[Dict] = None):
        self.cfg = cfg
        self.world = world
        self.sources = sources or SOURCES
        self.seen: Set[Tuple[str, int]] = set()
        self.last_heard: Dict[str, int] = {s: 0 for s in self.sources}
        self.missing: Set[str] = set()
        self.window: List[Observation] = []   # zone-related observations (accepted or not)

    # ------------------------------------------------------------------ checks
    def validate(self, ev: Event, t: int) -> Observation:
        cfg = self.cfg
        flags: List[str] = []
        age = t - ev.step
        key = (ev.source_id, ev.seq)
        if key in self.seen:
            flags.append("duplicate")
        if ev.source_id not in self.sources or ev.event_type not in ALL_TYPES:
            flags.append("invalid_value")
        elif not (0.0 <= ev.confidence <= 1.0):
            flags.append("invalid_value")
        elif ev.event_type == "range_contact":
            d = ev.value.get("distance_m")
            if not isinstance(d, (int, float)) or not (cfg.range_min_m <= d <= cfg.range_max_m):
                flags.append("invalid_value")
        elif ev.event_type == "access_anomaly":
            n = ev.value.get("failed_attempts", 0)
            if not isinstance(n, (int, float)) or n < 0:
                flags.append("invalid_value")
        if ev.polarity != 0 and "invalid_value" not in flags:
            zone = self.world.zones.get(ev.zone_id or "")
            if zone is None:
                flags.append("invalid_value")
            else:
                cell = ev.value.get("cell")
                if cell is not None:
                    cell = tuple(cell)
                    if (not self.world.passable(cell)) or cell not in zone.cells:
                        flags.append("implausible")
        if age < 0:
            flags.append("invalid_value")
        elif age > cfg.stale_threshold:
            flags.append("stale")
        elif age > cfg.delay_threshold:
            flags.append("delayed")
        # de-duplicate flag list while keeping order
        flags = list(dict.fromkeys(flags))
        return Observation(ev, t, age, flags)

    def ingest(self, events: List[Event], t: int) -> List[Observation]:
        out = []
        for ev in events:
            obs = self.validate(ev, t)
            self.seen.add((ev.source_id, ev.seq))
            if ev.source_id in self.last_heard and "invalid_value" not in obs.flags:
                self.last_heard[ev.source_id] = t
            if ev.polarity != 0:
                self.window.append(obs)
            out.append(obs)
        self._prune(t)
        return out

    def check_missing(self, t: int) -> Tuple[List[str], List[str]]:
        """Return (newly_missing, resumed) source ids."""
        newly, resumed = [], []
        for s, last in self.last_heard.items():
            silent = t - last > self.cfg.heartbeat_timeout
            if silent and s not in self.missing:
                self.missing.add(s)
                newly.append(s)
            elif not silent and s in self.missing:
                self.missing.discard(s)
                resumed.append(s)
        return newly, resumed

    def _prune(self, t: int) -> None:
        # keep by *arrival* so rejected (e.g. stale) records stay visible for audit;
        # accepted records are additionally aged out by their source timestamp below
        w = self.cfg.association_window
        self.window = [o for o in self.window if t - o.received_step <= w]

    # --------------------------------------------------------------- weights
    def freshness(self, age: int) -> float:
        cfg = self.cfg
        if age > cfg.association_window or age < 0:
            return 0.0
        if age <= cfg.delay_threshold:
            return 1.0
        return math.exp(-(age - cfg.delay_threshold) / cfg.freshness_tau)

    def _latest_accepted(self, zone_id: str, t: int) -> Dict[str, Observation]:
        latest: Dict[str, Observation] = {}
        for o in self.window:
            if (o.event.zone_id != zone_id or not o.accepted or o.received_step > t
                    or t - o.event.step > self.cfg.association_window):
                continue
            cur = latest.get(o.source_id)
            if cur is None or (o.event.step, o.event.seq) > (cur.event.step, cur.event.seq):
                latest[o.source_id] = o
        return latest

    def relations(self, obs: Observation, t: int) -> Tuple[List[str], List[str]]:
        """Independent sources that currently agree / disagree with ``obs``."""
        agree, disagree = [], []
        if not obs.accepted or obs.event.polarity == 0:
            return agree, disagree
        my_group = self.sources[obs.source_id]["group"]
        for sid, other in self._latest_accepted(obs.event.zone_id, t).items():
            if sid == obs.source_id or self.sources[sid]["group"] == my_group:
                continue
            if other.event.polarity == obs.event.polarity:
                agree.append(sid)
            else:
                disagree.append(sid)
        return sorted(agree), sorted(disagree)

    def picture(self, zone_id: str, t: int, trust: Dict[str, float]) -> ZonePicture:
        def item(o: Observation) -> EvidenceItem:
            age = t - o.event.step
            f = self.freshness(age) if o.accepted else 0.0
            tr = trust.get(o.source_id, 0.0)
            w = tr * max(0.0, min(1.0, o.event.confidence)) * f if o.accepted else 0.0
            return EvidenceItem(o.source_id, self.sources.get(o.source_id, {}).get("type", "?"),
                                o.event.polarity, o.event.event_type, o.event.event_id,
                                o.event.confidence, tr, f, w, age, list(o.flags),
                                t - o.received_step)

        pos, neg = [], []
        for o in self._latest_accepted(zone_id, t).values():
            it = item(o)
            if it.weight <= 0:
                continue
            (pos if it.polarity > 0 else neg).append(it)
        rejected = [item(o) for o in self.window
                    if o.event.zone_id == zone_id and not o.accepted and o.received_step <= t
                    and t - o.received_step <= self.cfg.association_window]
        return ZonePicture(zone_id, t, sorted(pos, key=lambda e: e.source_id),
                           sorted(neg, key=lambda e: e.source_id), rejected,
                           sorted(self.missing))
