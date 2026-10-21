"""Command-line entry point: ``python -m cacpdf <command>``.

    gui       open the Pygame simulation window
    run       run one scenario headlessly and save its log
    evaluate  run scenarios x methods x seeds and write metrics/report
    list      list scenarios
"""

from __future__ import annotations

import argparse
import sys

from .config import Config
from .scenarios import SCENARIOS


def _config(path):
    return Config.load(path) if path else Config()


def cmd_list(_args) -> int:
    for s in SCENARIOS.values():
        print(f"{s.key}  {s.name:<28} {s.description}")
        print(f"   expected: {s.expected_text}")
    return 0


def cmd_run(args) -> int:
    from .logger import save_run
    from .simulation import Simulation
    sim = Simulation(args.scenario, args.seed, args.method, _config(args.config))
    while not sim.done:
        sim.step()
        if args.verbose:
            s = sim.step_log[-1]
            print(f"t={s['step']:3d} pos={tuple(s['pos'])} {s['state']:<11} "
                  f"{s['action']:<18} {s['reason'][:70]}")
    out = sim.outcome
    paths = save_run(sim, args.out)
    print(f"run_id={out['run_id']}  final={out['final_action']} ({out['final_reason']})")
    print(f"steps={out['steps']} distance={out['distance_travelled']} "
          f"exposure={out['static_exposure_incurred']} max_state={out['max_state']} "
          f"alert={out['alert_raised']} expected_met={out['expected_behavior_met']}")
    failed = [k for k, v in out["checks"].items() if v is False]
    if failed:
        print("failed checks:", ", ".join(failed))
    for k, p in paths.items():
        print(f"{k}: {p}")
    return 0


def cmd_evaluate(args) -> int:
    from .evaluate import SPLITS, evaluate, parse_seeds
    seeds = parse_seeds(args.seeds) if args.seeds else SPLITS[args.split]
    scenarios = [s.strip().upper() for s in args.scenarios.split(",")] if args.scenarios else None
    paths = evaluate(args.split, seeds, scenarios, args.out, _config(args.config),
                     save_all_logs=args.save_logs)
    for k, p in paths.items():
        print(f"{k}: {p}")
    return 0


def cmd_gui(args) -> int:
    from .gui import run_gui
    return run_gui(args.scenario, args.seed, args.method, _config(args.config))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m cacpdf",
                                description="CACPDF simulation proof of concept")
    p.add_argument("--config", help="JSON file overriding values in cacpdf/config.py")
    sub = p.add_subparsers(dest="cmd")

    g = sub.add_parser("gui", help="open the Pygame window")
    g.add_argument("--scenario", default="A")
    g.add_argument("--seed", type=int, default=0)
    g.add_argument("--method", default="proposed", choices=["proposed", "baseline"])

    r = sub.add_parser("run", help="run one scenario headlessly")
    r.add_argument("--scenario", default="A")
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--method", default="proposed", choices=["proposed", "baseline"])
    r.add_argument("--out", default="results/runs")
    r.add_argument("-v", "--verbose", action="store_true", help="print every step")

    e = sub.add_parser("evaluate", help="batch evaluation")
    e.add_argument("--split", default="eval", choices=["eval", "calib"])
    e.add_argument("--seeds", help="override seeds, e.g. '1000-1009' or '1,2,3'")
    e.add_argument("--scenarios", help="comma list, default all (A-G)")
    e.add_argument("--out", default="results")
    e.add_argument("--save-logs", action="store_true", help="save full JSON log of every run")

    sub.add_parser("list", help="list scenarios")
    argv = list(sys.argv[1:] if argv is None else argv)
    args = p.parse_args(argv)
    if args.cmd is None:          # no command given: open the GUI with defaults
        args = p.parse_args(argv + ["gui"])
    handlers = {"gui": cmd_gui, "run": cmd_run, "evaluate": cmd_evaluate, "list": cmd_list}
    try:
        return handlers[args.cmd](args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
