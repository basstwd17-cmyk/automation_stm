"""Recompute timing metrics from external_input_k15.py's native CSV export."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

from analyze_input_output_jitter import metrics


def analyze(path: Path) -> dict[str, dict[str, float]]:
    values = {"d1_c1_minus_input": [], "d2_c2_minus_input": [], "delta_c2_minus_c1": []}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            c1, c2, cin = (float(row[key]) for key in ("t50_c1_s", "t50_c2_s", "t50_c3_s"))
            if not all(math.isfinite(value) for value in (c1, c2, cin)):
                raise ValueError("Every crossing time must be finite")
            for key, value in zip(values, (c1 - cin, c2 - cin, c2 - c1)):
                values[key].append(value)
    return {key: metrics(samples) for key, samples in values.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    source = args.run_dir / "input_output_segments.csv"
    result = analyze(source)
    output = args.run_dir / "analysis_external_input"
    output.mkdir(exist_ok=True)
    (output / "timing_metrics.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    with (output / "timing_metrics.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["quantity", *next(iter(result.values()))])
        writer.writeheader()
        writer.writerows({"quantity": key, **value} for key, value in result.items())
    print(output)


if __name__ == "__main__":
    main()
