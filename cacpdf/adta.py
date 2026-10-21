"""ADTA - Adaptive Dynamic Trust Algorithm (simulation version).

Per source i, trust is kept in [0, 1] and updated whenever a record arrives
(or a source goes silent):

    T_i(t) = clamp(T_i(t-1) + alpha*R_i(t) - beta*P_i(t) - gamma*A_i(t), 0, 1)

* R (reward): ``reward_valid`` for a timely, valid, plausible record, plus
  ``reward_corroborated`` when an independent source currently agrees. During
  ``probation_steps`` after any penalty, rewards are multiplied by
  ``probation_reward_scale``; total rise per step is capped by
  ``max_rise_per_step`` (trust is slow to regain after bad behaviour).
* P (penalty): sum of the configured penalty for each integrity reason code
  (stale, invalid_value, implausible, duplicate, delayed, missing).
* A (disagreement): a single disagreement is only *recorded* (no penalty),
  because disagreement can mean either source is wrong or the scene is unusual.
  When the same pair disagrees repeatedly, every Nth repeat applies a small
  *symmetric* penalty to both sides. A source contradicted by >= 2 agreeing
  independent sources (and supported by none) receives ``outvoted_penalty``.

Each observation's evidence weight is W = T_source * confidence * freshness
(computed in fusion.FusionEngine.picture). Ground-truth labels are never used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .config import ADTAConfig
from .events import SOURCES
from .fusion import Observation


@dataclass
class TrustUpdate:
    step: int
    source_id: str
    before: float
    after: float
    reasons: List[str]
    event_id: Optional[str] = None
    injected_fault: Optional[str] = None   # copied from record for offline evaluation only

    @property
    def delta(self) -> float:
        return self.after - self.before

    def to_dict(self) -> dict:
        return {"step": self.step, "source_id": self.source_id, "before": round(self.before, 4),
                "after": round(self.after, 4), "delta": round(self.delta, 4),
                "reasons": list(self.reasons), "event_id": self.event_id,
                "injected_fault": self.injected_fault}


class ADTA:
    def __init__(self, cfg: ADTAConfig, sources: Optional[Dict] = None):
        self.cfg = cfg
        self.sources = sources or SOURCES
        self.trust: Dict[str, float] = {
            s: float(cfg.initial_trust.get(s, cfg.default_initial_trust)) for s in self.sources}
        self.initial = dict(self.trust)
        self.probation_until: Dict[str, int] = {s: -1 for s in self.sources}
        self.rise_used: Dict[str, float] = {s: 0.0 for s in self.sources}
        self.pair_conflicts: Dict[Tuple[str, str], int] = {}
        self.pair_last_step: Dict[Tuple[str, str], int] = {}
        self.last_reason: Dict[str, str] = {s: "initial" for s in self.sources}
        self._step = -1

    def begin_step(self, t: int) -> None:
        if t != self._step:
            self._step = t
            self.rise_used = {s: 0.0 for s in self.sources}

    def _apply(self, t: int, source: str, reward: float, penalty: float, disagreement: float,
               reasons: List[str], event_id=None, fault=None) -> TrustUpdate:
        cfg = self.cfg
        before = self.trust[source]
        if penalty > 0 or disagreement > 0:
            self.probation_until[source] = t + cfg.probation_steps
        if reward > 0 and t <= self.probation_until[source] and not (penalty or disagreement):
            reward *= cfg.probation_reward_scale
            reasons.append("probation(reward scaled)")
        delta = cfg.alpha * reward - cfg.beta * penalty - cfg.gamma * disagreement
        if delta > 0:
            budget = max(0.0, cfg.max_rise_per_step - self.rise_used[source])
            if delta > budget:
                delta = budget
                reasons.append("rise_capped")
            self.rise_used[source] += delta
        after = before + delta
        if after > 1.0 or after < 0.0:
            reasons.append("clamped")
        after = max(0.0, min(1.0, after))
        self.trust[source] = after
        self.last_reason[source] = ", ".join(reasons) if reasons else "no change"
        return TrustUpdate(t, source, before, after, reasons, event_id, fault)

    def update_for_observation(self, obs: Observation, agree: List[str], disagree: List[str],
                               t: int) -> List[TrustUpdate]:
        """Update the observation's source (and, on repeated conflict, its partner)."""
        cfg = self.cfg
        self.begin_step(t)
        src = obs.source_id
        if src not in self.trust:
            return []  # unknown source: record is already flagged invalid, nothing to score
        ev = obs.event
        reasons: List[str] = []
        penalty = sum(cfg.penalties.get(f, 0.0) for f in obs.flags)
        reasons.extend(obs.flags)
        reward = 0.0
        disagreement = 0.0
        partner_updates: List[TrustUpdate] = []
        if obs.accepted:
            reward += cfg.reward_valid
            if not obs.flags:
                reasons.append("valid")
            if agree:
                reward += cfg.reward_corroborated
                reasons.append("corroborated:" + "+".join(agree))
            for p in disagree:
                # conflicts are counted at most once per pair per step
                pair = tuple(sorted((src, p)))
                if self.pair_last_step.get(pair) == t:
                    reasons.append(f"disagreement_observed:{p}(no penalty)")
                    continue
                self.pair_last_step[pair] = t
                n = self.pair_conflicts.get(pair, 0) + 1
                self.pair_conflicts[pair] = n
                if n % cfg.disagreement_repeat == 0:
                    disagreement += cfg.disagreement_penalty
                    reasons.append(f"repeated_disagreement:{p}(x{n},symmetric)")
                    partner_updates.append(self._apply(
                        t, p, 0.0, 0.0, cfg.disagreement_penalty,
                        [f"repeated_disagreement:{src}(x{n},symmetric)"]))
                else:
                    reasons.append(f"disagreement_observed:{p}(no penalty)")
            if len(disagree) >= 2 and not agree:
                groups = {self.sources[p]["group"] for p in disagree}
                if len(groups) >= 2:
                    disagreement += cfg.outvoted_penalty
                    reasons.append("outvoted_by:" + "+".join(disagree))
        upd = self._apply(t, src, reward, penalty, disagreement, reasons, ev.event_id,
                          ev.injected_fault)
        return [upd] + partner_updates

    def update_missing(self, source: str, t: int, fault_label: Optional[str] = None) -> TrustUpdate:
        """Penalise a heartbeat timeout. ``fault_label`` is evaluation bookkeeping only."""
        self.begin_step(t)
        return self._apply(t, source, 0.0, self.cfg.penalties.get("missing", 0.0), 0.0,
                           ["missing(heartbeat timeout)"], None, fault_label)

    def snapshot(self) -> Dict[str, float]:
        return {k: round(v, 4) for k, v in self.trust.items()}
