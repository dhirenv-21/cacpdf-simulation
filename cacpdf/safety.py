"""Response policy action set and the safety monitor.

Only safe simulated actions exist. There is deliberately no pursue, confront,
intercept or any force-related action.

Proposed policy (CTRE state -> action):

    NORMAL       -> CONTINUE along the planned route (REROUTE if costs changed it)
    UNCERTAIN    -> WAIT in place and request another observation;
                    SAFE_STOP if uncertainty lasts > uncertain_max_wait steps
    INVESTIGATE  -> INVESTIGATE: hold at a safe observation point outside the zone
                    (MOVE_TO_SAFE_POINT first if inside it), raise a local alert,
                    add dynamic exposure to the zone for later planning
    HIGH_ALERT   -> STOP_ALERT on entry, zone becomes no-go, then REROUTE via an
                    alternate route; SAFE_STOP if no safe route exists
    no safe route / time limit -> SAFE_STOP with a stated reason

Baseline policy: alert -> STOP_ALERT (hold); otherwise CONTINUE on the
obstacle-only route.

The SafetyMonitor validates every motion command after the policy and can
override it with a hold. It enforces boundaries, obstacles, no corner cutting,
single-cell moves and prohibited cells regardless of what the planner wants.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Set

from .world import Cell, GridMap

ACTIONS = {
    "CONTINUE": "follow route to next waypoint",
    "REROUTE": "follow alternate route that avoids the alerted/exposed zone",
    "WAIT": "pause in place and request another observation",
    "INVESTIGATE": "hold at a safe observation point outside the zone; local alert",
    "MOVE_TO_SAFE_POINT": "leave the alerted zone towards the nearest safe cell",
    "STOP_ALERT": "stop and raise alert",
    "HOLD": "hold position (safety override)",
    "SAFE_STOP": "terminal safe stop with stated reason",
    "ARRIVED": "goal reached",
}
MOVING_ACTIONS = {"CONTINUE", "REROUTE", "MOVE_TO_SAFE_POINT"}
MODE_OF_ACTION = {
    "CONTINUE": "MOVING", "REROUTE": "REROUTING", "WAIT": "WAITING",
    "INVESTIGATE": "INVESTIGATING", "MOVE_TO_SAFE_POINT": "REPOSITIONING",
    "STOP_ALERT": "STOPPED", "HOLD": "STOPPED", "SAFE_STOP": "SAFE_STOP", "ARRIVED": "ARRIVED",
}


@dataclass
class Decision:
    action: str
    reason: str
    alert: Optional[str] = None
    next_cell: Optional[Cell] = None
    terminal: bool = False
    override: Optional[str] = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class SafetyMonitor:
    def __init__(self, world: GridMap):
        self.world = world

    def check_move(self, cur: Cell, nxt: Cell, prohibited: Set[Cell]) -> Optional[str]:
        """Return a violation reason, or None if the move is allowed."""
        w = self.world
        if not w.in_bounds(nxt):
            return f"boundary: {nxt} outside map"
        if nxt in w.obstacles:
            return f"obstacle at {nxt}"
        dx, dy = nxt[0] - cur[0], nxt[1] - cur[1]
        if max(abs(dx), abs(dy)) != 1:
            return f"invalid step {cur}->{nxt} (must be one adjacent cell)"
        if dx and dy and not (w.passable((cur[0] + dx, cur[1])) and w.passable((cur[0], cur[1] + dy))):
            return f"diagonal corner-cut {cur}->{nxt}"
        if nxt in prohibited and cur not in prohibited:
            return f"prohibited cell {nxt}"
        return None

    def validate(self, decision: Decision, cur: Cell, prohibited: Set[Cell]) -> Decision:
        if decision.action not in MOVING_ACTIONS or decision.next_cell is None:
            return decision
        violation = self.check_move(cur, decision.next_cell, prohibited)
        if violation is None:
            return decision
        return Decision("HOLD", f"safety override: {violation}", decision.alert,
                        None, False, override=violation)
