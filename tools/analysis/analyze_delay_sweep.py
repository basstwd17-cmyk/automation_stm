"""Analyze an RTM3004 autonomous HRTIM delay-sweep result."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


HRTIM_TICK_PS = 100_000.0 / 544.0
BLUE = "#2563EB"
BLUE_DARK = "#1E3A5F"
ORANGE = "#D97706"
GOLD = "#B88900"
GREY = "#6B7280"
LIGHT_GREY = "#D1D5DB"
INK = "#111827"
WHITE = "#FFFFFF"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "arialbd.ttf" if bold else "arial.ttf"
    return ImageFont.truetype(str(Path("C:/Windows/Fonts") / name), size)


def draw_axes(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    title: str,
    x_label: str,
    y_label: str,
    x_limits: tuple[float, float],
    y_limits: tuple[float, float],
    x_ticks: int = 5,
    y_ticks: int = 5,
):
    left, top, right, bottom = box
    plot = (left + 105, top + 58, right - 28, bottom - 72)
    px0, py0, px1, py1 = plot
    draw.text((left + 8, top + 6), title, fill=INK, font=font(24, True))
    draw.text((px0, top + 34), y_label, fill=GREY, font=font(15))

    xmin, xmax = x_limits
    ymin, ymax = y_limits

    def map_xy(x: float, y: float) -> tuple[float, float]:
        px = px0 + (x - xmin) * (px1 - px0) / (xmax - xmin)
        py = py1 - (y - ymin) * (py1 - py0) / (ymax - ymin)
        return px, py

    for value in np.linspace(xmin, xmax, x_ticks):
        px, _ = map_xy(float(value), ymin)
        draw.line((px, py0, px, py1), fill="#E5E7EB", width=1)
        label = f"{value:.1f}"
        draw.text((px, py1 + 10), label, fill=GREY, font=font(14), anchor="ma")
    for value in np.linspace(ymin, ymax, y_ticks):
        _, py = map_xy(xmin, float(value))
        draw.line((px0, py, px1, py), fill="#E5E7EB", width=1)
        label = f"{value:.0f}"
        draw.text((px0 - 12, py), label, fill=GREY, font=font(14), anchor="rm")

    draw.line((px0, py0, px0, py1), fill="#9CA3AF", width=2)
    draw.line((px0, py1, px1, py1), fill="#9CA3AF", width=2)
    draw.text(((px0 + px1) / 2, bottom - 24), x_label, fill=GREY, font=font(16), anchor="mm")
    return map_xy, plot


def draw_marker(
    draw: ImageDraw.ImageDraw,
    point: tuple[float, float],
    color: str,
    radius: int = 5,
) -> None:
    x, y = point
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color, outline=WHITE, width=1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    return parser.parse_args()


def load_samples(path: Path) -> np.ndarray:
    with path.open("r", encoding="utf-8", newline="") as stream:
        rows = csv.DictReader(stream)
        return np.asarray([float(row["delay_s"]) * 1e12 for row in rows])


def main() -> None:
    run_dir = parse_args().run_dir.resolve()
    output_dir = run_dir / "analysis"
    output_dir.mkdir(exist_ok=True)

    with (run_dir / "summary.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as stream:
        summary_rows = list(csv.DictReader(stream))

    rows: list[dict[str, object]] = []
    for raw in summary_rows:
        prefix = raw["prefix"]
        samples = load_samples(
            run_dir / prefix / f"{prefix}_delay_samples.csv"
        )
        rows.append(
            {
                "index": int(raw["index"]),
                "set_id": raw["set_id"],
                "prefix": prefix,
                "requested_delay_ps": int(raw["requested_delay_ps"]),
                "signed_ticks": int(raw["signed_ticks"]),
                "actual_delay_ps": float(raw["actual_delay_ps"]),
                "sample_count": int(samples.size),
                "raw_mean_ps": float(np.mean(samples)),
                "raw_median_ps": float(np.median(samples)),
                "raw_stddev_ps": float(np.std(samples, ddof=1)),
                "raw_min_ps": float(np.min(samples)),
                "raw_max_ps": float(np.max(samples)),
            }
        )

    actual = np.asarray([float(row["actual_delay_ps"]) for row in rows])
    raw_mean = np.asarray([float(row["raw_mean_ps"]) for row in rows])
    raw_std = np.asarray([float(row["raw_stddev_ps"]) for row in rows])
    indexes = np.asarray([int(row["index"]) for row in rows])
    set_ids = np.asarray([str(row["set_id"]) for row in rows])

    zero_mask = actual == 0.0
    baseline_ps = float(np.mean(raw_mean[zero_mask]))
    calibrated = raw_mean - baseline_ps
    residual = calibrated - actual

    canonical_mask = (actual != 0.0) | (set_ids == "Z00")
    x_fit = actual[canonical_mask]
    y_fit = calibrated[canonical_mask]
    slope, intercept_ps = np.polyfit(x_fit, y_fit, 1)
    fitted = slope * x_fit + intercept_ps
    fit_residual = y_fit - fitted
    ss_res = float(np.sum((y_fit - fitted) ** 2))
    ss_tot = float(np.sum((y_fit - np.mean(y_fit)) ** 2))
    r_squared = 1.0 - ss_res / ss_tot

    canonical_rows = sorted(
        (row for row in rows if row["actual_delay_ps"] != 0.0 or row["set_id"] == "Z00"),
        key=lambda row: int(row["signed_ticks"]),
    )
    canonical_measured = np.asarray(
        [float(row["raw_mean_ps"]) - baseline_ps for row in canonical_rows]
    )
    step_sizes = np.diff(canonical_measured)
    dnl_ps = step_sizes - HRTIM_TICK_PS

    zero_drift = raw_mean[zero_mask] - baseline_ps
    metrics = {
        "run_dir": str(run_dir),
        "step_count": len(rows),
        "samples_per_step_min": min(int(row["sample_count"]) for row in rows),
        "samples_per_step_max": max(int(row["sample_count"]) for row in rows),
        "total_samples": sum(int(row["sample_count"]) for row in rows),
        "zero_reference_count": int(np.sum(zero_mask)),
        "baseline_offset_ps": baseline_ps,
        "zero_reference_drift_peak_to_peak_ps": float(np.ptp(zero_drift)),
        "gain_slope": float(slope),
        "gain_error_percent": float((slope - 1.0) * 100.0),
        "fit_intercept_ps_after_baseline": float(intercept_ps),
        "fit_r_squared": float(r_squared),
        "ideal_residual_rmse_ps": float(np.sqrt(np.mean(residual[canonical_mask] ** 2))),
        "ideal_residual_max_abs_ps": float(np.max(np.abs(residual[canonical_mask]))),
        "best_fit_inl_max_abs_ps": float(np.max(np.abs(fit_residual))),
        "stddev_mean_ps": float(np.mean(raw_std)),
        "stddev_max_ps": float(np.max(raw_std)),
        "dnl_max_abs_ps": float(np.max(np.abs(dnl_ps))),
        "measured_step_mean_ps": float(np.mean(step_sizes)),
        "hrtim_tick_ps": HRTIM_TICK_PS,
    }

    for row, corrected, error in zip(rows, calibrated, residual):
        row["baseline_corrected_mean_ps"] = float(corrected)
        row["residual_from_ideal_ps"] = float(error)

    fieldnames = list(rows[0].keys())
    with (output_dir / "analysis_metrics.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    (output_dir / "analysis_summary.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )

    image = Image.new("RGB", (2000, 1450), WHITE)
    draw = ImageDraw.Draw(image)
    draw.text((70, 38), "STM32G474 HRTIM delay sweep analysis", fill=INK, font=font(36, True))
    draw.text(
        (72, 88),
        "Run 2026-07-22 18:07 | 50 steps | 100 samples per step | zero-reference calibrated",
        fill=GREY,
        font=font(19),
    )
    draw.line((70, 126, 1930, 126), fill=LIGHT_GREY, width=2)

    panel_boxes = [
        (55, 155, 990, 760),
        (1010, 155, 1945, 760),
        (55, 790, 990, 1390),
        (1010, 790, 1945, 1390),
    ]

    order = np.argsort(x_fit)
    limit = 4.3
    map_xy, _ = draw_axes(
        draw,
        panel_boxes[0],
        "Programmed vs measured delay",
        "Programmed delay (ns)",
        "Offset-corrected measured delay (ns)",
        (-limit, limit),
        (-limit, limit),
        x_ticks=5,
        y_ticks=5,
    )
    ideal_points = [map_xy(-limit, -limit), map_xy(limit, limit)]
    draw.line(ideal_points, fill=GREY, width=3)
    fit_points = [map_xy(float(x_fit[i] / 1000.0), float(fitted[i] / 1000.0)) for i in order]
    draw.line(fit_points, fill=ORANGE, width=4)
    for x, y in zip(x_fit / 1000.0, y_fit / 1000.0):
        draw_marker(draw, map_xy(float(x), float(y)), BLUE, 6)
    draw.text((175, 220), "Ideal y=x", fill=GREY, font=font(16))
    draw.line((130, 229, 165, 229), fill=GREY, width=3)
    draw.text((175, 248), f"Fit slope = {slope:.5f}", fill=ORANGE, font=font(16))
    draw.line((130, 257, 165, 257), fill=ORANGE, width=4)

    residual_order = np.argsort(x_fit)
    ideal_residual = y_fit - x_fit
    residual_limit = max(220.0, float(np.max(np.abs(ideal_residual))) + 25.0)
    map_xy, _ = draw_axes(
        draw,
        panel_boxes[1],
        "Residual from ideal delay",
        "Programmed delay (ns)",
        "Measured - programmed (ps)",
        (-limit, limit),
        (-residual_limit, residual_limit),
        x_ticks=5,
        y_ticks=5,
    )
    draw.line((map_xy(-limit, 0.0), map_xy(limit, 0.0)), fill=INK, width=2)
    draw.line((map_xy(-limit, HRTIM_TICK_PS), map_xy(limit, HRTIM_TICK_PS)), fill=LIGHT_GREY, width=2)
    draw.line((map_xy(-limit, -HRTIM_TICK_PS), map_xy(limit, -HRTIM_TICK_PS)), fill=LIGHT_GREY, width=2)
    residual_points = [
        map_xy(float(x_fit[i] / 1000.0), float(ideal_residual[i]))
        for i in residual_order
    ]
    draw.line(residual_points, fill=BLUE, width=3)
    for point in residual_points:
        draw_marker(draw, point, BLUE, 5)
    draw.rounded_rectangle((1115, 220, 1455, 290), radius=4, fill=WHITE, outline=LIGHT_GREY, width=1)
    draw.text(
        (1130, 231),
        f"Max |residual|: {np.max(np.abs(ideal_residual)):.1f} ps\nBest-fit INL: {np.max(np.abs(fit_residual)):.1f} ps",
        fill=BLUE_DARK,
        font=font(16),
    )

    std_limit = max(40.0, float(np.max(raw_std)) + 6.0)
    map_xy, _ = draw_axes(
        draw,
        panel_boxes[2],
        "Per-step timing precision",
        "Programmed delay (ns)",
        "Sample standard deviation (ps)",
        (-limit, limit),
        (0.0, std_limit),
        x_ticks=5,
        y_ticks=5,
    )
    mean_std = float(np.mean(raw_std))
    draw.line((map_xy(-limit, mean_std), map_xy(limit, mean_std)), fill=ORANGE, width=3)
    for x, y in zip(actual / 1000.0, raw_std):
        draw_marker(draw, map_xy(float(x), float(y)), BLUE, 6)
    draw.text((175, 855), f"Mean precision: {mean_std:.1f} ps", fill=ORANGE, font=font(16))

    zero_indexes = indexes[zero_mask]
    zero_ids = set_ids[zero_mask]
    padding = max(8.0, float(np.max(np.abs(zero_drift))) + 5.0)
    map_xy, _ = draw_axes(
        draw,
        panel_boxes[3],
        "Zero-reference drift",
        "Acquisition index",
        "Offset from zero-reference mean (ps)",
        (0.0, 49.0),
        (-padding, padding),
        x_ticks=6,
        y_ticks=5,
    )
    draw.line((map_xy(0.0, 0.0), map_xy(49.0, 0.0)), fill=INK, width=2)
    zero_points = [map_xy(float(x), float(y)) for x, y in zip(zero_indexes, zero_drift)]
    draw.line(zero_points, fill=GOLD, width=4)
    for index, label, drift in zip(zero_indexes, zero_ids, zero_drift):
        point = map_xy(float(index), float(drift))
        draw_marker(draw, point, GOLD, 7)
        draw.text((point[0], point[1] - 20), str(label), fill=BLUE_DARK, font=font(14), anchor="ms")
    draw.rounded_rectangle((1115, 855, 1465, 925), radius=4, fill=WHITE, outline=LIGHT_GREY, width=1)
    draw.text(
        (1130, 866),
        f"Baseline: {baseline_ps:.1f} ps\nPeak-to-peak drift: {np.ptp(zero_drift):.1f} ps",
        fill=BLUE_DARK,
        font=font(16),
    )
    draw.text(
        (70, 1418),
        "Source: RTM3004 delay samples | Baseline = mean of Z00 and ZR1-ZR5",
        fill=GREY,
        font=font(15),
    )
    image.save(output_dir / "delay_sweep_analysis.png", format="PNG")
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
