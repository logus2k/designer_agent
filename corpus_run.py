"""Run the audit across open-design's shipped example.html files.

These are real, hand-authored design artifacts, so the score distribution tells
us where a gate can honestly sit -- rather than picking thresholds from two
fixtures and hoping.
"""
import collections
import glob
import json
import os
import statistics
import sys
import traceback

sys.path.insert(0, "src")
from designer import render, verify  # noqa: E402

CORPUS = os.environ.get(
    "DESIGNER_CORPUS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "corpus"))

POLICIES = {
    "strict_aa": verify.Thresholds(),
    "split_gate": verify.Thresholds(contrast_marginal_severity="P2"),
}


def main():
    files = sorted(glob.glob(os.path.join(CORPUS, "*.html")))
    print(f"corpus: {len(files)} files from {CORPUS}\n")
    rows, errors = [], []
    rule_counts = collections.Counter()
    ratios = []

    for i, path in enumerate(files, 1):
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            a = render.audit(path, timeout=120)
            if "probe_error" in a:
                errors.append((name, "probe: " + a["probe_error"][:120]))
                continue
            html = open(path, encoding="utf-8", errors="replace").read()
            t1 = verify.tier1(html)
            row = {"name": name, "elements": a.get("elements_examined", 0),
                   "text_nodes": len(a.get("text_contrast", []))}
            for pname, th in POLICIES.items():
                f = t1 + verify.tier2(a, th=th)
                s = verify.score(f)
                row[pname] = s
                if pname == "strict_aa":
                    for x in f:
                        rule_counts[f'{x["severity"]}:{x["rule"]}'] += 1
            ratios.extend(c["ratio"] for c in a.get("text_contrast", []))
            rows.append(row)
        except Exception as e:
            errors.append((name, f"{type(e).__name__}: {e}"))
        if i % 20 == 0:
            print(f"  ...{i}/{len(files)}", flush=True)

    print(f"\naudited OK: {len(rows)}   errors: {len(errors)}")
    if errors:
        print("\n-- errors --")
        for n, e in errors[:15]:
            print(f"  {n}: {e}")

    print("\n-- pass rate by policy --")
    for pname in POLICIES:
        passed = sum(1 for r in rows if r[pname]["passed"])
        pens = sorted(r[pname]["penalty"] for r in rows)
        med = statistics.median(pens) if pens else 0
        print(f"  {pname:12s} pass {passed:3d}/{len(rows)} "
              f"({100*passed/max(1,len(rows)):5.1f}%)  median penalty {med:7.0f}")

    print("\n-- findings by rule (strict_aa) --")
    for k, v in rule_counts.most_common(20):
        print(f"  {k:34s} {v}")

    if ratios:
        ratios.sort()
        def pct(p): return ratios[min(len(ratios) - 1, int(len(ratios) * p))]
        print(f"\n-- contrast ratios across {len(ratios)} text elements --")
        print(f"  p05 {pct(.05):5.2f}  p10 {pct(.10):5.2f}  p25 {pct(.25):5.2f}  "
              f"median {pct(.50):5.2f}  p75 {pct(.75):5.2f}")
        for lo, hi in [(0, 1.5), (1.5, 3.0), (3.0, 4.5), (4.5, 7.0), (7.0, 99)]:
            n = sum(1 for r in ratios if lo <= r < hi)
            print(f"  {lo:4.1f}-{hi:4.1f}: {n:5d} ({100*n/len(ratios):5.1f}%)")

    print("\n-- worst 12 by strict_aa penalty --")
    for r in sorted(rows, key=lambda r: -r["strict_aa"]["penalty"])[:12]:
        c = r["strict_aa"]["counts"]
        print(f'  {r["name"][:44]:44s} P0={c["P0"]:3d} P1={c["P1"]:3d} '
              f'pen={r["strict_aa"]["penalty"]:6d}')

    print("\n-- cleanest 12 by strict_aa penalty --")
    for r in sorted(rows, key=lambda r: r["strict_aa"]["penalty"])[:12]:
        c = r["strict_aa"]["counts"]
        print(f'  {r["name"][:44]:44s} P0={c["P0"]:3d} P1={c["P1"]:3d} '
              f'pen={r["strict_aa"]["penalty"]:6d}')

    with open("corpus_results.json", "w") as fh:
        json.dump({"rows": rows, "errors": errors}, fh, indent=1)
    print("\nwrote corpus_results.json")


if __name__ == "__main__":
    main()
