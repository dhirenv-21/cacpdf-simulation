"""ATNO - Adaptive Threat Navigation Optimizer (grid A*).

Hard constraints (never traded against cost): grid boundary, obstacles,
no corner-cutting on diagonals, and prohibited cells.

Route objective for the threat-aware planner:

    J(path) = lambda_d * distance + lambda_t * exposure
    exposure = sum over entered cells of exposure(cell) * step_length

``exposure(cell)`` is the static exposure of marked zones plus any dynamic
exposure added after CTRE alerts. The obstacle-only baseline planner uses
J = distance. Distance and exposure are always reported separately.

If the threat-aware optimum exceeds ``max_route_exposure``, the planner also
searches for the minimum-exposure route; if even that exceeds the limit, no
safe route exists and the caller must stop and report why.
"""

from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set

from .config import ATNOConfig
from .world import Cell, GridMap, step_length


@dataclass
class PlanResult:
    path: Optional[List[Cell]]
    distance: float = 0.0
    exposure: float = 0.0
    cost: float = 0.0
    reason: str = ""
    planner: str = ""
    rejected_path: Optional[List[Cell]] = None   # best route found but deemed unsafe

    @property
    def ok(self) -> bool:
        return self.path is not None

    def to_dict(self) -> dict:
        return {"ok": self.ok, "distance": round(self.distance, 3),
                "exposure": round(self.exposure, 3), "cost": round(self.cost, 3),
                "reason": self.reason, "planner": self.planner,
                "path": self.path, "rejected_path": self.rejected_path}


def octile(a: Cell, b: Cell) -> float:
    dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
    return (dx + dy) + (math.sqrt(2) - 2) * min(dx, dy)


def path_metrics(path: List[Cell], exposure_fn: Callable[[Cell], float]):
    dist = expo = 0.0
    for a, b in zip(path, path[1:]):
        L = step_length(a, b)
        dist += L
        expo += exposure_fn(b) * L
    return dist, expo


class Planner:
    def __init__(self, world: GridMap, cfg: ATNOConfig, threat_aware: bool):
        self.world = world
        self.cfg = cfg
        self.threat_aware = threat_aware
        self.name = "threat-aware A*" if threat_aware else "obstacle-only A*"

    def _astar(self, start: Cell, goal: Cell, lam_d: float, lam_t: float,
               exposure_fn: Callable[[Cell], float], prohibited: Set[Cell]) -> Optional[List[Cell]]:
        if not self.world.passable(start) or not self.world.passable(goal) or goal in prohibited:
            return None
        counter = itertools.count()   # deterministic tie-breaking
        open_heap = [(lam_d * octile(start, goal), next(counter), start)]
        g = {start: 0.0}
        parent = {start: None}
        closed = set()
        while open_heap:
            _, _, cur = heapq.heappop(open_heap)
            if cur in closed:
                continue
            if cur == goal:
                path = [cur]
                while parent[path[-1]] is not None:
                    path.append(parent[path[-1]])
                return path[::-1]
            closed.add(cur)
            for nb, L in self.world.neighbors(cur, self.cfg.allow_diagonal):
                if nb in prohibited or nb in closed:
                    continue
                step_cost = lam_d * L + lam_t * exposure_fn(nb) * L
                ng = g[cur] + step_cost
                if ng < g.get(nb, float("inf")) - 1e-12:
                    g[nb] = ng
                    parent[nb] = cur
                    heapq.heappush(open_heap, (ng + lam_d * octile(nb, goal), next(counter), nb))
        return None

    def plan(self, start: Cell, goal: Cell, exposure_fn: Callable[[Cell], float],
             prohibited: Optional[Set[Cell]] = None) -> PlanResult:
        prohibited = set(prohibited or ()) - {start}
        cfg = self.cfg
        lam_t = cfg.lambda_exposure if self.threat_aware else 0.0
        path = self._astar(start, goal, cfg.lambda_distance, lam_t, exposure_fn, prohibited)
        if path is None:
            return PlanResult(None, reason="no route: boundaries/obstacles/prohibited zones "
                                           "block every path to the goal", planner=self.name)
        dist, expo = path_metrics(path, exposure_fn)
        res = PlanResult(path, dist, expo, cfg.lambda_distance * dist + lam_t * expo,
                         "optimal under " + ("J = distance + exposure" if self.threat_aware
                                             else "J = distance (exposure ignored)"),
                         self.name)
        if self.threat_aware and expo > cfg.max_route_exposure + 1e-9:
            # try the minimum-exposure route before declaring the mission unsafe
            alt = self._astar(start, goal, 1e-3, 1.0, exposure_fn, prohibited)
            if alt is not None:
                d2, e2 = path_metrics(alt, exposure_fn)
                if e2 <= cfg.max_route_exposure + 1e-9:
                    return PlanResult(alt, d2, e2, cfg.lambda_distance * d2 + lam_t * e2,
                                      "minimum-exposure route (cost-optimal route exceeded "
                                      "exposure limit)", self.name)
                expo = min(expo, e2)
            return PlanResult(None, dist, expo, res.cost,
                              f"no safe route: minimum exposure {expo:.1f} exceeds limit "
                              f"{cfg.max_route_exposure:.1f}", self.name, rejected_path=path)
        return res
