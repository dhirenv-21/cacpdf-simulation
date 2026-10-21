"""Batch evaluation: run scenarios x methods x seeds headlessly and compute metrics.

Every number in the generated report is computed from the runs executed by
this module; nothing is pre-filled. Seed splits:

* ``calib`` (seeds 0-19): used while hand-setting thresholds during development.
* ``eval``  (seeds 1000-1029): run only after the configuration was frozen.

Classification is scored per run: a run is "predicted positive" if any alert
state (INVESTIGATE or HIGH_ALERT) occurred, and "actually positive" if the
scenario's ground truth contains a staged event. UNCERTAIN is a caution state,
not an alert, and is reported separately (uncertain steps) so its cost is visible.
"""

from __future__ import annotations

import json
import os
import statistics
from collections import defaultdict
from typing import Dict, Iterable, List, Optional

from .config import Config
from .logger import save_run, write_csv
from .scenarios import SCENARIOS
from .simulation import METHODS, Simulation

SPLITS = {"calib": list(range(0, 20)), "eval": list(range(1000, 1030))}
RECOVERY_HORIZON = 10   # steps after a fault at which trust is re-sampled


def parse_seeds(text: str) -> List[int]:
    """'1000-1029' or '1,2,5' or 'eval'/'calib'."""
    if text in SPLITS:
        return SPLITS[text]
    seeds: List[int] = []
    for part in text.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            seeds.extend(range(int(a), int(b) + 1))
        elif part:
            seeds.append(int(part))
    return seeds


def _trust_at(step_log: List[dict], source: str, step: int) -> Optional[float]:
    val = None
    for s in step_log:
        if s["step"] > step:
            break
        val = s["trust"].get(source)
    return val


def run_batch(scenarios: Iterable[str], seeds: List[int], config: Optional[Config] = None,
              log_dir: Optional[str] = None, sample_log_dir: Optional[str] = None,
              progress=None):
    summaries, fault_rows, healthy_penalties = [], [], []
    for key in scenarios:
        for seed in seeds:
            for method in METHODS:
                sim = Simulation(key, seed, method, config)
                out = sim.run_to_end()
                summaries.append(out)
                if log_dir:
                    save_run(sim, log_dir, with_csv=False)
                if sample_log_dir and seed == seeds[0]:
                    save_run(sim, sample_log_dir, with_csv=True)
                if method == "proposed":   # trust is computed identically in both runs
                    for u in sim.trust_log:
                        if u["injected_fault"]:
                            fault_rows.append({
                                "scenario": key, "seed": seed, "step": u["step"],
                                "source_id": u["source_id"], "fault": u["injected_fault"],
                                "trust_before": u["before"], "trust_after": u["after"],
                                "delta": u["delta"], "reasons": u["reasons"],
                                f"trust_after_{RECOVERY_HORIZON}_steps": _trust_at(
                                    sim.step_log, u["source_id"], u["step"] + RECOVERY_HORIZON)})
                        elif u["delta"] < 0:
                            healthy_penalties.append({"scenario": key, "seed": seed, **u})
                if progress:
                    progress(out)
    return summaries, fault_rows, healthy_penalties


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def _sd(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.stdev(xs), 3) if len(xs) > 1 else None


def _prf(rows):
    tp = sum(1 for r in rows if r["event_present"] and r["alert_raised"])
    fp = sum(1 for r in rows if not r["event_present"] and r["alert_raised"])
    fn = sum(1 for r in rows if r["event_present"] and not r["alert_raised"])
    tn = sum(1 for r in rows if not r["event_present"] and not r["alert_raised"])
    p = tp / (tp + fp) if tp + fp else None
    rc = tp / (tp + fn) if tp + fn else None
    f1 = 2 * p * rc / (p + rc) if p and rc else (0.0 if p == 0 or rc == 0 else None)
    rnd = lambda v: None if v is None else round(v, 3)
    return {"runs": len(rows), "TP": tp, "FP": fp, "FN": fn, "TN": tn,
            "precision": rnd(p), "recall": rnd(rc), "f1": rnd(f1)}


def compute_metrics(summaries: List[dict], fault_rows: List[dict],
                    healthy_penalties: List[dict]) -> dict:
    by = defaultdict(list)
    for s in summaries:
        by[(s["scenario"], s["method"])].append(s)
    per = []
    for (sc, m), rows in sorted(by.items()):
        rec = {"scenario": sc, "method": m, **_prf(rows),
               "alert_runs": sum(r["alert_raised"] for r in rows),
               "high_alert_runs": sum(r["max_state"] == "HIGH_ALERT" for r in rows),
               "goal_rate": round(sum(r["goal_reached"] for r in rows) / len(rows), 3),
               "safe_stops": sum(r["safe_stop"] for r in rows),
               "expected_met": sum(bool(r["expected_behavior_met"]) for r in rows),
               "steps_mean": _mean([r["steps"] for r in rows]),
               "steps_sd": _sd([r["steps"] for r in rows]),
               "distance_mean": _mean([r["distance_travelled"] for r in rows]),
               "static_exposure_mean": _mean([r["static_exposure_incurred"] for r in rows]),
               "event_zone_steps_mean": _mean([r["event_zone_steps_during_event"] for r in rows]),
               "uncertain_steps_mean": _mean([r["uncertain_steps"] for r in rows]),
               "latency_mean": _mean([r["detection_latency"] for r in rows]),
               "collisions": sum(r["collisions"] for r in rows),
               "safety_overrides": sum(r["safety_overrides"] for r in rows)}
        per.append(rec)
    pooled = {m: _prf([s for s in summaries if s["method"] == m]) for m in METHODS}

    # identical-input verification
    hashes = defaultdict(set)
    for s in summaries:
        hashes[(s["scenario"], s["seed"])].add(s["events_hash"])
    identical = all(len(v) == 1 for v in hashes.values())

    faults = defaultdict(list)
    for f in fault_rows:
        faults[f["fault"]].append(f)
    fault_summary = [{
        "fault": k, "updates": len(v),
        "delta_mean": _mean([x["delta"] for x in v]),
        "trust_before_mean": _mean([x["trust_before"] for x in v]),
        "trust_after_mean": _mean([x["trust_after"] for x in v]),
        f"trust_after_{RECOVERY_HORIZON}_steps_mean":
            _mean([x[f"trust_after_{RECOVERY_HORIZON}_steps"] for x in v]),
    } for k, v in sorted(faults.items())]

    healthy = defaultdict(lambda: {"count": 0, "total_delta": 0.0})
    for h in healthy_penalties:
        key = (h["scenario"], h["source_id"], h["reasons"][0].split("(")[0] if h["reasons"] else "?")
        healthy[key]["count"] += 1
        healthy[key]["total_delta"] += h["delta"]
    healthy_summary = [{"scenario": k[0], "source_id": k[1], "reason": k[2], "count": v["count"],
                        "total_delta": round(v["total_delta"], 3)}
                       for k, v in sorted(healthy.items())]

    failures = [{"run_id": s["run_id"],
                 "failed_checks": [c for c, ok in s["checks"].items() if ok is False],
                 "max_state": s["max_state"], "final_action": s["final_action"],
                 "final_reason": s["final_reason"]}
                for s in summaries if s["expected_behavior_met"] is False]
    return {"per_scenario": per, "pooled": pooled, "identical_inputs": identical,
            "pairs_compared": len(hashes), "fault_trust": fault_summary,
            "penalties_without_injected_fault": healthy_summary, "failures": failures}


def _table(rows: List[dict], cols: List[str]) -> str:
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join("" if r.get(c) is None else str(r.get(c)) for c in cols) + " |")
    return "\n".join(out)


def write_report(metrics: dict, split: str, seeds: List[int], out_dir: str,
                 plot_path: Optional[str]) -> str:
    n = len(seeds)
    lines = [
        f"# CACPDF evaluation report — split `{split}`",
        "",
        "> **Preliminary, simulation-only results.** Generated automatically by "
        "`python -m cacpdf evaluate` from runs executed at generation time. "
        f"{n} seeds per scenario per method (seeds {seeds[0]}–{seeds[-1]}). "
        "Event streams are synthetic; these numbers say nothing about real-world "
        "sensors, networks or security performance. Scenario C–G inputs were designed to "
        "exercise specific behaviours, so results reflect those designs, not a "
        "representative workload. No statistical significance is claimed.",
        "",
        f"Identical event inputs for both methods verified by hash: "
        f"**{'yes' if metrics['identical_inputs'] else 'NO'}** "
        f"({metrics['pairs_compared']} scenario/seed pairs).",
        "",
        "## Pooled run-level detection (all scenarios)",
        "",
        "Positive = scenario ground truth has a staged event (B, G). "
        "Predicted positive = any INVESTIGATE/HIGH_ALERT during the run.",
        "",
        _table([{"method": m, **v} for m, v in metrics["pooled"].items()],
               ["method", "runs", "TP", "FP", "FN", "TN", "precision", "recall", "f1"]),
        "",
        "## Per scenario and method",
        "",
        _table(metrics["per_scenario"],
               ["scenario", "method", "runs", "alert_runs", "high_alert_runs", "FP", "FN",
                "expected_met", "goal_rate", "safe_stops", "steps_mean", "steps_sd",
                "distance_mean", "static_exposure_mean", "event_zone_steps_mean",
                "uncertain_steps_mean", "latency_mean", "collisions"]),
        "",
        "Columns: `expected_met` = runs meeting every pre-written expectation check for the "
        "scenario; `static_exposure_mean` = marked-zone exposure actually incurred; "
        "`event_zone_steps_mean` = steps spent inside the event zone while the staged event was "
        "active (ground truth); `latency_mean` = steps from event onset to first alert; "
        "`uncertain_steps_mean` = steps spent in UNCERTAIN (a cost of caution).",
        "",
    ]
    if plot_path:
        lines += [f"![baseline vs proposed]({os.path.basename(plot_path)})", ""]
    lines += [
        "## ADTA trust change after injected faults (proposed runs)",
        "",
        f"Trust of the affected source immediately before/after the update triggered by the "
        f"faulty record, and {RECOVERY_HORIZON} steps later.",
        "",
        _table(metrics["fault_trust"],
               ["fault", "updates", "trust_before_mean", "trust_after_mean", "delta_mean",
                f"trust_after_{RECOVERY_HORIZON}_steps_mean"]),
        "",
        "Note: `false_detection` (D) and `spurious_detection` (A) records are well-formed; ADTA "
        "cannot see they are false from the record alone. Their trust effect comes only from "
        "disagreement handling or is positive (valid-record reward).",
        "",
        "## Trust penalties on records without an injected-fault label",
        "",
        "These are penalties applied to records the generator did not label as faulty "
        "(e.g. symmetric repeated-disagreement penalties on the healthy side of a conflict).",
        "",
        _table(metrics["penalties_without_injected_fault"] or [{"scenario": "none"}],
               ["scenario", "source_id", "reason", "count", "total_delta"]),
        "",
        "## Failure cases (runs not meeting the scenario's expected behaviour)",
        "",
    ]
    fails = metrics["failures"]
    if not fails:
        lines.append("None in this split.")
    else:
        grouped = defaultdict(list)
        for f in fails:
            sc, m = f["run_id"].split("_")[:2]
            grouped[(sc, m, tuple(f["failed_checks"]))].append(f)
        lines.append(_table([{"scenario": k[0], "method": k[1], "failed_checks": ", ".join(k[2]),
                              "runs": len(v), "example": v[0]["run_id"],
                              "example_final": f"{v[0]['final_action']}: {v[0]['final_reason']}"}
                             for k, v in sorted(grouped.items())],
                            ["scenario", "method", "failed_checks", "runs", "example",
                             "example_final"]))
    lines += ["", "## Limitations", "",
              "- Software-only simulation with synthetic, scripted event streams.",
              "- Thresholds were hand-set on the calibration split; no optimisation or "
              "sensitivity study was performed.",
              "- Run-level detection metrics with few positive scenarios; small N.",
              "- The baseline is deliberately simple (fixed threshold, no integrity checks).",
              ""]
    path = os.path.join(out_dir, "report.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def make_plot(metrics: dict, path: str) -> Optional[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    per = metrics["per_scenario"]
    scen = sorted({r["scenario"] for r in per})
    get = lambda m, k: [next(r[k] for r in per if r["scenario"] == s and r["method"] == m)
                        for s in scen]
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.6))
    panels = [("alert_runs", "Runs with an alert"), ("expected_met", "Runs meeting expectations"),
              ("static_exposure_mean", "Mean marked-zone exposure")]
    x = range(len(scen))
    for ax, (key, title) in zip(axes, panels):
        for i, (m, color) in enumerate((("baseline", "#9a9a9a"), ("proposed", "#2b6cb0"))):
            ax.bar([xi + (i - 0.5) * 0.38 for xi in x], get(m, key), width=0.38, label=m,
                   color=color)
        ax.set_xticks(list(x))
        ax.set_xticklabels(scen)
        ax.set_title(title, fontsize=10)
        ax.spines[["top", "right"]].set_visible(False)
    axes[0].legend(frameon=False, fontsize=8)
    n = per[0]["runs"] if per else 0
    fig.suptitle(f"Simulation results by scenario (n={n} seeds each; preliminary)", fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def evaluate(split: str = "eval", seeds: Optional[List[int]] = None,
             scenarios: Optional[List[str]] = None, out_root: str = "results",
             config: Optional[Config] = None, save_all_logs: bool = False) -> Dict[str, str]:
    seeds = seeds or SPLITS[split]
    scenarios = scenarios or list(SCENARIOS)
    out_dir = os.path.join(out_root, split)
    os.makedirs(out_dir, exist_ok=True)
    cfg = config or Config()
    summaries, faults, healthy = run_batch(
        scenarios, seeds, cfg,
        log_dir=os.path.join(out_dir, "runs") if save_all_logs else None,
        sample_log_dir=os.path.join(out_dir, "sample_logs"))
    metrics = compute_metrics(summaries, faults, healthy)
    metrics.update(split=split, seeds=seeds, scenarios=scenarios, config=cfg.to_dict())
    paths = {}
    paths["summary_csv"] = os.path.join(out_dir, "summary_runs.csv")
    write_csv(paths["summary_csv"], summaries,
              ["run_id", "scenario", "method", "seed", "events_hash", "event_present",
               "alert_raised", "first_alert_step", "detection_latency", "max_state",
               "states_visited", "uncertain_steps", "goal_reached", "safe_stop", "final_action",
               "final_reason", "steps", "distance_travelled", "static_exposure_incurred",
               "event_zone_steps_during_event", "zone_entries_after_alert", "reroutes",
               "safety_overrides", "collisions", "expected_behavior_met", "checks"])
    paths["fault_csv"] = os.path.join(out_dir, "trust_faults.csv")
    write_csv(paths["fault_csv"], faults,
              ["scenario", "seed", "step", "source_id", "fault", "trust_before", "trust_after",
               "delta", f"trust_after_{RECOVERY_HORIZON}_steps", "reasons"])
    paths["metrics_json"] = os.path.join(out_dir, "metrics.json")
    with open(paths["metrics_json"], "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, indent=1)
    plot = make_plot(metrics, os.path.join(out_dir, "comparison.png"))
    if plot:
        paths["plot"] = plot
    paths["report"] = write_report(metrics, split, seeds, out_dir, plot)
    return paths
