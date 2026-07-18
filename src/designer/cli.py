"""Command line entry point.

    designer run-plan <plan.json> [--out DIR] [--iters N] [--no-vision]
    designer run-task <task.json> [--out DIR]
    designer check                      validate the installed packages
    designer audit <file.html>          render + grade one artifact
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _cmd_run_plan(args) -> int:
    from . import design
    with open(args.plan, encoding="utf-8") as fh:
        data = json.load(fh)
    tasks = data.get("tasks", data) if isinstance(data, dict) else data
    out = design.run_plan(tasks, args.out, max_iters=args.iters,
                          use_vision=not args.no_vision)
    print(json.dumps(out["summary"], indent=1))
    for r in out["reports"]:
        print(f'  {r["task_id"]:8s} {r["outcome"]:12s} '
              f'penalty={r.get("score", {}).get("penalty", "-")}')
    return 0 if out["summary"]["failed"] == 0 else 1


def _cmd_run_task(args) -> int:
    from . import design
    with open(args.task, encoding="utf-8") as fh:
        t = design.DesignTask.from_dict(json.load(fh))
    r = design.run_task(t, os.path.join(args.out, t.task_id),
                        max_iters=args.iters, use_vision=not args.no_vision)
    print(json.dumps({k: v for k, v in r.items() if k != "iterations"}, indent=1))
    return 0 if r["outcome"] == "designed" else 1


def _cmd_check(args) -> int:
    from . import designsystem, render, template
    try:
        chrome = render.find_chrome()
    except RuntimeError as e:
        print(f"renderer: UNAVAILABLE -- {e}")
        return 1
    print(f"renderer: {chrome}")
    print(f"fonts:    {len(designsystem.installed_font_families())} families")

    ds_all = designsystem.available()
    ds_ok = [i for i in ds_all if designsystem.usable(i)]
    print(f"design systems: {len(ds_ok)}/{len(ds_all)} usable")
    tpl_all = template.available()
    bad = [t for t in tpl_all
           if any(f["severity"] == "P0" for f in template.validate(template.load(t)))]
    print(f"templates:      {len(tpl_all) - len(bad)}/{len(tpl_all)} usable")
    if args.verbose:
        for i in ds_all:
            if i in ds_ok:
                continue
            defects = [f for f in designsystem.validate(designsystem.load(i))
                       if f["severity"] == "P0"]
            print(f"  {i}: {defects[0]['rule']} -- {defects[0]['message'][:70]}")
    return 0 if ds_ok and not bad else 1


def _cmd_audit(args) -> int:
    from . import designsystem, render, verify
    if not os.path.isfile(args.file):
        print(f"audit: no such file: {args.file}")
        return 2
    if not args.file.lower().endswith((".html", ".htm")):
        print(f"audit: expects an HTML file, got {os.path.splitext(args.file)[1] or 'no extension'}")
        return 2
    palette = []
    if args.design_system:
        palette = designsystem.load(args.design_system).palette()
    a = render.audit(args.file, palette=palette, components=args.component or [])
    with open(args.file, encoding="utf-8", errors="replace") as fh:
        html = fh.read()
    findings = verify.tier1(html) + verify.tier2(a, args.component or [])
    score = verify.score(findings)
    print(json.dumps(score, indent=1))
    print(verify.render_for_agent(findings))
    return 0 if score["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="designer", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("run-plan", help="run every kind=design task in a plan")
    rp.add_argument("plan")
    rp.add_argument("--out", default=os.path.join(os.environ.get("DESIGNER_DATA", "data"), "runs"))
    rp.add_argument("--iters", type=int, default=3)
    rp.add_argument("--no-vision", action="store_true")
    rp.set_defaults(fn=_cmd_run_plan)

    rt = sub.add_parser("run-task", help="run a single design task")
    rt.add_argument("task")
    rt.add_argument("--out", default=os.path.join(os.environ.get("DESIGNER_DATA", "data"), "runs"))
    rt.add_argument("--iters", type=int, default=3)
    rt.add_argument("--no-vision", action="store_true")
    rt.set_defaults(fn=_cmd_run_task)

    ck = sub.add_parser("check", help="validate renderer, fonts and packages")
    ck.add_argument("-v", "--verbose", action="store_true")
    ck.set_defaults(fn=_cmd_check)

    au = sub.add_parser("audit", help="render and grade one HTML file")
    au.add_argument("file")
    au.add_argument("--design-system")
    au.add_argument("--component", action="append")
    au.set_defaults(fn=_cmd_audit)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
