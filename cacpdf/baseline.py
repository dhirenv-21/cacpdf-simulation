"""Fixed-threshold detect-and-alert baseline (comparison method).

No dynamic trust, no freshness, no duplicate/stale/plausibility checks and no
handling of contradicting reports. It only drops records whose confidence is
not a number in [0, 1]. Rule, using records received in the last ``window``
steps:

    distinct sources with a positive report of confidence >= alert_confidence
        0                      -> NORMAL
        1                      -> INVESTIGATE  (alert)
        >= high_alert_sources  -> HIGH_ALERT   (alert)

It receives exactly the same event records as the proposed method.
"""

from __future__ import annotations

from typing import List, Tuple

from .config import BaselineConfig
from .ctre import Assessment
from .events import Event


class BaselineDetector:
    def __init__(self, cfg: BaselineConfig):
        self.cfg = cfg
        self.recent: List[Tuple[int, Event]] = []
        self.state = "NORMAL"

    def assess(self, arrived: List[Event], t: int) -> Assessment:
        cfg = self.cfg
        for ev in arrived:
            if ev.polarity > 0 and isinstance(ev.confidence, (int, float)) \
                    and 0.0 <= ev.confidence <= 1.0 and ev.confidence >= cfg.alert_confidence:
                self.recent.append((t, ev))
        self.recent = [(rt, ev) for rt, ev in self.recent if t - rt < cfg.window]
        sources = sorted({ev.source_id for _, ev in self.recent})
        if len(sources) >= cfg.high_alert_sources:
            state = "HIGH_ALERT"
        elif sources:
            state = "INVESTIGATE"
        else:
            state = "NORMAL"
        transition = f"{self.state} -> {state}" if state != self.state else None
        self.state = state
        zone = self.recent[-1][1].zone_id if self.recent else None
        expl = [f"Baseline: {len(sources)} source(s) with confidence >= "
                f"{cfg.alert_confidence} in last {cfg.window} steps"]
        expl += [f"  + {ev.source_id} {ev.event_type} C={ev.confidence:.2f} (step {ev.step})"
                 for _, ev in self.recent]
        return Assessment(step=t, state=state, raw_state=state, zone_id=zone,
                          rule="fixed threshold", s_pos=max([ev.confidence for _, ev in self.recent],
                                                            default=0.0),
                          s_neg=0.0, supporting=[{"source_id": ev.source_id,
                                                  "confidence": ev.confidence}
                                                 for _, ev in self.recent],
                          explanation=expl, transition=transition)
