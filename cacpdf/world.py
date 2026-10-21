"""Grid world: boundaries, obstacles, start/goal and marked zones."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

Cell = Tuple[int, int]

# Legend for ASCII layouts: '#' obstacle, '.' free, 'S' start, 'G' goal.
BASE_LAYOUT = [
    "........................",  # 0   north gap (rows 0-2)
    "........................",  # 1
    "........................",  # 2
    "...........##.....##....",  # 3
    "...........##.....##....",  # 4
    "...........##...........",  # 5
    "........................",  # 6   centre gap (rows 6-8)
    "..S..................G..",  # 7
    "........................",  # 8
    "...........##...........",  # 9
    ".....##....##...........",  # 10
    ".....##....##...........",  # 11
    "...........##...........",  # 12
    "...........##...........",  # 13
    "...........##...........",  # 14
]


@dataclass
class Zone:
    zone_id: str
    label: str
    cells: Set[Cell]
    exposure: float = 0.0       # static exposure cost per unit distance inside the zone
    prohibited: bool = False    # static hard no-go (safety constraint for every method)
    monitored: bool = True      # staged scenario events are reported for this zone

    def to_dict(self) -> dict:
        return {"zone_id": self.zone_id, "label": self.label, "exposure": self.exposure,
                "prohibited": self.prohibited, "monitored": self.monitored,
                "cells": sorted(self.cells)}


@dataclass
class GridMap:
    width: int
    height: int
    obstacles: Set[Cell]
    start: Cell
    goal: Cell
    zones: Dict[str, Zone] = field(default_factory=dict)

    @classmethod
    def from_layout(cls, layout: List[str], zones: Iterable[Zone] = ()) -> "GridMap":
        height = len(layout)
        width = len(layout[0])
        obstacles: Set[Cell] = set()
        start = goal = None
        for y, row in enumerate(layout):
            if len(row) != width:
                raise ValueError(f"Layout row {y} has length {len(row)}, expected {width}")
            for x, ch in enumerate(row):
                if ch == "#":
                    obstacles.add((x, y))
                elif ch == "S":
                    start = (x, y)
                elif ch == "G":
                    goal = (x, y)
                elif ch != ".":
                    raise ValueError(f"Unknown layout character '{ch}' at {(x, y)}")
        if start is None or goal is None:
            raise ValueError("Layout must contain exactly one 'S' and one 'G'")
        gm = cls(width, height, obstacles, start, goal)
        for z in zones:
            # zones only cover traversable cells
            z.cells = {c for c in z.cells if gm.in_bounds(c) and c not in obstacles}
            gm.zones[z.zone_id] = z
        return gm

    def in_bounds(self, c: Cell) -> bool:
        return 0 <= c[0] < self.width and 0 <= c[1] < self.height

    def passable(self, c: Cell) -> bool:
        return self.in_bounds(c) and c not in self.obstacles

    def zones_at(self, c: Cell) -> List[Zone]:
        return [z for z in self.zones.values() if c in z.cells]

    def neighbors(self, c: Cell, diagonal: bool = True) -> List[Tuple[Cell, float]]:
        """Passable neighbours with step length; diagonals may not cut corners."""
        out = []
        x, y = c
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            n = (x + dx, y + dy)
            if self.passable(n):
                out.append((n, 1.0))
        if diagonal:
            for dx, dy in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
                n = (x + dx, y + dy)
                if (self.passable(n) and self.passable((x + dx, y))
                        and self.passable((x, y + dy))):
                    out.append((n, math.sqrt(2)))
        return out

    def to_dict(self) -> dict:
        return {"width": self.width, "height": self.height, "start": self.start,
                "goal": self.goal, "obstacles": sorted(self.obstacles),
                "zones": [z.to_dict() for z in self.zones.values()]}


def rect(x0: int, y0: int, x1: int, y1: int) -> Set[Cell]:
    """Inclusive rectangle of cells."""
    return {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}


def step_length(a: Cell, b: Cell) -> float:
    return math.sqrt(2) if (a[0] != b[0] and a[1] != b[1]) else 1.0
