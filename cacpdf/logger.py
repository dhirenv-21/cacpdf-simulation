"""Run log export (JSON for the full record, CSV for quick analysis)."""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from typing import Dict, List


def _jsonable(obj):
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(f"not JSON serialisable: {type(obj)}")


def save_run(sim, out_dir: str, with_csv: bool = True) -> Dict[str, str]:
    """Write ``<run_id>.json`` (+ ``<run_id>_steps.csv`` and ``_trust.csv``)."""
    os.makedirs(out_dir, exist_ok=True)
    log = sim.to_log()
    log["saved_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    paths = {"json": os.path.join(out_dir, f"{sim.run_id}.json")}
    with open(paths["json"], "w", encoding="utf-8") as fh:
        json.dump(log, fh, indent=1, default=_jsonable)
    if with_csv:
        paths["steps_csv"] = os.path.join(out_dir, f"{sim.run_id}_steps.csv")
        sources = list(sim.adta.trust)
        cols = ["step", "x", "y", "heading", "mode", "action", "state", "raw_state", "rule",
                "s_pos", "s_neg", "shadow_state", "alert", "safety_override", "reason"]
        with open(paths["steps_csv"], "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols + [f"trust_{s}" for s in sources])
            for s in sim.step_log:
                w.writerow([s["step"], s["pos"][0], s["pos"][1], s["heading"], s["mode"],
                            s["action"], s["state"], s["raw_state"], s["rule"], s["s_pos"],
                            s["s_neg"], s["shadow_state"], s["alert"] or "",
                            s["safety_override"] or "", s["reason"]]
                           + [s["trust"].get(src) for src in sources])
        paths["trust_csv"] = os.path.join(out_dir, f"{sim.run_id}_trust.csv")
        write_csv(paths["trust_csv"], sim.trust_log,
                  ["step", "source_id", "before", "after", "delta", "reasons", "event_id",
                   "injected_fault"])
    return paths


def write_csv(path: str, rows: List[dict], cols: List[str]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            vals = []
            for c in cols:
                v = r.get(c)
                if isinstance(v, (list, tuple)):
                    v = "; ".join(str(x) for x in v)
                elif isinstance(v, dict):
                    v = json.dumps(v)
                vals.append("" if v is None else v)
            w.writerow(vals)
