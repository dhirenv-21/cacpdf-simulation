"""Pygame front end: a dark "control-room" dashboard for live demonstrations.

Rendering and input only. Every value shown is read from the running
``Simulation`` (and its ADTA / CTRE / ATNO objects); nothing here changes or
invents system behaviour.

The UI is drawn on a fixed 1920x1080 canvas and scaled to the window, so the
layout is identical on a laptop and on a projector.

Keys:
    SPACE start/pause   N single step   R reset   1-7 scenario A-G
    M toggle method     [ / ] seed -/+   - / + speed
    F1 stale  F2 duplicate  F3 invalid  F4 conflict (inject a fault record)
    H show/hide heartbeat records   L save log now   F11 fullscreen   ESC quit
"""

from __future__ import annotations

import math
import os
import sys
import time
import traceback
from typing import Callable, Dict, List, Optional, Tuple

import pygame

from .config import Config
from .ctre import STATES
from .events import SOURCES
from .logger import save_run
from .scenarios import SCENARIOS
from .simulation import Simulation

CW, CH = 1920, 1080                    # design canvas (scaled to the window)
GUI_LOG_DIR = os.path.join("results", "gui_runs")

# ---------------------------------------------------------------- layout
TOP_H = 64
LEFT = pygame.Rect(16, 80, 352, CH - 96)
CENTER_X, CENTER_W = 384, 1040
RIGHT_X, RIGHT_W = 1440, 464
CELL = 40

# ---------------------------------------------------------------- palette
C = {
    "bg": (9, 14, 26), "panel": (16, 24, 42), "panel2": (21, 31, 54),
    "border": (36, 49, 77), "text": (230, 237, 247), "dim": (148, 163, 184),
    "faint": (100, 116, 139), "map": (12, 20, 37), "grid": (27, 38, 62),
    "obstacle": (51, 65, 92), "obstacle_edge": (76, 94, 128), "boundary": (120, 136, 164),
    "blue": (59, 130, 246), "blue_soft": (37, 64, 122), "green": (34, 197, 94),
    "amber": (245, 166, 35), "amber_light": (252, 211, 77), "red": (239, 68, 68),
    "ref": (120, 134, 160), "trail": (96, 165, 250), "monitored": (129, 140, 248),
    "btn": (30, 42, 70), "btn_hover": (41, 56, 92), "btn_text": (220, 228, 240),
}
STATE_COLOR = {"NORMAL": C["green"], "UNCERTAIN": C["amber_light"],
               "INVESTIGATE": C["amber"], "HIGH_ALERT": C["red"]}
STATE_PLAIN = {"NORMAL": "No supported event", "UNCERTAIN": "Weak, unreliable or conflicting",
               "INVESTIGATE": "Corroborated by 2+ sources", "HIGH_ALERT": "Physical + cyber agree"}
MODE_COLOR = {"MOVING": C["green"], "REROUTING": C["blue"], "WAITING": C["amber_light"],
              "INVESTIGATING": C["amber"], "REPOSITIONING": C["amber"], "STOPPED": C["red"],
              "SAFE_STOP": C["red"], "ARRIVED": C["green"], "READY": C["dim"]}
ACTION_COLOR = {"CONTINUE": C["green"], "ARRIVED": C["green"], "REROUTE": C["blue"],
                "WAIT": C["amber_light"], "INVESTIGATE": C["amber"],
                "MOVE_TO_SAFE_POINT": C["amber"], "STOP_ALERT": C["red"], "HOLD": C["red"],
                "SAFE_STOP": C["red"]}

# What the audience should notice. Presentation guidance only; it restates the
# documented scenario behaviour (scenarios.py / README) and is labelled as such.
WATCH = {
    "A": "The rover follows the planned route to G. CTRE should stay NORMAL and raise no alert.",
    "B": "Camera and range sensor both report the staged marker in Z1. CTRE moves to "
         "INVESTIGATE, the rover holds outside Z1 with a local alert, then continues.",
    "C": "Stale, duplicate, invalid and implausible records are REJECTED in the event feed. "
         "Watch the affected trust bars drop while CTRE waits in UNCERTAIN instead of alerting.",
    "D": "The camera says 'object in Z1', the range sensor says 'Z1 clear'. CTRE exposes the "
         "conflict as UNCERTAIN and the rover waits. No source is assumed correct.",
    "E": "The dashed shortest path crosses exposure zone Z1. ATNO chooses the longer northern "
         "route with zero exposure.",
    "F": "The only corridor crosses a high-exposure zone. No route is within the limit, so the "
         "rover makes a SAFE STOP and shows why.",
    "G": "Camera, range sensor and door-controller telemetry agree. CTRE raises HIGH_ALERT, "
         "Z1 becomes no-go and the rover reroutes around it.",
}
FLAG_LABEL = {"stale": "STALE", "duplicate": "DUPLICATE", "invalid_value": "INVALID",
              "implausible": "IMPLAUSIBLE", "delayed": "DELAYED", "missing": "MISSING"}


def _font(names: str, size: int, bold: bool = False):
    win = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts")
    files = {("segoe", False): "segoeui.ttf", ("segoe", True): "segoeuib.ttf",
             ("semibold", False): "seguisb.ttf", ("mono", False): "consola.ttf",
             ("mono", True): "consolab.ttf"}
    key = (names, bold)
    if key in files and os.path.exists(os.path.join(win, files[key])):
        return pygame.font.Font(os.path.join(win, files[key]), size)
    fallback = {"segoe": "segoeui,dejavusans,helvetica,arial",
                "semibold": "segoeuisemibold,segoeui,dejavusans,arial",
                "mono": "consolas,dejavusansmono,menlo,couriernew"}[names]
    return pygame.font.SysFont(fallback, size, bold=bold)


class App:
    def __init__(self, scenario: str, seed: int, method: str, config: Config,
                 window_size: Optional[Tuple[int, int]] = None):
        if sys.platform == "win32":
            try:   # crisp rendering on scaled Windows displays
                import ctypes
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        pygame.init()
        pygame.display.set_caption("CACPDF — simulation proof of concept")
        self.fullscreen = False
        self.window = self._open_window(window_size)
        self.canvas = pygame.Surface((CW, CH))
        self.screen = self.canvas          # kept for callers that read the rendered frame
        self.clock = pygame.time.Clock()
        self.F = {
            "xs": _font("segoe", 14), "s": _font("segoe", 16), "m": _font("segoe", 18),
            "l": _font("segoe", 22), "sb": _font("semibold", 16), "sbm": _font("semibold", 18),
            "b": _font("segoe", 18, True), "bl": _font("segoe", 24, True),
            "bxl": _font("segoe", 30, True), "title": _font("segoe", 26, True),
            "cap": _font("semibold", 13), "mono": _font("mono", 15),
            "monos": _font("mono", 14), "monob": _font("mono", 16, True),
            "axis": _font("mono", 12),
        }
        self._tcache: Dict[tuple, pygame.Surface] = {}
        self.config = config
        self.scenario, self.seed, self.method = scenario.upper(), seed, method
        self.running = False
        self.steps_per_sec = 3.0
        self.acc = 0.0
        self.show_heartbeats = False
        self.message, self.message_t = "", 0.0
        self.banner, self.banner_t = "", -10.0
        self.saved_path: Optional[str] = None
        self.error: Optional[str] = None
        self.prev_pos = None
        self.sim: Optional[Simulation] = None
        self.hits: List[Tuple[pygame.Rect, Callable[[], None]]] = []
        self.mouse = (-1, -1)
        self.new_sim()

    # ------------------------------------------------------------ window
    def _open_window(self, size=None):
        if size is None:
            info = pygame.display.Info()
            sw, sh = info.current_w or CW, info.current_h or CH
            scale = min(1.0, (sw * 0.96) / CW, (sh * 0.90) / CH)
            size = (int(CW * scale), int(CH * scale))
        return pygame.display.set_mode(size, pygame.RESIZABLE)

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        if self.fullscreen:
            self.window = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            self.window = self._open_window()

    def _view(self):
        """Scale and offset that letterbox the canvas into the window."""
        ww, wh = self.window.get_size()
        s = min(ww / CW, wh / CH)
        return s, (ww - CW * s) / 2, (wh - CH * s) / 2

    def to_canvas(self, pos):
        s, ox, oy = self._view()
        return (pos[0] - ox) / s, (pos[1] - oy) / s

    # ------------------------------------------------------------ control
    def new_sim(self, announce: str = "Reset"):
        try:
            self.sim = Simulation(self.scenario, self.seed, self.method, self.config,
                                  run_tag="gui" + time.strftime("%Y%m%d-%H%M%S"))
            self.error = None
        except Exception as exc:
            self.error = f"Could not start scenario {self.scenario}: {exc}"
            traceback.print_exc()
        self.running = False
        self.acc = 0.0
        self.saved_path = None
        self.prev_pos = self.sim.rover.pos if self.sim else None
        if self.sim:
            s = self.sim.scenario
            self.banner = (f"{announce}:  Scenario {s.key} · {s.name}  ·  seed {self.seed}  ·  "
                           f"{self.method}  ·  step 0")
            self.banner_t = time.time()

    def flash(self, msg: str):
        self.message, self.message_t = msg, time.time()

    def set_scenario(self, key: str):
        self.scenario = key
        self.new_sim("Loaded")

    def set_method(self, method: str):
        if method != self.method:
            self.method = method
            self.new_sim("Method changed")

    def toggle_method(self):
        self.set_method("baseline" if self.method == "proposed" else "proposed")

    def change_seed(self, d: int):
        self.seed = max(0, self.seed + d)
        self.new_sim("Seed changed")

    def toggle_run(self):
        if self.sim and not self.sim.done:
            self.running = not self.running

    def do_step(self):
        if not self.sim or self.sim.done:
            return
        try:
            self.prev_pos = self.sim.rover.pos
            self.sim.step()
        except Exception as exc:
            self.error = f"Simulation error at step {self.sim.t}: {exc}"
            self.running = False
            traceback.print_exc()
            return
        if self.sim.done:
            self.running = False
            self.save_log()

    def save_log(self):
        if not self.sim:
            return
        try:
            paths = save_run(self.sim, GUI_LOG_DIR)
            self.saved_path = paths["json"]
            self.flash(f"Log saved: {paths['json']}")
        except OSError as exc:
            self.flash(f"Could not save log: {exc}")

    def inject(self, kind: str):
        if not self.sim or self.sim.done:
            return
        try:
            self.flash(self.sim.inject(kind) + " — run marked as manually injected")
        except ValueError as exc:
            self.flash(str(exc))

    def speed(self, f: float):
        self.steps_per_sec = max(0.5, min(20.0, self.steps_per_sec * f))

    def toggle_hb(self):
        self.show_heartbeats = not self.show_heartbeats

    # ------------------------------------------------------------ primitives
    def txt(self, s, font="s", color=None) -> pygame.Surface:
        key = (str(s), font, color or C["text"])
        img = self._tcache.get(key)
        if img is None:
            if len(self._tcache) > 4000:
                self._tcache.clear()
            img = self.F[font].render(str(s), True, color or C["text"])
            self._tcache[key] = img
        return img

    def text(self, s, pos, font="s", color=None, anchor="topleft") -> pygame.Rect:
        img = self.txt(s, font, color)
        r = img.get_rect(**{anchor: (int(pos[0]), int(pos[1]))})
        self.canvas.blit(img, r)
        return r

    def fit(self, s: str, font: str, width: int) -> str:
        f = self.F[font]
        if f.size(s)[0] <= width:
            return s
        while s and f.size(s + "…")[0] > width:
            s = s[:-1]
        return s + "…"

    def wrap(self, s: str, font: str, width: int, max_lines: int = 99) -> List[str]:
        f = self.F[font]
        words, lines, cur = str(s).split(" "), [], ""
        for w in words:
            trial = (cur + " " + w).strip()
            if f.size(trial)[0] <= width:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = self.fit(lines[-1] + " …", font, width)
        return [self.fit(l, font, width) for l in lines]

    def para(self, s, pos, width, font="s", color=None, max_lines=99, lh=None) -> int:
        lh = lh or int(self.F[font].get_height() * 1.18)
        y = pos[1]
        for ln in self.wrap(s, font, width, max_lines):
            self.text(ln, (pos[0], y), font, color)
            y += lh
        return y

    def panel(self, rect, title=None, right=None) -> pygame.Rect:
        r = pygame.Rect(rect)
        pygame.draw.rect(self.canvas, C["panel"], r, border_radius=10)
        pygame.draw.rect(self.canvas, C["border"], r, 1, border_radius=10)
        if title:
            self.text(title.upper(), (r.x + 16, r.y + 12), "cap", C["dim"])
        if right:
            self.text(right, (r.right - 16, r.y + 11), "xs", C["faint"], "topright")
        return r

    def pill(self, label, center_or_pos, bg, fg=(10, 14, 24), font="sb", pad=(12, 5),
             anchor="center", outline=False) -> pygame.Rect:
        img = self.txt(label, font, fg)
        r = pygame.Rect(0, 0, img.get_width() + 2 * pad[0], img.get_height() + 2 * pad[1])
        setattr(r, anchor, (int(center_or_pos[0]), int(center_or_pos[1])))
        if outline:
            pygame.draw.rect(self.canvas, bg, r, 2, border_radius=r.h // 2)
        else:
            pygame.draw.rect(self.canvas, bg, r, border_radius=r.h // 2)
        self.canvas.blit(img, img.get_rect(center=r.center))
        return r

    def button(self, rect, label, action, active=False, enabled=True, key=None,
               font="sb", primary=False):
        r = pygame.Rect(rect)
        hover = enabled and r.collidepoint(self.mouse)
        if active or primary:
            bg = C["blue"] if enabled else C["btn"]
        else:
            bg = C["btn_hover"] if hover else C["btn"]
        if hover and (active or primary):
            bg = tuple(min(255, v + 18) for v in bg)
        pygame.draw.rect(self.canvas, bg, r, border_radius=8)
        if not (active or primary):
            pygame.draw.rect(self.canvas, C["border"], r, 1, border_radius=8)
        fg = (255, 255, 255) if (active or primary) else C["btn_text"]
        if not enabled:
            fg = C["faint"]
        if key:
            kimg = self.txt(key, "cap", C["dim"] if not (active or primary) else (220, 232, 255))
            kr = kimg.get_rect(midright=(r.right - 10, r.centery))
            box = kr.inflate(10, 6)
            pygame.draw.rect(self.canvas, C["panel"] if not (active or primary) else (37, 99, 200),
                             box, border_radius=4)
            self.canvas.blit(kimg, kr)
            self.text(label, (r.x + (r.w - box.w - 6) / 2 + 2, r.centery), font, fg, "center")
        else:
            self.text(label, r.center, font, fg, "center")
        if enabled:
            self.hits.append((r, action))
        return r

    def bar(self, rect, value, color, tick=None):
        r = pygame.Rect(rect)
        pygame.draw.rect(self.canvas, C["panel2"], r, border_radius=r.h // 2)
        w = int(r.w * max(0.0, min(1.0, value)))
        if w > 0:
            pygame.draw.rect(self.canvas, color, (r.x, r.y, max(w, r.h), r.h),
                             border_radius=r.h // 2)
        if tick is not None:
            x = r.x + int(r.w * tick)
            pygame.draw.line(self.canvas, C["text"], (x, r.y - 3), (x, r.bottom + 2), 2)

    def dashed(self, pts, color, width=2, dash=8, gap=6):
        for a, b in zip(pts, pts[1:]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            L = math.hypot(dx, dy)
            if L == 0:
                continue
            d = 0.0
            while d < L:
                e = min(L, d + dash)
                pygame.draw.line(self.canvas, color, (a[0] + dx * d / L, a[1] + dy * d / L),
                                 (a[0] + dx * e / L, a[1] + dy * e / L), width)
                d += dash + gap

    # ------------------------------------------------------------ top bar
    def draw_top(self):
        sim = self.sim
        self.text("CACPDF", (20, 12), "title", C["text"])
        self.text("Cognitive Autonomous Cyber-Physical Defense Framework", (20, 42), "xs",
                  C["dim"])
        self.pill("SIMULATION ONLY · not a deployed security system", (400, 32), C["amber"],
                  C["amber"], "cap", (12, 6), "midleft", outline=True)
        if not sim:
            return
        x = CW - 20
        status = "DONE" if sim.done else ("RUNNING" if self.running else
                                          ("READY" if sim.t == 0 else "PAUSED"))
        scol = {"RUNNING": C["green"], "PAUSED": C["amber_light"], "READY": C["dim"],
                "DONE": C["blue"]}[status]
        r = self.pill(f"● {status}", (x, 32), C["panel2"], scol, "sb", (14, 7), "midright")
        x = r.left - 10
        r = self.pill(f"Step {sim.t} / {sim.cfg.sim.max_steps}", (x, 32), C["panel2"],
                      C["text"], "monob", (14, 7), "midright")
        x = r.left - 10
        mlabel = "Method: Proposed (ADTA+CTRE+ATNO)" if sim.method == "proposed" else \
            "Method: Baseline (fixed threshold)"
        r = self.pill(mlabel, (x, 32), C["panel2"], C["text"], "sb", (14, 7), "midright")
        x = r.left - 10
        r = self.pill(f"Seed {sim.seed}", (x, 32), C["panel2"], C["text"], "sb", (14, 7),
                      "midright")
        x = r.left - 10
        self.pill(f"Scenario {sim.scenario.key} · {sim.scenario.name}", (x, 32), C["blue"],
                  (255, 255, 255), "sb", (14, 7), "midright")

    # ------------------------------------------------------------ left column
    def draw_left(self):
        sim = self.sim
        x, w = LEFT.x, LEFT.w
        # scenarios
        p = self.panel((x, 80, w, 352), "Scenario", "keys 1–7")
        y = p.y + 40
        for i, (k, s) in enumerate(SCENARIOS.items()):
            on = k == self.scenario
            r = pygame.Rect(p.x + 12, y, w - 24, 40)
            hover = r.collidepoint(self.mouse)
            pygame.draw.rect(self.canvas, C["blue_soft"] if on else
                             (C["btn_hover"] if hover else C["panel2"]), r, border_radius=8)
            if on:
                pygame.draw.rect(self.canvas, C["blue"], r, 2, border_radius=8)
            badge = pygame.Rect(r.x + 8, r.y + 7, 26, 26)
            pygame.draw.rect(self.canvas, C["blue"] if on else C["btn"], badge, border_radius=6)
            self.text(k, badge.center, "b", (255, 255, 255), "center")
            self.text(self.fit(s.name, "sb", r.w - 60), (r.x + 44, r.centery), "sb",
                      C["text"] if on else C["btn_text"], "midleft")
            self.hits.append((r, (lambda kk=k: lambda: self.set_scenario(kk))()))
            y += 44
        # run controls
        p = self.panel((x, 444, w, 284), "Run controls")
        y = p.y + 40
        done = bool(sim and sim.done)
        label = "Run finished — press Reset" if done else ("Pause" if self.running else "Start")
        self.button((p.x + 12, y, w - 24, 50), label, self.toggle_run, active=self.running,
                    enabled=not done, key="SPACE", font="b", primary=not done)
        y += 60
        half = (w - 24 - 10) // 2
        self.button((p.x + 12, y, half, 40), "Step", self.do_step, enabled=not done, key="N")
        self.button((p.x + 22 + half, y, half, 40), "Reset", lambda: self.new_sim("Reset"),
                    key="R")
        y += 52
        self.text("Method", (p.x + 16, y + 10), "s", C["dim"])
        seg_x, seg_w = p.x + 96, (w - 108) // 2
        self.button((seg_x, y, seg_w, 36), "Proposed", lambda: self.set_method("proposed"),
                    active=self.method == "proposed", font="s")
        self.button((seg_x + seg_w + 4, y, seg_w - 4, 36), "Baseline",
                    lambda: self.set_method("baseline"), active=self.method == "baseline",
                    font="s")
        y += 46
        self.text("Seed", (p.x + 16, y + 8), "s", C["dim"])
        self.button((p.x + 96, y, 36, 34), "−", lambda: self.change_seed(-1), font="b")
        self.text(str(self.seed), (p.x + 96 + 36 + 46, y + 17), "monob", C["text"], "center")
        self.button((p.x + 96 + 128, y, 36, 34), "+", lambda: self.change_seed(1), font="b")
        self.text("[ ]", (p.right - 16, y + 9), "cap", C["faint"], "topright")
        y += 44
        self.text("Speed", (p.x + 16, y + 8), "s", C["dim"])
        self.button((p.x + 96, y, 36, 34), "−", lambda: self.speed(1 / 1.5), font="b")
        self.text(f"{self.steps_per_sec:.1f}/s", (p.x + 96 + 36 + 46, y + 17), "monob",
                  C["text"], "center")
        self.button((p.x + 96 + 128, y, 36, 34), "+", lambda: self.speed(1.5), font="b")
        self.text("− +", (p.right - 16, y + 9), "cap", C["faint"], "topright")
        # what to watch
        p = self.panel((x, 740, w, 206), "What to watch", "guidance")
        if sim:
            y = self.para(WATCH.get(sim.scenario.key, sim.scenario.description),
                          (p.x + 16, p.y + 38), w - 32, "s", C["text"], 5)
            self.para("Expected: " + sim.scenario.expected_text, (p.x + 16, y + 6), w - 32,
                      "xs", C["dim"], 3)
        # inject faults
        p = self.panel((x, 958, w, CH - 16 - 958), "Inject a fault (live demo)", "F1–F4")
        bw = (w - 24 - 8) // 2
        for i, (k, lab) in enumerate((("stale", "Stale record"), ("duplicate", "Duplicate"),
                                      ("invalid", "Invalid value"), ("conflict", "Conflict"))):
            bx = p.x + 12 + (i % 2) * (bw + 8)
            by = p.y + 36 + (i // 2) * 40
            self.button((bx, by, bw, 34), lab, (lambda kk=k: lambda: self.inject(kk))(),
                        enabled=not done, font="s")

    # ------------------------------------------------------------ map
    def map_geom(self):
        w = self.sim.world
        mw, mh = w.width * CELL, w.height * CELL
        ox = CENTER_X + (CENTER_W - mw) // 2 + 8
        oy = 80 + 62
        return ox, oy, mw, mh

    def cc(self, c, frac_from=None, frac=1.0):
        ox, oy, _, _ = self.map_geom()
        x, y = c
        if frac_from is not None and frac < 1.0:
            x = frac_from[0] + (c[0] - frac_from[0]) * frac
            y = frac_from[1] + (c[1] - frac_from[1]) * frac
        return ox + x * CELL + CELL / 2, oy + y * CELL + CELL / 2

    def initial_plan(self):
        sim = self.sim
        return sim.threat_plan_initial if sim.method == "proposed" else sim.reference_plan

    def draw_map(self):
        sim, w, cv = self.sim, self.sim.world, self.canvas
        ox, oy, mw, mh = self.map_geom()
        p = self.panel((CENTER_X, 80, CENTER_W, 706))
        self.text("SIMULATION MAP", (p.x + 16, p.y + 12), "cap", C["dim"])
        self.text(f"{w.width} × {w.height} cells · edge = hard boundary", (p.x + 140, p.y + 11),
                  "xs", C["faint"])
        # rover state + waypoint, top-right of the map panel
        rv = sim.rover
        nxt = sim._route_next()
        mcol = MODE_COLOR.get(rv.mode, C["dim"])
        r = self.pill(f"Rover: {rv.mode}", (p.right - 16, p.y + 22), mcol, (10, 14, 24), "b",
                      (14, 5), "midright")
        self.text(f"pos {rv.pos}   heading {rv.heading:.0f}°   next waypoint "
                  f"{nxt if nxt else '—'}", (r.left - 14, p.y + 22), "mono", C["text"],
                  "midright")
        # axis labels
        for x in range(w.width):
            if x % 2 == 0:
                self.text(str(x), (ox + x * CELL + CELL / 2, oy - 12), "axis", C["faint"],
                          "center")
        for y in range(w.height):
            self.text(str(y), (ox - 12, oy + y * CELL + CELL / 2), "axis", C["faint"], "center")
        pygame.draw.rect(cv, C["map"], (ox, oy, mw, mh))
        for x in range(w.width + 1):
            pygame.draw.line(cv, C["grid"], (ox + x * CELL, oy), (ox + x * CELL, oy + mh))
        for y in range(w.height + 1):
            pygame.draw.line(cv, C["grid"], (ox, oy + y * CELL), (ox + mw, oy + y * CELL))
        # zones
        overlay = pygame.Surface((mw, mh), pygame.SRCALPHA)
        for zid, z in w.zones.items():
            dyn = sim.dynamic.get(zid)
            if dyn and dyn["prohibited"]:
                col, a, hatch = C["red"], 70, True
            elif dyn:
                col, a, hatch = C["amber"], 60, False
            elif z.exposure > 0:
                col, a, hatch = C["amber"], int(min(85, 30 + 18 * z.exposure)), True
            else:
                col, a, hatch = C["monitored"], 34, False
            for (cx, cy) in z.cells:
                pygame.draw.rect(overlay, (*col, a), (cx * CELL, cy * CELL, CELL, CELL))
                if hatch:
                    pygame.draw.line(overlay, (*col, 120), (cx * CELL, cy * CELL + CELL),
                                     (cx * CELL + CELL, cy * CELL), 1)
            # outline around the zone's bounding cells
            if z.cells:
                xs = [c[0] for c in z.cells]
                ys = [c[1] for c in z.cells]
                pygame.draw.rect(overlay, (*col, 200), (min(xs) * CELL, min(ys) * CELL,
                                                        (max(xs) - min(xs) + 1) * CELL,
                                                        (max(ys) - min(ys) + 1) * CELL), 2)
        cv.blit(overlay, (ox, oy))
        for (x, y) in w.obstacles:
            rr = pygame.Rect(ox + x * CELL + 1, oy + y * CELL + 1, CELL - 1, CELL - 1)
            pygame.draw.rect(cv, C["obstacle"], rr)
        for (x, y) in w.obstacles:   # subtle edge only where an obstacle meets free space
            for dx, dy, a, b in ((0, -1, (0, 0), (1, 0)), (0, 1, (0, 1), (1, 1)),
                                 (-1, 0, (0, 0), (0, 1)), (1, 0, (1, 0), (1, 1))):
                if (x + dx, y + dy) not in w.obstacles and w.in_bounds((x + dx, y + dy)):
                    pygame.draw.line(cv, C["obstacle_edge"],
                                     (ox + (x + a[0]) * CELL, oy + (y + a[1]) * CELL),
                                     (ox + (x + b[0]) * CELL, oy + (y + b[1]) * CELL), 1)
        pygame.draw.rect(cv, C["boundary"], (ox - 2, oy - 2, mw + 4, mh + 4), 2)
        for zid, z in w.zones.items():
            if not z.cells:
                continue
            dyn = sim.dynamic.get(zid)
            tag = "NO-GO (alert)" if dyn and dyn["prohibited"] else \
                ("+ alert cost" if dyn else (f"exposure {z.exposure:g}/cell" if z.exposure
                                             else "monitored"))
            xs = [c[0] for c in z.cells]
            ys = [c[1] for c in z.cells]
            col = C["red"] if dyn and dyn["prohibited"] else \
                (C["amber"] if (dyn or z.exposure) else C["monitored"])
            self.pill(f"{zid} · {tag}", (ox + min(xs) * CELL, oy + min(ys) * CELL - 4), col,
                      (10, 14, 24), "cap", (8, 3), "bottomleft")
        # start / goal
        sx, sy = self.cc(w.start)
        pygame.draw.rect(cv, C["green"], (sx - 15, sy - 15, 30, 30), 3, border_radius=6)
        self.text("S", (sx, sy), "b", C["green"], "center")
        gx, gy = self.cc(w.goal)
        pygame.draw.circle(cv, C["blue"], (gx, gy), 16, 3)
        pygame.draw.circle(cv, C["blue"], (gx, gy), 6)
        self.text("GOAL", (gx, gy + 26), "cap", C["blue"], "center")
        # routes
        ref = sim.reference_plan
        if ref.ok:
            self.dashed([self.cc(c) for c in ref.path], C["ref"], 2)
        if sim.plan and sim.plan.rejected_path:
            self.dashed([self.cc(c) for c in sim.plan.rejected_path], C["red"], 3, 6, 6)
        for c in sim.rover.trail[:-1]:
            pygame.draw.circle(cv, C["trail"], self.cc(c), 3)
        route = sim.route
        if not route and sim.t == 0:      # before the first step: the plan computed at reset
            init = self.initial_plan()
            route = init.path if init.ok else []
            if not init.ok and init.rejected_path:
                self.dashed([self.cc(c) for c in init.rejected_path], C["red"], 3, 6, 6)
        if route and sim.rover.pos in route:
            rest = route[route.index(sim.rover.pos):]
            if len(rest) > 1:
                pts = [self.cc(c) for c in rest]
                pygame.draw.lines(cv, C["blue_soft"], False, pts, 9)
                pygame.draw.lines(cv, C["blue"], False, pts, 4)
                pygame.draw.circle(cv, C["text"], self.cc(rest[1]), 11, 2)
        # recent evidence markers (records received in the last 4 steps)
        for o in list(sim.recent_obs)[:30]:
            if sim.t - o["received_step"] > 4 or not o.get("event_id"):
                continue
            ev = next((e for e in sim.events if e.event_id == o["event_id"]), None)
            if ev is None or ev.polarity == 0:
                continue
            cell = ev.value.get("cell")
            if cell is None:
                z = w.zones.get(ev.zone_id or "")
                if not z or not z.cells:
                    continue
                cell = max(z.cells)
            cx, cy = self.cc(tuple(cell))
            col = C["green"] if o["accepted"] else C["red"]
            if ev.polarity > 0:
                pts = [(cx, cy - 9), (cx + 9, cy), (cx, cy + 9), (cx - 9, cy)]
                pygame.draw.polygon(cv, col, pts, 0 if o["accepted"] else 3)
            else:
                pygame.draw.line(cv, col, (cx - 8, cy - 8), (cx + 8, cy + 8), 3)
                pygame.draw.line(cv, col, (cx - 8, cy + 8), (cx + 8, cy - 8), 3)
        # rover (interpolated between cells while running)
        frac = min(1.0, self.acc * self.steps_per_sec) if self.running else 1.0
        rx, ry = self.cc(sim.rover.pos, self.prev_pos, frac)
        pygame.draw.circle(cv, mcol, (rx, ry), 16)
        pygame.draw.circle(cv, (10, 14, 24), (rx, ry), 16, 3)
        h = math.radians(sim.rover.heading)
        tip = (rx + math.cos(h) * 13, ry + math.sin(h) * 13)
        l = (rx + math.cos(h + 2.4) * 9, ry + math.sin(h + 2.4) * 9)
        rr_ = (rx + math.cos(h - 2.4) * 9, ry + math.sin(h - 2.4) * 9)
        pygame.draw.polygon(cv, (10, 14, 24), [tip, l, rr_])
        # overlays: final outcome, ready hint, load/reset banner
        if sim.done:
            d = sim.decision
            ok = d.action == "ARRIVED"
            msg = (f"GOAL REACHED in {sim.t} steps" if ok else f"SAFE STOP at step {sim.t}: "
                   f"{d.reason}")
            lines = self.wrap(msg, "bl", mw - 60, 2)
            bh = 18 + 32 * len(lines)
            box = pygame.Rect(ox + 16, oy + 14, mw - 32, bh)
            pygame.draw.rect(cv, C["green"] if ok else C["red"], box, border_radius=10)
            for i, ln in enumerate(lines):
                self.text(ln, (box.x + 20, box.y + 9 + 32 * i), "bl", (10, 14, 24))
        elif sim.t == 0:
            hint = "Ready.  Press Start (SPACE) to run, or Step (N) to advance one step."
            box = pygame.Rect(ox + 16, oy + mh - 56, mw - 32, 40)
            s = pygame.Surface(box.size, pygame.SRCALPHA)
            pygame.draw.rect(s, (9, 14, 26, 215), s.get_rect(), border_radius=10)
            cv.blit(s, box)
            self.text(hint, box.center, "sbm", C["text"], "center")
        age = time.time() - self.banner_t
        if self.banner and age < 2.6 and not sim.done:
            a = 1.0 if age < 2.0 else max(0.0, 1 - (age - 2.0) / 0.6)
            img = self.txt(self.banner, "bl", (255, 255, 255))
            box = img.get_rect(center=(ox + mw / 2, oy + 56)).inflate(40, 22)
            s = pygame.Surface(box.size, pygame.SRCALPHA)
            pygame.draw.rect(s, (*C["blue"], int(235 * a)), s.get_rect(), border_radius=12)
            cv.blit(s, box)
            img2 = img.copy()
            img2.set_alpha(int(255 * a))
            cv.blit(img2, img2.get_rect(center=box.center))
        # legend
        ly = p.bottom - 34
        x = p.x + 18
        planner = "ATNO" if sim.method == "proposed" else "obstacle-only"
        items = [("route", f"Route ({planner})"), ("ref", "Shortest (reference)"),
                 ("rej", "Rejected route"), ("trail", "Travelled"),
                 ("zone_e", "Exposure"), ("zone_m", "Monitored"),
                 ("nogo", "No-go"), ("acc", "Accepted"), ("rejr", "Rejected")]
        for key, lab in items:
            cy = ly + 10
            if key == "route":
                pygame.draw.line(cv, C["blue"], (x, cy), (x + 22, cy), 4)
            elif key == "ref":
                self.dashed([(x, cy), (x + 22, cy)], C["ref"], 2, 6, 4)
            elif key == "rej":
                self.dashed([(x, cy), (x + 22, cy)], C["red"], 3, 6, 4)
            elif key == "trail":
                for k in range(3):
                    pygame.draw.circle(cv, C["trail"], (x + 4 + 7 * k, cy), 3)
            elif key in ("zone_e", "zone_m", "nogo"):
                col = {"zone_e": C["amber"], "zone_m": C["monitored"], "nogo": C["red"]}[key]
                pygame.draw.rect(cv, col, (x, cy - 7, 22, 14), 2)
            elif key == "acc":
                pygame.draw.polygon(cv, C["green"], [(x + 11, cy - 7), (x + 18, cy),
                                                     (x + 11, cy + 7), (x + 4, cy)])
            else:
                pygame.draw.polygon(cv, C["red"], [(x + 11, cy - 7), (x + 18, cy),
                                                   (x + 11, cy + 7), (x + 4, cy)], 2)
            x += 28
            x = self.text(lab, (x, ly + 1), "xs", C["dim"]).right + 14

    # ------------------------------------------------------------ event feed
    def draw_feed(self):
        sim = self.sim
        note = "newest first · heartbeats " + ("shown" if self.show_heartbeats else "hidden")
        note += " (H)"
        if sim.method == "baseline":
            note = "status = fusion checks, which the baseline does NOT apply · " + note
        p = self.panel((CENTER_X, 798, CENTER_W, CH - 16 - 798), "Event feed — simulated records",
                       note)
        cols = [("STEP", 64), ("SRC T", 64), ("SOURCE", 128), ("TYPE", 170), ("ZONE", 56),
                ("CONF", 64), ("AGE", 50), ("STATUS", 330), ("TRUST", 70)]
        x0, y = p.x + 18, p.y + 38
        x = x0
        for name, wd in cols:
            self.text(name, (x, y), "cap", C["faint"])
            x += wd
        y += 22
        rows = [o for o in sim.recent_obs
                if self.show_heartbeats or o["event_type"] != "status" or o["flags"]]
        if not rows:
            if sim.t == 0:
                msg = "No records received yet — press Start or Step."
            elif sim.done:
                msg = "No zone reports were received in this run (heartbeats hidden — press H)."
            else:
                msg = "No zone reports yet. Status heartbeats are hidden (press H to show)."
            self.text(msg, (p.centerx, p.y + 130), "m", C["faint"], "center")
            return
        row_h = 26
        max_rows = (p.bottom - 10 - y) // row_h
        for o in rows[:max_rows]:
            rejected = not o["accepted"]
            missing = o["event_type"] == "heartbeat_timeout"
            if rejected and not missing:
                pygame.draw.rect(self.canvas, (64, 22, 30), (p.x + 8, y - 3, p.w - 16, row_h - 2),
                                 border_radius=5)
            vals = [str(o["received_step"]), str(o["step"]), o["source_id"],
                    o["event_type"], o["zone_id"] or "—",
                    "—" if o["confidence"] is None else f"{o['confidence']:.2f}",
                    str(o["age_at_arrival"])]
            x = x0
            for (name, wd), v in zip(cols, vals):
                self.text(self.fit(v, "monos", wd - 8), (x, y), "monos", C["text"])
                x += wd
            # status badges
            bx = x
            if missing:
                bx = self.pill("MISSING", (bx, y + 9), C["amber"], (10, 14, 24), "cap", (8, 3),
                               "midleft").right + 8
                self.text("no message within timeout; not treated as 'clear'", (bx, y), "xs",
                          C["amber_light"])
            elif rejected:
                bx = self.pill("REJECTED", (bx, y + 9), C["red"], (255, 255, 255), "cap",
                               (8, 3), "midleft").right + 6
                for f in o["flags"]:
                    bx = self.pill(FLAG_LABEL.get(f, f.upper()), (bx, y + 9), C["red"], C["red"],
                                   "cap", (7, 2), "midleft", outline=True).right + 5
            else:
                bx = self.pill("ACCEPTED", (bx, y + 9), C["green"], (10, 14, 24), "cap", (8, 3),
                               "midleft").right + 6
                for f in o["flags"]:
                    bx = self.pill(FLAG_LABEL.get(f, f.upper()), (bx, y + 9), C["amber"],
                                   C["amber"], "cap", (7, 2), "midleft", outline=True).right + 5
            x += cols[7][1]
            tr = o.get("trust_after")
            if tr not in (None, ""):
                self.text(f"{tr:.2f}", (x, y), "monos", C["dim"])
            y += row_h

    # ------------------------------------------------------------ right column
    def draw_status(self):
        sim = self.sim
        p = self.panel((RIGHT_X, 80, RIGHT_W, 196), "Run status")
        d = sim.decision
        if sim.done and d and d.action == "ARRIVED":
            out, col = "GOAL REACHED", C["green"]
        elif sim.done:
            out, col = "SAFE STOP", C["red"]
        elif sim.t == 0:
            out, col = "READY", C["dim"]
        else:
            out, col = "IN PROGRESS", C["text"]
        self.text(out, (p.x + 16, p.y + 34), "bxl", col)
        self.text(f"step {sim.t} of max {sim.cfg.sim.max_steps}", (p.right - 16, p.y + 44),
                  "mono", C["dim"], "topright")
        rem = sim._remaining_metrics()
        lab_d, lab_e = "Route distance left", "Route exposure cost left"
        if rem is None and sim.t == 0:
            init = self.initial_plan()
            if init.ok:
                rem = {"distance": init.distance, "exposure": init.exposure}
                lab_d, lab_e = "Planned route distance", "Planned exposure cost"
        tiles = [(lab_d, f"{rem['distance']:.1f}" if rem else "—", "cells"),
                 (lab_e, f"{rem['exposure']:.1f}" if rem else "—", ""),
                 ("Distance travelled", f"{sim.rover.distance:.1f}", "cells"),
                 ("Zone exposure incurred", f"{sim.static_exposure_incurred:.1f}", "")]
        tw = (RIGHT_W - 32 - 10) // 2
        for i, (lab, val, unit) in enumerate(tiles):
            tx = p.x + 16 + (i % 2) * (tw + 10)
            ty = p.y + 80 + (i // 2) * 54
            pygame.draw.rect(self.canvas, C["panel2"], (tx, ty, tw, 50), border_radius=8)
            self.text(lab, (tx + 10, ty + 4), "xs", C["dim"])
            r = self.text(val, (tx + 10, ty + 23), "b", C["text"])
            if unit and val != "—":
                self.text(unit, (r.right + 6, ty + 27), "xs", C["faint"])

    def draw_trust(self):
        sim = self.sim
        note = "" if sim.method == "proposed" else "not used by baseline"
        p = self.panel((RIGHT_X, 288, RIGHT_W, 258), "ADTA · trust score per source (0–1)", note)
        y = p.y + 40
        for s, meta in SOURCES.items():
            t = sim.adta.trust[s]
            init = sim.adta.initial[s]
            col = C["green"] if t >= 0.6 else (C["amber"] if t >= 0.35 else C["red"])
            self.text(s, (p.x + 16, y), "monob", C["text"])
            self.text(meta["type"], (p.x + 140, y + 1), "xs", C["faint"])
            self.bar((p.x + 210, y + 4, 170, 12), t, col, init)
            self.text(f"{t:.2f}", (p.right - 16, y), "monob", col, "topright")
            reason = sim.adta.last_reason.get(s, "")
            if s in sim.fusion.missing:
                reason = "SILENT (missing) · " + reason
            rcol = C["dim"] if reason in ("initial", "valid") or reason.startswith("valid") \
                else C["amber_light"]
            self.text(self.fit("last: " + reason, "xs", RIGHT_W - 48), (p.x + 16, y + 22), "xs",
                      rcol)
            y += 50
        self.text("Tick = starting trust.  Weight = trust × confidence × freshness.", (p.x + 16,
                  p.bottom - 24), "xs", C["faint"])

    def draw_ctre(self):
        sim = self.sim
        a = sim.assessment
        title = "CTRE · reasoning about the situation" if sim.method == "proposed" else \
            "Baseline · fixed confidence threshold"
        p = self.panel((RIGHT_X, 558, RIGHT_W, 300), title, "staged scenario labels")
        state = a.state if a else None
        # state ladder
        x, y = p.x + 16, p.y + 38
        seg_w = (RIGHT_W - 32 - 18) // 4
        for s in STATES:
            on = s == state
            r = pygame.Rect(x, y, seg_w, 30)
            pygame.draw.rect(self.canvas, STATE_COLOR[s] if on else C["panel2"], r,
                             border_radius=6)
            if sim.method == "baseline" and s == "UNCERTAIN":
                self.text("n/a", r.center, "cap", C["faint"], "center")
            else:
                self.text(s.replace("_", " "), r.center, "cap",
                          (10, 14, 24) if on else C["faint"], "center")
            x += seg_w + 6
        if not a:
            self.text("Not assessed yet — waiting for the first step.", (p.x + 16, p.y + 90),
                      "m", C["faint"])
            return
        y = p.y + 78
        self.text(STATE_PLAIN.get(state, "") if sim.method == "proposed" else
                  "Alert if any report has confidence ≥ threshold", (p.x + 16, y), "sb",
                  STATE_COLOR.get(state, C["text"]))
        y += 24
        self.text(self.fit(f"Rule: {a.rule}", "s", RIGHT_W - 32), (p.x + 16, y), "s", C["dim"])
        y += 26
        if sim.method == "proposed":
            self.text("Support", (p.x + 16, y), "xs", C["dim"])
            self.bar((p.x + 84, y + 4, 120, 10), a.s_pos, C["green"])
            self.text(f"{a.s_pos:.2f}", (p.x + 212, y), "monos", C["text"])
            self.text("Against", (p.x + 262, y), "xs", C["dim"])
            self.bar((p.x + 326, y + 4, 70, 10), a.s_neg, C["amber"])
            self.text(f"{a.s_neg:.2f}", (p.right - 16, y), "monos", C["text"], "topright")
            y += 26
        lines = []
        for e in a.supporting:
            if "weight" in e:
                lines.append((C["green"], f"+ {e['source_id']} {e['event_type']}  w {e['weight']:.2f}"))
            else:
                lines.append((C["green"], f"+ {e['source_id']}  conf {e['confidence']:.2f}"))
        for e in a.conflicting:
            lines.append((C["amber"], f"− {e['source_id']} {e['event_type']}  w {e['weight']:.2f}"
                                      "  (conflicts)"))
        for e in a.rejected:
            lines.append((C["red"], f"× {e['source_id']} {e['event_type']}  rejected: "
                                    f"{', '.join(FLAG_LABEL.get(f, f) for f in e['flags'])}"))
        for s in a.missing:
            lines.append((C["amber_light"], f"? {s} silent — coverage gap, not 'clear'"))
        if a.pending:
            lines.insert(0, (C["dim"], f"Persistence filter: holding {a.state}, raw {a.pending}"))
        if not lines:
            lines = [(C["faint"], "No zone evidence in the current window.")]
        max_lines = (p.bottom - 40 - y) // 21
        for col, ln in lines[:max_lines]:
            self.text(self.fit(ln, "monos", RIGHT_W - 32), (p.x + 16, y), "monos", col)
            y += 21
        if len(lines) > max_lines:
            self.text(f"+{len(lines) - max_lines} more in the run log", (p.x + 16, y), "xs",
                      C["faint"])
        sh = sim.shadow
        if sh:
            other = "fixed-threshold baseline" if sim.method == "proposed" else "proposed CTRE"
            self.text(f"Same inputs → {other}: {sh.state}", (p.x + 16, p.bottom - 28), "sb",
                      STATE_COLOR.get(sh.state, C["dim"]))

    def draw_response(self):
        sim = self.sim
        p = self.panel((RIGHT_X, 870, RIGHT_W, CH - 16 - 870), "Selected safe response",
                       "after safety check")
        d = sim.decision
        if not d:
            self.text("No action yet.", (p.x + 16, p.y + 44), "l", C["faint"])
            self.text("Allowed: continue · wait · investigate from a safe point · reroute ·",
                      (p.x + 16, p.y + 84), "xs", C["dim"])
            self.text("stop with alert · safe stop. No pursuit or interception exists.",
                      (p.x + 16, p.y + 104), "xs", C["dim"])
            return
        col = ACTION_COLOR.get(d.action, C["text"])
        r = self.pill(d.action.replace("_", " "), (p.x + 16, p.y + 52), col, (10, 14, 24), "bl",
                      (14, 4), "midleft")
        self.text(sim.rover.mode, (r.right + 12, p.y + 52), "sb", MODE_COLOR.get(
            sim.rover.mode, C["dim"]), "midleft")
        y = self.para(d.reason, (p.x + 16, p.y + 76), RIGHT_W - 32, "s", C["text"], 2)
        if d.override:
            self.text(self.fit(f"SAFETY OVERRIDE: {d.override}", "sb", RIGHT_W - 32),
                      (p.x + 16, y + 2), "sb", C["red"])
            y += 22
        if d.alert:
            self.text(self.fit(f"ALERT: {d.alert}", "sb", RIGHT_W - 32), (p.x + 16, y + 2), "sb",
                      C["amber"] if "local" in d.alert else C["red"])
            y += 22
        if sim.done:
            o = sim.outcome
            ok = o.get("expected_behavior_met")
            failed = [k for k, v in o["checks"].items() if v is False]
            msg = "Outcome matches the scenario's expected behaviour" if ok else \
                "Outcome differs from expected: " + ", ".join(failed)
            self.text(self.fit(msg, "sb", RIGHT_W - 32), (p.x + 16, p.bottom - 30), "sb",
                      C["green"] if ok else C["amber"])
        else:
            self.text(f"alerts raised: {len(sim.alerts)}    safety overrides: "
                      f"{sim.safety_overrides}", (p.x + 16, p.bottom - 28), "xs", C["dim"])

    # ------------------------------------------------------------ frame
    def draw(self):
        self.hits = []
        self.canvas.fill(C["bg"])
        self.draw_top()
        if self.sim:
            self.draw_left()
            self.draw_map()
            self.draw_feed()
            self.draw_status()
            self.draw_trust()
            self.draw_ctre()
            self.draw_response()
        if self.message and time.time() - self.message_t < 4:
            img = self.txt(self.message, "s", (255, 255, 255))
            box = img.get_rect(midbottom=(CENTER_X + CENTER_W / 2, 786 - 46)).inflate(24, 12)
            pygame.draw.rect(self.canvas, (40, 52, 82), box, border_radius=8)
            self.canvas.blit(img, img.get_rect(center=box.center))
        if self.error:
            box = pygame.Rect(CENTER_X + 40, 380, CENTER_W - 80, 110)
            pygame.draw.rect(self.canvas, C["red"], box, border_radius=10)
            self.para(self.error, (box.x + 20, box.y + 16), box.w - 40, "m", (255, 255, 255), 4)
        self.present()

    def present(self):
        s, ox, oy = self._view()
        self.window.fill((0, 0, 0))
        if abs(s - 1.0) < 1e-6:
            self.window.blit(self.canvas, (ox, oy))
        else:
            img = pygame.transform.smoothscale(self.canvas, (int(CW * s), int(CH * s)))
            self.window.blit(img, (ox, oy))
        pygame.display.flip()

    # ------------------------------------------------------------ loop
    def handle_key(self, key):
        keymap = {pygame.K_SPACE: self.toggle_run, pygame.K_n: self.do_step,
                  pygame.K_r: lambda: self.new_sim("Reset"), pygame.K_m: self.toggle_method,
                  pygame.K_LEFTBRACKET: lambda: self.change_seed(-1),
                  pygame.K_RIGHTBRACKET: lambda: self.change_seed(1),
                  pygame.K_MINUS: lambda: self.speed(1 / 1.5),
                  pygame.K_EQUALS: lambda: self.speed(1.5), pygame.K_PLUS: lambda: self.speed(1.5),
                  pygame.K_KP_MINUS: lambda: self.speed(1 / 1.5),
                  pygame.K_KP_PLUS: lambda: self.speed(1.5),
                  pygame.K_F1: lambda: self.inject("stale"),
                  pygame.K_F2: lambda: self.inject("duplicate"),
                  pygame.K_F3: lambda: self.inject("invalid"),
                  pygame.K_F4: lambda: self.inject("conflict"),
                  pygame.K_h: self.toggle_hb, pygame.K_l: self.save_log,
                  pygame.K_F11: self.toggle_fullscreen}
        for i, k in enumerate(SCENARIOS):
            keymap[pygame.K_1 + i] = (lambda kk=k: lambda: self.set_scenario(kk))()
        if key in keymap:
            keymap[key]()

    def tick(self, dt: float) -> bool:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return False
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    if self.fullscreen:
                        self.toggle_fullscreen()
                        continue
                    return False
                self.handle_key(ev.key)
            if ev.type == pygame.MOUSEMOTION:
                self.mouse = self.to_canvas(ev.pos)
            if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                pos = self.to_canvas(ev.pos)
                for r, action in self.hits:
                    if r.collidepoint(pos):
                        action()
                        break
            if ev.type == pygame.VIDEORESIZE and not self.fullscreen:
                self.window = pygame.display.set_mode(ev.size, pygame.RESIZABLE)
        if self.running and self.sim and not self.sim.done:
            self.acc += dt
            interval = 1.0 / self.steps_per_sec
            while self.acc >= interval and self.running:
                self.acc -= interval
                self.do_step()
        self.draw()
        return True

    def run(self) -> int:
        alive = True
        while alive:
            dt = self.clock.tick(30) / 1000.0
            alive = self.tick(dt)
        pygame.quit()
        return 0


def run_gui(scenario: str = "A", seed: int = 0, method: str = "proposed",
            config: Optional[Config] = None) -> int:
    if scenario.upper() not in SCENARIOS:
        raise ValueError(f"Unknown scenario '{scenario}'. Choose one of: {', '.join(SCENARIOS)}")
    return App(scenario, seed, method, config or Config()).run()
