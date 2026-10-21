"""Event record schema shared by the generator, fusion, logging and GUI.

Each record represents one simulated message from a simulated source. Nothing
here is read from real sensors or real networks.

``injected_fault`` is ground-truth bookkeeping written by the scenario
generator. Decision modules (fusion, ADTA, CTRE, policy) never read it; it is
used only for logging and offline evaluation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

# Simulated sources. 'group' identifies independence: sources in different
# groups are treated as independent evidence for corroboration.
SOURCES: Dict[str, Dict[str, str]] = {
    "camera-1":    {"type": "physical", "group": "vision",  "label": "Camera (sim)"},
    "range-1":     {"type": "physical", "group": "ranging", "label": "Range sensor (sim)"},
    "beam-1":      {"type": "physical", "group": "beam",    "label": "Zone beam (sim)"},
    "door-ctrl-1": {"type": "cyber",    "group": "access",  "label": "Door controller telemetry (sim)"},
}

# Event types. 'positive' types claim the staged scenario event is present in a
# zone; 'negative' types explicitly claim the zone is clear. Status messages are
# heartbeats with no claim about any zone.
POSITIVE_TYPES = {"object_detected", "range_contact", "zone_entry", "access_anomaly"}
NEGATIVE_TYPES = {"zone_clear"}
STATUS_TYPES = {"status"}
ALL_TYPES = POSITIVE_TYPES | NEGATIVE_TYPES | STATUS_TYPES


@dataclass
class Event:
    event_id: str
    source_id: str
    event_type: str
    step: int                  # timestamp claimed by the source (simulation steps)
    arrival_step: int          # step at which the record reaches the rover
    seq: int                   # per-source sequence number (duplicates reuse one)
    confidence: float
    zone_id: Optional[str] = None
    value: Dict[str, Any] = field(default_factory=dict)
    injected_fault: Optional[str] = None   # ground truth label only (not a decision input)

    @property
    def source_type(self) -> str:
        return SOURCES.get(self.source_id, {}).get("type", "unknown")

    @property
    def polarity(self) -> int:
        """+1 claims event present, -1 claims zone clear, 0 no claim."""
        if self.event_type in POSITIVE_TYPES:
            return 1
        if self.event_type in NEGATIVE_TYPES:
            return -1
        return 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["source_type"] = self.source_type
        return d
