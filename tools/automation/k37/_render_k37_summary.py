"""Render one PNG overview from a completed K37 result folder."""

from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D


RESULT_DIR = Path(__file__).resolve().parents[3] / "output/k37_spectrum/20260728_100203_main"
OUTPUT_PATH = RESULT_DIR / "k37_spectrum_summary.png"
BANDS_MHZ = ((6.0, 300.0), (300.0, 600.0), (600.0, 900.0), (900.0, 1200.0))
COLORS = ("#2563eb", "#dc2626", "#059669", "#7c3aed")


def delay_label(delay_ns: float) -> str:
    return f"{delay_ns:+.3f} ns" if delay_ns else "0 ns"


def load_records() -> list[dict[str, object]]:
    with (RESULT_DIR / "summary.csv").open(encoding="utf-8-sig", newline="") as stream:
        summary = list(csv.DictReader(stream))

    records: list[dict[str, object]] = []
    for row in summary:
        prefix = row["prefix"]
        with (RESULT_DIR / prefix / f"{prefix}_spectrum.csv").open(
            encoding="utf-8-sig", newline=""
        ) as stream:
            spectrum = list(csv.DictReader(stream))
        values = [
            (float(item["frequency_hz"]) / 1e6, float(item["average_level"]))
            for item in spectrum
        ]
        records.append(
            {
                "prefix": prefix,
                "channel": row["source"],
                "delay_ns": float(row["actual_delay_ps"]) / 1000.0,
                "captured": int(row["captured_acquisitions"]),
                "planned": int(row["planned_acquisitions"]),
                "bands": [
                    statistics.fmean(
                        level
                        for frequency, level in values
                        if low <= frequency < high
                    )
                    for low, high in BANDS_MHZ
                ],
            }
        )
    return records


def main() -> None:
    global RESULT_DIR, OUTPUT_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, nargs="?", default=RESULT_DIR)
    args = parser.parse_args()
    RESULT_DIR = args.run_dir.resolve()
    OUTPUT_PATH = RESULT_DIR / "k37_spectrum_summary.png"
    records = load_records()
    figure = plt.figure(figsize=(12, 8), constrained_layout=True)
    grid = figure.add_gridspec(2, 2, height_ratios=(1.25, 1.0))
    axes = [figure.add_subplot(grid[0, column]) for column in range(2)]
    capture_axis = figure.add_subplot(grid[1, :])
    band_labels = [f"{low:.0f}-{high:.0f}" for low, high in BANDS_MHZ]

    for axis, channel in zip(axes, ("CH1", "CH2"), strict=True):
        series = sorted(
            (record for record in records if record["channel"] == channel),
            key=lambda record: float(record["delay_ns"]),
        )
        for color, record in zip(COLORS, series, strict=True):
            axis.plot(
                band_labels,
                record["bands"],
                marker="o",
                linewidth=2,
                color=color,
                label=delay_label(float(record["delay_ns"])),
            )
        axis.set_title(f"{channel}: band-averaged Spectrum")
        axis.set_ylabel("Average level (dBV)")
        axis.set_ylim(-68.5, -55.5)
        axis.grid(axis="y", alpha=0.3)
        axis.legend(title="Actual delay", fontsize=8, title_fontsize=8)

    labels = [str(record["prefix"]).replace("_T", "\nT") for record in records]
    positions = list(range(len(records)))
    planned = [int(record["planned"]) for record in records]
    captured = [int(record["captured"]) for record in records]
    capture_axis.bar(positions, planned, width=0.62, color="#d1d5db", label="Planned")
    capture_colors = ["#2563eb" if record["channel"] == "CH1" else "#dc2626" for record in records]
    capture_axis.bar(positions, captured, width=0.40, color=capture_colors, label="Captured")
    for position, count in zip(positions, captured, strict=True):
        capture_axis.text(position, count + 2, str(count), ha="center", va="bottom", fontsize=9)
    capture_axis.set_xticks(positions, labels)
    capture_axis.set_ylim(0, 112)
    capture_axis.set_ylabel("Scope capture count")
    capture_axis.set_title("Planned vs actual capture count")
    capture_axis.grid(axis="y", alpha=0.3)
    capture_axis.legend(
        handles=[
            plt.Rectangle((0, 0), 1, 1, color="#d1d5db", label="Planned (100)"),
            Line2D([0], [0], color="#2563eb", linewidth=8, label="Captured CH1"),
            Line2D([0], [0], color="#dc2626", linewidth=8, label="Captured CH2"),
        ],
        loc="upper right",
    )
    figure.suptitle(
        "K37 Spectrum Overview — diagnostic result, not a completed 100-capture average",
        fontsize=14,
        fontweight="bold",
    )
    figure.savefig(OUTPUT_PATH, dpi=180, bbox_inches="tight")
    print(OUTPUT_PATH)


if __name__ == "__main__":
    main()
