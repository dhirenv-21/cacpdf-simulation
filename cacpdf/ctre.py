"""CTRE - Cognitive Threat Reasoning Engine (interpretable rule table).

States describe the *staged simulation scenario* only; they are not judgments
about real people or objects.

For each monitored zone, with W_j = trust * confidence * freshness:

    S_pos = 1 - prod(1 - W_j)  over the latest accepted positive report per source
    S_neg = 1 - prod(1 - W_j)  over the latest accepted explicit 'zone_clear' reports
    supporters = positive sources with W_j >= min_obs_weight

Rules, evaluated in order (first match wins):

    R1 conflict      S_pos >= conflict_level and S_neg >= conflict_level      -> UNCERTAIN
    R2 high alert    supporters >= high_alert_sources, S_pos >= high_alert_support,
                     and (if required) physical AND cyber supporters          -> HIGH_ALERT
    R3 investigate   supporters >= investigate_sources, S_pos >= investigate_support
                                                                              -> INVESTIGATE
    R4 weak/unreliable  S_pos >= uncertain_support, or a rejected (stale/invalid/
                     duplicate/implausible) positive report arrived within the
                     last rejected_evidence_window steps                      -> UNCERTAIN
    R5 default                                                                -> NORMAL

The reported state is the most severe zone's raw state passed through a
persistence filter: escalation needs ``escalate_persist`` consecutive steps,
de-escalation needs ``deescalate_persist`` steps (prevents flicker).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import CTREConfig
from .fusion import ZonePicture

STATES = ["NORMAL", "UNCERTAIN", "INVESTIGATE", "HIGH_ALERT"]
SEVERITY = {s: i for i, s in enumerate(STATES)}
ALERT_STATES = {"INVESTIGATE", "HIGH_ALERT"}


@dataclass
class Assessment:
    step: int
    state: str
    raw_state: str
    zone_id: Optional[str]
    rule: str
    s_pos: float
    s_neg: float
    supporting: List[dict] = field(default_factory=list)
    conflicting: List[dict] = field(default_factory=list)
    rejected: List[dict] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    explanation: List[str] = field(default_factory=list)
    transition: Optional[str] = None
    pending: Optional[str] = None

    @property
    def confidence(self) -> float:
        """Net support for the event after discounting contrary evidence."""
        return self.s_pos * (1.0 - self.s_neg)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["confidence"] = round(self.confidence, 4)
        d["s_pos"] = round(self.s_pos, 4)
        d["s_neg"] = round(self.s_neg, 4)
        return d


def _fmt(e) -> str:
    return (f"{e.source_id} {e.event_type} w={e.weight:.2f} "
            f"(T={e.trust:.2f} C={e.confidence:.2f} F={e.freshness:.2f})")


class CTRE:
    def __init__(self, cfg: CTREConfig):
        self.cfg = cfg
        self.state = "NORMAL"
        self.candidate: Optional[str] = None
        self.candidate_count = 0

    def raw_zone_state(self, pic: ZonePicture):
        cfg = self.cfg
        s_pos, s_neg = pic.s_pos, pic.s_neg
        supporters = [e for e in pic.positives if e.weight >= cfg.min_obs_weight]
        domains = {e.source_type for e in supporters}
        rejected_pos = [e for e in pic.rejected if e.polarity > 0
                        and e.since_arrival <= cfg.rejected_evidence_window]
        lines: List[str] = []
        if s_pos >= cfg.conflict_level and s_neg >= cfg.conflict_level:
            state, rule = "UNCERTAIN", "R1 conflict"
            lines.append(f"R1: sources disagree about {pic.zone_id} "
                         f"(S_pos={s_pos:.2f}, S_neg={s_neg:.2f} both >= {cfg.conflict_level}); "
                         "neither is assumed correct")
        elif (len(supporters) >= cfg.high_alert_sources and s_pos >= cfg.high_alert_support
              and (not cfg.high_alert_requires_cross_domain or {"physical", "cyber"} <= domains)):
            state, rule = "HIGH_ALERT", "R2 cross-domain corroboration"
            lines.append(f"R2: {len(supporters)} independent sources ({'+'.join(sorted(domains))}) "
                         f"support the staged event in {pic.zone_id} (S_pos={s_pos:.2f})")
        elif len(supporters) >= cfg.investigate_sources and s_pos >= cfg.investigate_support:
            state, rule = "INVESTIGATE", "R3 corroborated"
            lines.append(f"R3: {len(supporters)} independent sources support the staged event "
                         f"in {pic.zone_id} (S_pos={s_pos:.2f} >= {cfg.investigate_support})")
        elif s_pos >= cfg.uncertain_support or (cfg.rejected_evidence_is_uncertain and rejected_pos):
            state, rule = "UNCERTAIN", "R4 weak or unreliable evidence"
            why = []
            if s_pos >= cfg.uncertain_support:
                why.append(f"uncorroborated support S_pos={s_pos:.2f}")
            if rejected_pos:
                why.append(f"{len(rejected_pos)} unreliable report(s) rejected")
            lines.append(f"R4: {', '.join(why)} for {pic.zone_id}; requesting another observation")
        elif cfg.missing_source_is_uncertain and pic.missing_sources:
            state, rule = "UNCERTAIN", "R4b coverage gap"
            lines.append("R4b: monitored sources silent")
        else:
            state, rule = "NORMAL", "R5 no supported event"
            lines.append(f"R5: no sufficiently supported event in {pic.zone_id} "
                         f"(S_pos={s_pos:.2f})")
        for e in pic.positives:
            lines.append("  + " + _fmt(e))
        for e in pic.negatives:
            lines.append("  - " + _fmt(e))
        for e in pic.rejected:
            lines.append(f"  x {e.source_id} {e.event_type} rejected: {','.join(e.flags)}")
        for s in pic.missing_sources:
            lines.append(f"  ? {s} silent (coverage gap, NOT treated as 'clear')")
        return state, rule, lines

    def assess(self, pictures: Dict[str, ZonePicture], t: int) -> Assessment:
        best = None
        for zid in sorted(pictures):
            pic = pictures[zid]
            state, rule, lines = self.raw_zone_state(pic)
            key = (SEVERITY[state], pic.s_pos)
            if best is None or key > best[0]:
                best = (key, pic, state, rule, lines)
        if best is None:
            raw, rule, lines, pic = "NORMAL", "R5 no monitored zone", ["no monitored zones"], None
        else:
            _, pic, raw, rule, lines = best

        transition, pending = None, None
        if raw == self.state:
            self.candidate, self.candidate_count = None, 0
        else:
            if raw == self.candidate:
                self.candidate_count += 1
            else:
                self.candidate, self.candidate_count = raw, 1
            need = (self.cfg.escalate_persist if SEVERITY[raw] > SEVERITY[self.state]
                    else self.cfg.deescalate_persist)
            if self.candidate_count >= need:
                transition = f"{self.state} -> {raw}"
                self.state = raw
                self.candidate, self.candidate_count = None, 0
            else:
                pending = f"{raw} ({self.candidate_count}/{need})"
        explanation = list(lines)
        if pending:
            explanation.insert(0, f"Persistence filter: holding {self.state}, raw {pending}")
        if transition:
            explanation.insert(0, f"Transition {transition}")

        return Assessment(
            step=t, state=self.state, raw_state=raw, zone_id=pic.zone_id if pic else None,
            rule=rule, s_pos=pic.s_pos if pic else 0.0, s_neg=pic.s_neg if pic else 0.0,
            supporting=[e.to_dict() for e in pic.positives] if pic else [],
            conflicting=[e.to_dict() for e in pic.negatives] if pic else [],
            rejected=[e.to_dict() for e in pic.rejected] if pic else [],
            missing=list(pic.missing_sources) if pic else [],
            explanation=explanation, transition=transition, pending=pending)
