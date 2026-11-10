"""Create report-ready metrics and figures for a K15 IN/C1/C2 run.

The input reference must be captured on C3 and each CSV row must contain a
valid rising 50 % crossing.  Metrics use one same-index history segment:
  d1 = t50(C1)-t50(IN), d2 = t50(C2)-t50(IN), skew = t50(C2)-t50(C1).
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
from pathlib import Path


def q(values: list[float], fraction: float) -> float:
    values = sorted(values)
    position = fraction * (len(values) - 1)
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return values[low]
    return values[low] + (values[high] - values[low]) * (position - low)


def metrics(values_s: list[float]) -> dict[str, float]:
    if len(values_s) < 2:
        raise ValueError("At least two valid samples are required")
    p01, p1, p99, p999 = (q(values_s, x) for x in (0.001, 0.01, 0.99, 0.999))
    return {
        "n": len(values_s),
        "mean_ps": statistics.mean(values_s) * 1e12,
        "sigma_ps_rms": statistics.stdev(values_s) * 1e12,
        "min_ps": min(values_s) * 1e12,
        "max_ps": max(values_s) * 1e12,
        "p99_p1_ps": (p99 - p1) * 1e12,
        "p999_p01_ps": (p999 - p01) * 1e12,
    }


def read_step(step_dir: Path, startup_segments: int) -> tuple[dict[str, list[float]], list[float]]:
    segments = next(step_dir.glob("*_segments.csv"))
    values = {"d1": [], "d2": [], "skew": []}
    timestamps: list[float] = []
    with segments.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["analysis_status"] != "ok" or int(row["segment_index"]) <= startup_segments:
                continue
            try:
                c1 = float(row["c1_relative_50_time_s"])
                c2 = float(row["c2_relative_50_time_s"])
                cin = float(row["c3_relative_50_time_s"])
                timestamp = float(row["relative_timestamp_s"])
            except (KeyError, ValueError) as error:
                raise ValueError(f"Invalid C1/C2/C3 edge row in {segments}") from error
            values["d1"].append(c1 - cin)
            values["d2"].append(c2 - cin)
            values["skew"].append(c2 - c1)
            timestamps.append(timestamp)
    return values, timestamps


def clock_metrics(timestamps: list[float]) -> dict[str, float] | None:
    if len(timestamps) < 4:
        return None
    periods = [right - left for left, right in zip(timestamps, timestamps[1:])]
    ideal_period = statistics.mean(periods)
    tie = [timestamp - (timestamps[0] + index * ideal_period)
           for index, timestamp in enumerate(timestamps)]
    c2c = [right - left for left, right in zip(periods, periods[1:])]
    return {
        "mean_period_s": ideal_period,
        "period_jitter_sigma_ps_rms": statistics.stdev(periods) * 1e12,
        "cycle_to_cycle_sigma_ps_rms": statistics.stdev(c2c) * 1e12,
        "tie_sigma_ps_rms": statistics.stdev(tie) * 1e12,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for derived results (default: <run_dir>/analysis_input_output)",
    )
    parser.add_argument(
        "--analyze-input-clock",
        action="store_true",
        help="Experimental: derive period/C2C/TIE from history timestamps. Use only after timestamp cadence is independently validated.",
    )
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    output_dir = (args.output_dir or run_dir / "analysis_input_output").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = run_dir / "summary.csv"
    with summary_path.open(encoding="utf-8-sig", newline="") as stream:
        summary = list(csv.DictReader(stream))
    if not summary:
        raise SystemExit("summary.csv has no conditions")

    rows: list[dict[str, object]] = []
    traces: list[tuple[str, dict[str, list[float]]]] = []
    for condition in summary:
        prefix = str(condition["prefix"])
        startup = int(condition["startup_segments"])
        values, timestamps = read_step(run_dir / prefix, startup)
        result: dict[str, object] = {
            "condition": condition["set_id"],
            "requested_delay_ps": float(condition["requested_delay_ps"]),
            "actual_delay_ps": float(condition["actual_delay_ps"]),
        }
        for label, samples in values.items():
            for name, value in metrics(samples).items():
                result[f"{label}_{name}"] = value
        result["skew_accuracy_ps"] = (
            float(result["skew_mean_ps"]) - float(result["actual_delay_ps"])
        )
        if args.analyze_input_clock:
            input_clock = clock_metrics(timestamps)
            if input_clock:
                result.update(input_clock)
        rows.append(result)
        traces.append((prefix, values))

    output_csv = output_dir / "input_output_jitter_summary.csv"
    fields = list(rows[0])
    with output_csv.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    report = output_dir / "input_output_jitter_report.md"
    lines = [
        "# IN/C1/C2 timing measurement",
        "",
        "Edge definition: rising-edge 50% crossing. d1=C1-IN, d2=C2-IN, skew=C2-C1.",
        "Period/C2C/TIE are intentionally omitted unless `--analyze-input-clock` is used after independently validating the RTM history timestamps.",
        "",
        "| Condition | N | C1 propagation mean / sigma | C2 propagation mean / sigma | skew mean / sigma | skew accuracy |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['condition']} | {row['d1_n']} | "
            f"{row['d1_mean_ps']:.1f} / {row['d1_sigma_ps_rms']:.1f} ps | "
            f"{row['d2_mean_ps']:.1f} / {row['d2_sigma_ps_rms']:.1f} ps | "
            f"{row['skew_mean_ps']:.1f} / {row['skew_sigma_ps_rms']:.1f} ps | "
            f"{row['skew_accuracy_ps']:.1f} ps |"
        )
    lines += [
        "",
        "`*_summary.csv` also records P99-P1 and P99.9-P0.1 ranges. Do not use a run if its condition mean fails to follow the requested delay, or if C3 edge-quality checks fail.",
    ]
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print(f"Wrote {output_csv} and {report}; matplotlib unavailable, figures skipped.")
        return

    figure, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
    labels = [("d1", "C1 - IN"), ("d2", "C2 - IN"), ("skew", "C2 - C1")]
    for axis, (key, title) in zip(axes, labels):
        for prefix, values in traces:
            centered = [(x - statistics.mean(values[key])) * 1e12 for x in values[key]]
            axis.plot(centered, ".", ms=2, label=prefix)
        axis.set_ylabel(f"{title}\nresidual (ps)")
        axis.grid(alpha=0.25)
    axes[0].legend(ncol=4, fontsize=8)
    axes[-1].set_xlabel("capture index")
    figure.tight_layout()
    figure.savefig(output_dir / "input_output_jitter_timeseries.png", dpi=180)
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(8, 4.5))
    actual = [float(row["actual_delay_ps"]) for row in rows]
    measured = [float(row["skew_mean_ps"]) for row in rows]
    errors = [float(row["skew_accuracy_ps"]) for row in rows]
    axis.plot(actual, measured, "o-", label="mean(C2-C1)")
    axis.plot(actual, actual, "--", color="gray", label="ideal")
    axis.set_xlabel("configured relative delay (ps)")
    axis.set_ylabel("measured relative delay (ps)")
    axis.grid(alpha=0.25)
    axis.legend()
    inset = axis.inset_axes([0.62, 0.14, 0.32, 0.32])
    inset.axhline(0, color="gray", lw=1)
    inset.plot(actual, errors, "o-")
    inset.set_title("accuracy error (ps)", fontsize=8)
    inset.tick_params(labelsize=7)
    figure.tight_layout()
    figure.savefig(output_dir / "relative_delay_accuracy.png", dpi=180)
    plt.close(figure)
    print(f"Wrote {output_csv}, {report}, and PNG figures in {output_dir}")


if __name__ == "__main__":
    main()
