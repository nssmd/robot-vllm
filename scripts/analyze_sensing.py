"""Summarize every frozen matched case, keeping infra separate from verdicts."""
import argparse
import json
import math
from pathlib import Path
import statistics
import uuid

from robot_vllm.runtime import write_json


def percentile(values, q):
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lo = int(index)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    import numpy as np
    root = args.root
    manifest = json.loads((root / "manifest.json").read_text())
    cases = {}
    missing = []
    for seed in manifest["seeds"]:
        for variant in manifest["variants"]:
            path = root / f"seed-{seed}" / variant / "result.json"
            if path.exists():
                cases[seed, variant] = json.loads(path.read_text())
                cases[seed, variant]["subscriber_wait_seconds"] = sum(
                    sum(p["subscriber_wait_s"].values()) for p in cases[seed, variant]["phases"])
            else:
                missing.append([seed, variant])
    report = {"scope": manifest["scene"], "missing": missing, "preliminary_generalization": True,
              "frozen_campaign_complete": not missing, "variants": {}, "paired": {}}
    for variant in manifest["variants"]:
        rows = [r for (seed, v), r in cases.items() if v == variant]
        valid = [r for r in rows if r["status"] in ("success", "failure")]
        n = len(valid)
        success = sum(r["status"] == "success" for r in valid)
        z = 1.96
        if n:
            rate = success / n
            midpoint = (rate + z*z/(2*n)) / (1 + z*z/n)
            half = z*math.sqrt(rate*(1-rate)/n + z*z/(4*n*n))/(1+z*z/n)
            interval = [midpoint-half, midpoint+half]
        else:
            interval = None
        report["variants"][variant] = {"success": success, "failure": n-success,
            "infra": len(rows)-n, "denominator": n, "wilson_success_95ci": interval,
            "unmetered_calls": sum(r["unmetered_calls"] for r in rows),
            "tokens_all_attempts": {k: sum(r["tokens"][k] for r in rows) for k in ("input_tokens", "output_tokens", "total_tokens")},
            "model_calls_all_attempts": sum(r["model_calls"] for r in rows),
            "median": {metric: statistics.median(r[metric] for r in valid) if valid else None
                       for metric in ("wall_time_s", "sensor_barrier_s", "robot_blocked_seconds", "subscriber_wait_seconds", "max_position_error_m")}}
    for variant in manifest["variants"]:
        if variant == "independent":
            continue
        pairs = [(cases[s, "independent"], cases[s, variant]) for s in manifest["seeds"]
                 if (s, "independent") in cases and (s, variant) in cases
                 and cases[s, "independent"]["status"] != "infra" and cases[s, variant]["status"] != "infra"]
        metrics = {}
        rng = np.random.default_rng(481)
        for metric in ("wall_time_s", "sensor_barrier_s", "robot_blocked_seconds", "subscriber_wait_seconds", "total_tokens"):
            def get(row):
                return row["tokens"][metric] if metric == "total_tokens" else row[metric]
            ratios = [get(b)/get(a) for a,b in pairs if get(a) > 0]
            if ratios:
                boot = [statistics.median(rng.choice(ratios, size=len(ratios), replace=True)) for _ in range(5000)]
                metrics[metric] = {"median_paired_reduction": 1-statistics.median(ratios),
                    "paired_reduction_bootstrap_95ci": [1-percentile(boot,.975), 1-percentile(boot,.025)],
                    "improved_pairs": sum(r < 1 for r in ratios), "pairs": len(ratios)}
        report["paired"][variant] = {"count": len(pairs), "metrics": metrics,
            "success_regressions": sum(a["status"] == "success" and b["status"] == "failure" for a,b in pairs),
            "same_predictions": sum([p["prediction"] for p in a["phases"]] == [p["prediction"] for p in b["phases"]] for a,b in pairs),
            "note": "Paired non-infra cases, including task failures. Small-sample bootstrap is descriptive, not broad equivalence proof."}
    path = root / ("analysis-final.json" if not missing else "analysis-progress-" + uuid.uuid4().hex + ".json")
    if path.exists():
        path = root / ("analysis-" + uuid.uuid4().hex + ".json")
    write_json(path, report)
    print(json.dumps(report, indent=2))
    print("Evidence:", path)


if __name__ == "__main__":
    main()
