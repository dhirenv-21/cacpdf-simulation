"""Central configuration for every CACPDF threshold and weight.

All tunable numbers live here so they are easy to find, change, and record.
A full copy of the active configuration is written into every run log.

Values were hand-set during development while inspecting the *calibration*
seed split only (see ``cacpdf.evaluate.SPLITS``). They were then frozen before
running the *evaluation* split. They are not optimised and have no claim to
generality beyond these simulated scenarios.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict


@dataclass
class SimConfig:
    max_steps: int = 150          # hard mission time limit (safety: stop when reached)
    step_seconds: float = 0.5     # nominal simulated seconds per step (display only)


@dataclass
class FusionConfig:
    """Integrity / freshness rules applied to each arriving record."""
    delay_threshold: int = 2      # age (arrival - source step) above this => 'delayed'
    stale_threshold: int = 4      # age above this at arrival => 'stale' (weight 0)
    association_window: int = 6   # steps a report stays in the zone evidence picture
    freshness_tau: float = 3.0    # exp decay constant (steps) once age > delay_threshold
    heartbeat_timeout: int = 8    # steps without any message => source 'missing'
    range_min_m: float = 0.05     # plausible range-sensor distance limits
    range_max_m: float = 6.0


@dataclass
class ADTAConfig:
    """Adaptive Dynamic Trust Algorithm parameters.

    T_i(t) = clamp(T_i(t-1) + alpha*R_i(t) - beta*P_i(t) - gamma*A_i(t), 0, 1)
    with the per-step rise additionally limited by ``max_rise_per_step``.
    """
    initial_trust: Dict[str, float] = field(default_factory=lambda: {
        "camera-1": 0.80, "range-1": 0.80, "beam-1": 0.80, "door-ctrl-1": 0.70,
    })
    default_initial_trust: float = 0.70
    alpha: float = 1.0
    beta: float = 1.0
    gamma: float = 1.0
    # R: rewards
    reward_valid: float = 0.004        # timely, valid, plausible record
    reward_corroborated: float = 0.03  # independent source agrees within window
    # P: penalties per integrity reason code
    penalties: Dict[str, float] = field(default_factory=lambda: {
        "stale": 0.15,
        "invalid_value": 0.25,
        "implausible": 0.20,
        "duplicate": 0.08,
        "delayed": 0.03,
        "missing": 0.04,
    })
    # A: disagreement handling (never a single-conflict penalty)
    disagreement_penalty: float = 0.03   # applied to BOTH sides on every Nth repeat
    disagreement_repeat: int = 3         # N
    outvoted_penalty: float = 0.05       # source contradicted by >= 2 agreeing independents
    # recovery limits
    max_rise_per_step: float = 0.03
    probation_steps: int = 12            # after a penalty, rewards are scaled down...
    probation_reward_scale: float = 0.25  # ...by this factor


@dataclass
class CTREConfig:
    """Cognitive Threat Reasoning Engine rule thresholds (see ctre.py docstring)."""
    min_obs_weight: float = 0.25          # weight for a source to count as 'supporting'
    conflict_level: float = 0.30          # S_pos and S_neg both >= this => conflict
    uncertain_support: float = 0.20       # weak support => UNCERTAIN
    investigate_support: float = 0.60
    investigate_sources: int = 2
    high_alert_support: float = 0.85
    high_alert_sources: int = 3
    high_alert_requires_cross_domain: bool = True   # needs physical AND cyber support
    rejected_evidence_is_uncertain: bool = True     # unreliable positive report => UNCERTAIN...
    rejected_evidence_window: int = 2               # ...for this many steps after it arrives
    missing_source_is_uncertain: bool = False       # silence is noted, not escalated
    escalate_persist: int = 2             # raw state must persist N steps to escalate
    deescalate_persist: int = 4           # ...and M steps to de-escalate (hysteresis)


@dataclass
class ATNOConfig:
    """Route cost J = lambda_d * distance + lambda_t * exposure (hard constraints first)."""
    lambda_distance: float = 1.0
    lambda_exposure: float = 4.0
    max_route_exposure: float = 6.0       # threat-aware routes above this are 'unsafe'
    allow_diagonal: bool = True
    investigate_zone_exposure: float = 1.0  # dynamic exposure added to a zone under INVESTIGATE
    alert_memory_steps: int = 40            # how long dynamic zone costs persist


@dataclass
class PolicyConfig:
    uncertain_max_wait: int = 20          # UNCERTAIN longer than this => safe stop
    investigate_max_hold: int = 40        # INVESTIGATE hold longer than this => safe stop


@dataclass
class BaselineConfig:
    """Fixed-threshold detect-and-alert baseline (no trust, no freshness, no conflicts)."""
    alert_confidence: float = 0.70        # any single positive report >= this => alert
    high_alert_sources: int = 2           # >= this many distinct sources => HIGH_ALERT
    window: int = 6                       # steps (by arrival) a report keeps the alert active
    max_hold: int = 40


@dataclass
class Config:
    sim: SimConfig = field(default_factory=SimConfig)
    fusion: FusionConfig = field(default_factory=FusionConfig)
    adta: ADTAConfig = field(default_factory=ADTAConfig)
    ctre: CTREConfig = field(default_factory=CTREConfig)
    atno: ATNOConfig = field(default_factory=ATNOConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    baseline: BaselineConfig = field(default_factory=BaselineConfig)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        """Build a config from defaults overridden by a (possibly partial) dict."""
        cfg = cls()
        for section, values in (data or {}).items():
            if not hasattr(cfg, section):
                raise ValueError(f"Unknown config section '{section}'")
            target = getattr(cfg, section)
            valid = {f.name for f in fields(target)}
            for key, value in values.items():
                if key not in valid:
                    raise ValueError(f"Unknown config key '{section}.{key}'")
                current = getattr(target, key)
                if isinstance(current, dict) and isinstance(value, dict):
                    merged = dict(current)
                    merged.update(value)
                    value = merged
                setattr(target, key, value)
        return cfg

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

