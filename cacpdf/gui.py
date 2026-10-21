"""Pygame front end. Rendering and input only; all logic lives in Simulation.

Controls (also shown on screen):
    SPACE start/pause   N single step   R reset   1-7 scenario A-G
    M toggle method     [ / ] seed -/+   - / + speed
    F1 stale  F2 duplicate  F3 invalid  F4 conflict (inject fault)
    H show/hide heartbeat messages   L save log now   ESC quit
"""

from __future__ import annotations

import math
import os
import time
import traceback
from typing import Callable, List, Optional, Tuple

import pygame

from .config import Config
from .ctre import STATES
from .events import SOURCES
from .logger import save_run
from .scenarios import SCENARIOS
from .simulation import Simulation

W, H = 1440, 900
CELL = 34
MAP_X, MAP_Y = 12, 52
RIGHT_X = 846
GUI_LOG_DIR = os.path.join("results", "gui_runs")

C = {
    "bg": (236, 239, 243), "panel": (255, 255, 255), "border": (200, 205, 214),
    "text": (28, 32, 40), "muted": (105, 112, 125), "grid": (226, 230, 236),
    "free": (248, 249, 251), "obstacle": (62, 70, 84), "start": (46, 160, 90),
    "goal": (214, 160, 20), "route": (37, 99, 235), "ref": (130, 136, 148),
    "rejected_route": (200, 40, 40), "trail": (147, 197, 253),
    "zone_mon": (147, 112, 219), "zone_exp": (237, 137, 54), "zone_inv": (245, 180, 40),
    "zone_nogo": (220, 38, 38), "ok": (22, 140, 70), "bad": (200, 40, 40), "warn": (204, 120, 0),
    "btn": (226, 231, 238), "btn_on": (37, 99, 235), "btn_hover": (210, 218, 230),
}
STATE_COLORS = {"NORMAL": (22, 140, 70), "UNCERTAIN": (204, 140, 0),
                "INVESTIGATE": (234, 88, 12), "HIGH_ALERT": (200, 30, 30)}
MODE_COLORS = {"MOVING": (37, 99, 235), "REROUTING": (124, 58, 237), "WAITING": (204, 140, 0),
               "INVESTIGATING": (234, 88, 12), "REPOSITIONING": (234, 88, 12),
               "STOPPED": (200, 30, 30), "SAFE_STOP": (200, 30, 30), "ARRIVED": (22, 140, 70),
               "READY": (105, 112, 125)}


class Button:
    def __init__(self, rect, label: str, action: Callable[[], None],
                 active: Optional[Callable[[], bool]] = None, tip: str = ""):
        self.rect = pygame.Rect(rect)
        self.label = label
        self.action = action
        self.active = active or (lambda: False)
        self.tip = tip

    def draw(self, surf, font, mouse):
        on = self.active()
        hover = self.rect.collidepoint(mouse)
        col = C["btn_on"] if on else (C["btn_hover"] if hover else C["btn"])
        pygame.draw.rect(surf, col, self.rect, border_radius=5)
        pygame.draw.rect(surf, C["border"], self.rect, 1, border_radius=5)
        txt = font.render(self.label() if callable(self.label) else self.label, True,
                          (255, 255, 255) if on else C["text"])
        surf.blit(txt, txt.get_rect(center=self.rect.center))


def wrap(text: str, font, width: int) -> List[str]:
    words, lines, cur = text.split(" "), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if font.size(trial)[0] <= width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            while font.size(w)[0] > width and len(w) > 1:   # hard-break long tokens
                k = len(w)
                while k > 1 and font.size(w[:k])[0] > width:
                    k -= 1
                lines.append(w[:k])
                w = w[k:]
            cur = w
    if cur:
        lines.append(cur)
    return lines


class App:
    def __init__(self, scenario: str, seed: int, method: str, config: Config):
        pygame.init()
        pygame.display.set_caption("CACPDF simulation proof of concept")
        self.screen = pygame.display.set_mode((W, H))
        self.clock = pygame.time.Clock()
        mono = "consolas,dejavusansmono,menlo,couriernew"
        sans = "segoeui,dejavusans,helvetica,arial"
        self.f_small = pygame.font.SysFont(mono, 12)
        self.f_mono = pygame.font.SysFont(mono, 13)
        self.f_ui = pygame.font.SysFont(sans, 14)
        self.f_bold = pygame.font.SysFont(sans, 15, bold=True)
        self.f_title = pygame.font.SysFont(sans, 19, bold=True)
        self.f_state = pygame.font.SysFont(sans, 22, bold=True)
        self.config = config
        self.scenario, self.seed, self.method = scenario.upper(), seed, method
        self.running = False
        self.steps_per_sec = 3.0
        self.acc = 0.0
        self.show_heartbeats = False
        self.message = ""
        self.message_t = 0.0
        self.saved_path: Optional[str] = None
        self.error: Optional[str] = None
        self.prev_pos = None
        self.sim: Optional[Simulation] = None
        self.new_sim()
        self.buttons = self._make_buttons()

    # ------------------------------------------------------------- control
    def new_sim(self):
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

    def flash(self, msg: str):
        self.message, self.message_t = msg, time.time()

    def set_scenario(self, key: str):
        self.scenario = key
        self.new_sim()
        self.flash(f"Scenario {key}: {SCENARIOS[key].name}")

    def toggle_method(self):
        self.method = "baseline" if self.method == "proposed" else "proposed"
        self.new_sim()
        self.flash(f"Method: {self.method} (run reset)")

    def change_seed(self, d: int):
        self.seed = max(0, self.seed + d)
        self.new_sim()
        self.flash(f"Seed {self.seed} (run reset)")

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
            self.flash(self.sim.inject(kind) + " (run marked as manually injected)")
        except ValueError as exc:
            self.flash(str(exc))

    def speed(self, f: float):
        self.steps_per_sec = max(0.5, min(20.0, self.steps_per_sec * f))
        self.flash(f"Speed {self.steps_per_sec:.1f} steps/s")

    def _make_buttons(self) -> List[Button]:
        b: List[Button] = []
        y1, y2 = 596, 630
        x = MAP_X
        b.append(Button((x, y1, 92, 28), lambda: "Pause" if self.running else "Start",
                        self.toggle_run, lambda: self.running))
        b.append(Button((x + 98, y1, 60, 28), "Step", self.do_step))
        b.append(Button((x + 164, y1, 64, 28), "Reset", self.new_sim))
        x = MAP_X + 246
        for i, key in enumerate(SCENARIOS):
            b.append(Button((x + i * 38, y1, 34, 28), key,
                            (lambda k=key: lambda: self.set_scenario(k))(),
                            (lambda k=key: lambda: self.scenario == k)()))
        x = MAP_X + 520
        b.append(Button((x, y1, 150, 28), lambda: f"Method: {self.method}", self.toggle_method,
                        lambda: self.method == "baseline"))
        b.append(Button((x + 156, y1, 28, 28), "-", lambda: self.change_seed(-1)))
        b.append(Button((x + 254, y1, 28, 28), "+", lambda: self.change_seed(1)))
        x = MAP_X
        b.append(Button((x, y2, 28, 26), "-", lambda: self.speed(1 / 1.5)))
        b.append(Button((x + 128, y2, 28, 26), "+", lambda: self.speed(1.5)))
        x = MAP_X + 200
        for i, (k, lab) in enumerate((("stale", "Inject stale"), ("duplicate", "Inject dup"),
                                      ("invalid", "Inject invalid"),
                                      ("conflict", "Inject conflict"))):
            b.append(Button((x + i * 112, y2, 106, 26), lab,
                            (lambda kk=k: lambda: self.inject(kk))()))
        b.append(Button((MAP_X + 652, y2, 82, 26), "Save log", self.save_log))
        b.append(Button((MAP_X + 740, y2, 76, 26), lambda: "HB: show" if self.show_heartbeats
                        else "HB: hide", self.toggle_hb, lambda: self.show_heartbeats))
        return b

    def toggle_hb(self):
        self.show_heartbeats = not self.show_heartbeats

    # -------------------------------------------------------------- drawing
    def cell_center(self, c, frac_from=None, frac=1.0) -> Tuple[float, float]:
        x, y = c
        if frac_from is not None and frac < 1.0:
            x = frac_from[0] + (c[0] - frac_from[0]) * frac
            y = frac_from[1] + (c[1] - frac_from[1]) * frac
        return MAP_X + x * CELL + CELL / 2, MAP_Y + y * CELL + CELL / 2

    def text(self, s, pos, font=None, color=None):
        img = (font or self.f_ui).render(str(s), True, color or C["text"])
        self.screen.blit(img, pos)
        return img.get_width()

    def panel(self, rect, title=None):
        r = pygame.Rect(rect)
        pygame.draw.rect(self.screen, C["panel"], r, border_radius=6)
        pygame.draw.rect(self.screen, C["border"], r, 1, border_radius=6)
        if title:
            self.text(title, (r.x + 10, r.y + 6), self.f_bold)
        return r

    def dashed(self, pts, color, width=2, dash=7):
        for a, b in zip(pts, pts[1:]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            L = math.hypot(dx, dy)
            n = max(1, int(L / dash))
            for i in range(0, n, 2):
                p = (a[0] + dx * i / n, a[1] + dy * i / n)
                q = (a[0] + dx * min(i + 1, n) / n, a[1] + dy * min(i + 1, n) / n)
                pygame.draw.line(self.screen, color, p, q, width)

    def draw_map(self):
        sim = self.sim
        w = sim.world
        surf = self.screen
        rect = pygame.Rect(MAP_X, MAP_Y, w.width * CELL, w.height * CELL)
        pygame.draw.rect(surf, C["free"], rect)
        overlay = pygame.Surface((rect.w, rect.h), pygame.SRCALPHA)
        for zid, z in w.zones.items():
            dyn = sim.dynamic.get(zid)
            if dyn and dyn["prohibited"]:
                col, a = C["zone_nogo"], 95
            elif dyn:
                col, a = C["zone_inv"], 90
            elif z.exposure > 0:
                col, a = C["zone_exp"], int(min(140, 45 + 30 * z.exposure))
            else:
                col, a = C["zone_mon"], 55
            for (x, y) in z.cells:
                pygame.draw.rect(overlay, (*col, a), (x * CELL, y * CELL, CELL, CELL))
                if dyn and dyn["prohibited"]:
                    pygame.draw.line(overlay, (*col, 150), (x * CELL, y * CELL + CELL),
                                     (x * CELL + CELL, y * CELL), 2)
        surf.blit(overlay, rect.topleft)
        for x in range(w.width + 1):
            pygame.draw.line(surf, C["grid"], (MAP_X + x * CELL, MAP_Y),
                             (MAP_X + x * CELL, rect.bottom))
        for y in range(w.height + 1):
            pygame.draw.line(surf, C["grid"], (MAP_X, MAP_Y + y * CELL),
                             (rect.right, MAP_Y + y * CELL))
        for (x, y) in w.obstacles:
            pygame.draw.rect(surf, C["obstacle"], (MAP_X + x * CELL, MAP_Y + y * CELL, CELL, CELL))
        pygame.draw.rect(surf, C["text"], rect, 3)   # hard boundary
        for zid, z in w.zones.items():
            if z.cells:
                xs = [c[0] for c in z.cells]
                ys = [c[1] for c in z.cells]
                dyn = sim.dynamic.get(zid)
                tag = (" NO-GO" if dyn and dyn["prohibited"] else
                       " +exposure" if dyn else "")
                lab = f"{z.label}{tag}" + (f" (exposure {z.exposure:g}/cell)" if z.exposure else "")
                img = self.f_small.render(lab, True, C["text"])
                pos = (MAP_X + min(xs) * CELL + 3, MAP_Y + min(ys) * CELL + 2)
                pygame.draw.rect(surf, (255, 255, 255), (*pos, img.get_width() + 4, 14))
                surf.blit(img, (pos[0] + 2, pos[1]))
        # start / goal
        sx, sy = self.cell_center(w.start)
        pygame.draw.rect(surf, C["start"], (sx - 12, sy - 12, 24, 24), border_radius=4)
        self.text("S", (sx - 5, sy - 9), self.f_bold, (255, 255, 255))
        gx, gy = self.cell_center(w.goal)
        pygame.draw.circle(surf, C["goal"], (gx, gy), 13)
        self.text("G", (gx - 6, gy - 9), self.f_bold, (255, 255, 255))
        # reference shortest path (obstacle-only) and rejected route
        ref = sim.reference_plan
        if ref.ok:
            self.dashed([self.cell_center(c) for c in ref.path], C["ref"], 2)
        if sim.plan and sim.plan.rejected_path:
            self.dashed([self.cell_center(c) for c in sim.plan.rejected_path],
                        C["rejected_route"], 3, 5)
        # trail and current route
        for c in sim.rover.trail:
            pygame.draw.circle(surf, C["trail"], self.cell_center(c), 3)
        if sim.route and sim.rover.pos in sim.route:
            rest = sim.route[sim.route.index(sim.rover.pos):]
            if len(rest) > 1:
                pygame.draw.lines(surf, C["route"], False, [self.cell_center(c) for c in rest], 3)
            nxt = rest[1] if len(rest) > 1 else None
            if nxt:
                pygame.draw.circle(surf, C["route"], self.cell_center(nxt), 9, 2)
        # recent evidence markers
        for o in list(sim.recent_obs)[:30]:
            if sim.t - o["received_step"] > 4:
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
            cx, cy = self.cell_center(tuple(cell))
            col = C["ok"] if o["accepted"] else C["bad"]
            if ev.polarity > 0:
                pts = [(cx, cy - 8), (cx + 8, cy), (cx, cy + 8), (cx - 8, cy)]
                pygame.draw.polygon(surf, col, pts, 0 if o["accepted"] else 2)
            else:
                pygame.draw.line(surf, col, (cx - 7, cy - 7), (cx + 7, cy + 7), 3)
                pygame.draw.line(surf, col, (cx - 7, cy + 7), (cx + 7, cy - 7), 3)
        # rover (interpolated between cells while running)
        frac = 1.0
        if self.running and self.steps_per_sec > 0:
            frac = min(1.0, self.acc * self.steps_per_sec)
        rx, ry = self.cell_center(sim.rover.pos, self.prev_pos, frac)
        col = MODE_COLORS.get(sim.rover.mode, C["muted"])
        pygame.draw.circle(surf, col, (rx, ry), 12)
        pygame.draw.circle(surf, (255, 255, 255), (rx, ry), 12, 2)
        h = math.radians(sim.rover.heading)
        tip = (rx + math.cos(h) * 18, ry + math.sin(h) * 18)
        l = (rx + math.cos(h + 2.5) * 9, ry + math.sin(h + 2.5) * 9)
        r = (rx + math.cos(h - 2.5) * 9, ry + math.sin(h - 2.5) * 9)
        pygame.draw.polygon(surf, (20, 20, 20), [tip, l, r])
        # banner
        if sim.done:
            d = sim.decision
            msg = (f"GOAL REACHED in {sim.t} steps" if d.action == "ARRIVED"
                   else f"SAFE STOP at step {sim.t}: {d.reason}")
            col = C["ok"] if d.action == "ARRIVED" else C["bad"]
            lines = wrap(msg, self.f_bold, rect.w - 40)
            bh = 14 + 20 * len(lines)
            pygame.draw.rect(surf, col, (rect.x + 10, rect.y + 10, rect.w - 20, bh), border_radius=6)
            for i, ln in enumerate(lines):
                self.text(ln, (rect.x + 20, rect.y + 17 + 20 * i), self.f_bold, (255, 255, 255))
        # legend
        ly = rect.bottom + 18
        items = [("route", "planned route"), ("ref", "shortest (dashed)"),
                 ("trail", "travelled"), ("zone_mon", "monitored"),
                 ("zone_exp", "exposure"), ("zone_inv", "+alert cost"),
                 ("zone_nogo", "no-go"), ("ok", "accepted"), ("bad", "rejected")]
        x = MAP_X
        for key, lab in items:
            pygame.draw.rect(surf, C[key], (x, ly + 3, 12, 10))
            x += 16 + self.text(lab, (x + 16, ly), self.f_small, C["muted"]) + 8

    def draw_header(self):
        sim = self.sim
        self.text("CACPDF — Cognitive Autonomous Cyber-Physical Defense Framework",
                  (12, 8), self.f_title)
        self.text("Simulation proof of concept only — not a deployed security system. "
                  "All sensor and cyber events are simulated; 'events' are staged scenario labels.",
                  (12, 32), self.f_small, C["muted"])
        if sim:
            s = sim.scenario
            self.text(f"Scenario {s.key}: {s.name}", (RIGHT_X, 8), self.f_title)

    def draw_controls(self):
        mouse = pygame.mouse.get_pos()
        for b in self.buttons:
            b.draw(self.screen, self.f_ui, mouse)
        self.text(f"seed {self.seed}", (MAP_X + 708, 602), self.f_ui)
        self.text(f"{self.steps_per_sec:.1f} st/s", (MAP_X + 34, 634), self.f_ui)
        self.text("Keys: SPACE run  N step  R reset  1-7 scen.  M method  [ ] seed  -/+ speed  "
                  "F1-F4 inject  H HB  L save  ESC",
                  (MAP_X, 662), self.f_small, C["muted"])

    def draw_feed(self):
        sim = self.sim
        r = self.panel((MAP_X, 680, 816, 210), "Event feed (newest first) — simulated records")
        cols = [("recv", 40), ("src t", 46), ("source", 92), ("type", 124), ("zone", 40),
                ("conf", 46), ("age", 34), ("status / flags", 230), ("trust", 50)]
        x0, y = r.x + 10, r.y + 28
        x = x0
        for name, wd in cols:
            self.text(name, (x, y), self.f_small, C["muted"])
            x += wd
        y += 16
        rows = [o for o in sim.recent_obs
                if self.show_heartbeats or o["event_type"] != "status" or o["flags"]]
        for o in rows[:10]:
            if o["accepted"]:
                status = "ACCEPTED" + (" (" + ",".join(o["flags"]) + ")" if o["flags"] else "")
                col = C["warn"] if o["flags"] else C["ok"]
            else:
                status = "REJECTED: " + ",".join(o["flags"])
                col = C["bad"]
            if o["event_type"] == "heartbeat_timeout":
                status, col = "MISSING: no message within timeout", C["warn"]
            vals = [o["received_step"], o["step"], o["source_id"], o["event_type"],
                    o["zone_id"] or "-", "-" if o["confidence"] is None else f"{o['confidence']:.2f}",
                    o["age_at_arrival"], status, o.get("trust_after", "")]
            x = x0
            for (name, wd), v in zip(cols, vals):
                self.text(str(v)[:40], (x, y), self.f_small, col if name == "status / flags"
                          else C["text"])
                x += wd
            y += 15

    def draw_status(self):
        sim = self.sim
        r = self.panel((RIGHT_X, 52, W - RIGHT_X - 12, 92))
        rv = sim.rover
        nxt = sim._route_next()
        self.text(f"run {sim.run_id}", (r.x + 10, r.y + 6), self.f_small, C["muted"])
        self.text(f"method: {sim.method}   seed: {sim.seed}   step: {sim.t}/{sim.cfg.sim.max_steps}"
                  f"   {'RUNNING' if self.running else ('DONE' if sim.done else 'PAUSED')}",
                  (r.x + 10, r.y + 24), self.f_ui)
        col = MODE_COLORS.get(rv.mode, C["muted"])
        self.text(f"Rover: {rv.mode}", (r.x + 10, r.y + 46), self.f_bold, col)
        self.text(f"pos {rv.pos}  heading {rv.heading:.0f}°  waypoint {nxt if nxt else '-'}",
                  (r.x + 230, r.y + 48), self.f_ui)
        self.text(f"travelled {rv.distance:.1f} cells   zone exposure {sim.static_exposure_incurred:.1f}"
                  f"   alerts {len(sim.alerts)}", (r.x + 10, r.y + 68), self.f_ui, C["muted"])

    def draw_trust(self):
        sim = self.sim
        r = self.panel((RIGHT_X, 150, W - RIGHT_X - 12, 160), "ADTA trust (0–1) and last update reason")
        y = r.y + 30
        for s, meta in SOURCES.items():
            t = sim.adta.trust[s]
            init = sim.adta.initial[s]
            self.text(f"{s}", (r.x + 10, y), self.f_mono)
            self.text(meta["type"], (r.x + 104, y), self.f_small, C["muted"])
            bx, bw = r.x + 170, 150
            pygame.draw.rect(self.screen, C["grid"], (bx, y + 2, bw, 13), border_radius=3)
            col = C["ok"] if t >= 0.6 else (C["warn"] if t >= 0.35 else C["bad"])
            pygame.draw.rect(self.screen, col, (bx, y + 2, int(bw * t), 13), border_radius=3)
            ix = bx + int(bw * init)
            pygame.draw.line(self.screen, C["text"], (ix, y), (ix, y + 17), 1)
            self.text(f"{t:.3f}", (bx + bw + 8, y), self.f_mono)
            reason = sim.adta.last_reason.get(s, "")
            if s in sim.fusion.missing:
                reason = "SILENT (missing) — " + reason
            rx = bx + bw + 58
            room = r.right - rx - 8
            while reason and self.f_small.size(reason)[0] > room:
                reason = reason[:-2]
            self.text(reason, (rx, y), self.f_small, C["muted"])
            y += 20
        self.text("| = initial trust.   W = trust x conf x freshness.   1 conflict: "
                  "no penalty.", (r.x + 10, r.bottom - 40), self.f_small, C["muted"])
        hist = sim.step_log[-60:]
        if len(hist) > 1:
            self.text("trust history (last 60 steps):", (r.x + 10, r.bottom - 20), self.f_small,
                      C["muted"])
            gx, gy, gw, gh = r.x + 200, r.bottom - 22, r.w - 212, 16
            pygame.draw.rect(self.screen, C["free"], (gx, gy, gw, gh))
            palette = [(37, 99, 235), (22, 140, 70), (147, 112, 219), (234, 88, 12)]
            for k, s in enumerate(SOURCES):
                pts = [(gx + gw * i / (len(hist) - 1), gy + gh - gh * h["trust"][s])
                       for i, h in enumerate(hist)]
                pygame.draw.lines(self.screen, palette[k % 4], False, pts, 1)

    def draw_ctre(self):
        sim = self.sim
        r = self.panel((RIGHT_X, 316, W - RIGHT_X - 12, 290))
        a = sim.assessment
        label = "CTRE threat state" if sim.method == "proposed" else "Baseline (fixed threshold) state"
        self.text(label, (r.x + 10, r.y + 6), self.f_bold)
        state = a.state if a else "NORMAL"
        x = r.x + 10
        for s in STATES:   # state ladder
            on = s == state
            wdt = self.f_small.size(s)[0] + 14
            pygame.draw.rect(self.screen, STATE_COLORS[s] if on else C["grid"],
                             (x, r.y + 30, wdt, 22), border_radius=4)
            self.text(s, (x + 7, r.y + 34), self.f_small, (255, 255, 255) if on else C["muted"])
            x += wdt + 6
        if a:
            self.text(f"rule {a.rule}   zone {a.zone_id or '-'}   raw {a.raw_state}",
                      (r.x + 10, r.y + 58), self.f_mono)
            self.text(f"S_pos={a.s_pos:.2f}  S_neg={a.s_neg:.2f}  confidence={a.confidence:.2f}",
                      (r.x + 10, r.y + 76), self.f_mono)
            y = r.y + 96
            for line in a.explanation:
                for ln in wrap(line, self.f_small, r.w - 24):
                    if y > r.bottom - 36:
                        break
                    col = C["text"]
                    s = line.strip()
                    if s.startswith("x ") or s.startswith("- "):
                        col = C["bad"]
                    elif s.startswith("? "):
                        col = C["warn"]
                    elif s.startswith("+ "):
                        col = C["ok"]
                    self.text(ln, (r.x + 12, y), self.f_small, col)
                    y += 14
        sh = sim.shadow
        if sh:
            other = "baseline" if sim.method == "proposed" else "proposed CTRE"
            self.text(f"Same inputs, {other} would say: {sh.state}", (r.x + 10, r.bottom - 22),
                      self.f_ui, STATE_COLORS.get(sh.state, C["muted"]))
        note = self.f_small.render("States label the staged scenario only.", True, C["muted"])
        self.screen.blit(note, note.get_rect(bottomright=(r.right - 10, r.bottom - 6)))

    def draw_nav(self):
        sim = self.sim
        r = self.panel((RIGHT_X, 612, W - RIGHT_X - 12, 278), "ATNO route + safety-checked response")
        d = sim.decision
        y = r.y + 30
        if d:
            col = C["bad"] if d.action in ("SAFE_STOP", "STOP_ALERT", "HOLD") else (
                C["ok"] if d.action in ("CONTINUE", "ARRIVED") else C["warn"])
            self.text(f"Action: {d.action}", (r.x + 10, y), self.f_state, col)
            y += 30
            for ln in wrap(f"Reason: {d.reason}", self.f_ui, r.w - 24)[:3]:
                self.text(ln, (r.x + 10, y), self.f_ui)
                y += 18
            if d.alert:
                self.text(f"ALERT: {d.alert}", (r.x + 10, y), self.f_ui, C["bad"])
                y += 18
            if d.override:
                self.text(f"SAFETY OVERRIDE: {d.override}", (r.x + 10, y), self.f_ui, C["bad"])
                y += 18
        else:
            self.text("Press Start (SPACE) or Step (N)", (r.x + 10, y), self.f_state, C["muted"])
            y += 30
        y += 4
        rem = sim._remaining_metrics()
        if rem:
            self.text(f"Current route: {rem['waypoints']} waypoints, distance {rem['distance']:.1f}, "
                      f"exposure cost {rem['exposure']:.1f}  ({sim.planner.name})",
                      (r.x + 10, y), self.f_ui)
            y += 20
        ref, ta = sim.reference_plan, sim.threat_plan_initial
        self.text("From start:", (r.x + 10, y), self.f_bold)
        y += 19
        self.text(f"  shortest (obstacle-only): distance {ref.distance:.1f}, exposure {ref.exposure:.1f}"
                  if ref.ok else f"  shortest: {ref.reason}", (r.x + 10, y), self.f_mono)
        y += 17
        txt = (f"  threat-aware:             distance {ta.distance:.1f}, exposure {ta.exposure:.1f}"
               if ta.ok else f"  threat-aware: {ta.reason}")
        for ln in wrap(txt, self.f_mono, r.w - 24)[:2]:
            self.text(ln, (r.x + 10, y), self.f_mono, C["text"] if ta.ok else C["bad"])
            y += 17
        cfg = sim.cfg.atno
        self.text(f"  J = {cfg.lambda_distance:g}·distance + {cfg.lambda_exposure:g}·exposure;"
                  f" max exposure {cfg.max_route_exposure:g}", (r.x + 10, y), self.f_small,
                  C["muted"])
        y += 18
        if sim.dynamic:
            for zid, dz in sim.dynamic.items():
                self.text(f"Dynamic cost {zid}: {dz['reason']} (until step {dz['until']})",
                          (r.x + 10, y), self.f_small, C["warn"])
                y += 15
        exp = wrap(f"Expected ({sim.scenario.key}): {sim.scenario.expected_text}", self.f_small,
                   r.w - 24)[:2]
        for i, ln in enumerate(exp):
            self.text(ln, (r.x + 10, r.bottom - 54 + 14 * i), self.f_small, C["muted"])
        if sim.done:
            o = sim.outcome
            ok = o.get("expected_behavior_met")
            failed = [k for k, v in o["checks"].items() if v is False]
            self.text(f"Outcome vs expectation: {'MET' if ok else 'NOT MET: ' + ', '.join(failed)}",
                      (r.x + 10, r.bottom - 20), self.f_ui, C["ok"] if ok else C["bad"])

    def draw(self):
        self.screen.fill(C["bg"])
        self.draw_header()
        if self.sim:
            self.draw_map()
            self.draw_status()
            self.draw_trust()
            self.draw_ctre()
            self.draw_nav()
            self.draw_feed()
        self.draw_controls()
        if self.message and time.time() - self.message_t < 4:
            img = self.f_ui.render(self.message, True, (255, 255, 255))
            bg = img.get_rect(midbottom=(MAP_X + 408, MAP_Y + 15 * CELL - 8)).inflate(16, 8)
            pygame.draw.rect(self.screen, (40, 46, 58), bg, border_radius=5)
            self.screen.blit(img, img.get_rect(center=bg.center))
        if self.error:
            pygame.draw.rect(self.screen, C["bad"], (MAP_X, MAP_Y + 200, 816, 70))
            for i, ln in enumerate(wrap(self.error, self.f_ui, 790)[:3]):
                self.text(ln, (MAP_X + 12, MAP_Y + 210 + 18 * i), self.f_ui, (255, 255, 255))
        pygame.display.flip()

    # -------------------------------------------------------------- loop
    def handle_key(self, key):
        keymap = {pygame.K_SPACE: self.toggle_run, pygame.K_n: self.do_step,
                  pygame.K_r: self.new_sim, pygame.K_m: self.toggle_method,
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
                  pygame.K_h: self.toggle_hb, pygame.K_l: self.save_log}
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
                    return False
                self.handle_key(ev.key)
            if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                for b in self.buttons:
                    if b.rect.collidepoint(ev.pos):
                        b.action()
                        break
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
