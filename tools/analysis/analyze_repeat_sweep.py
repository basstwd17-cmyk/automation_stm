"""Analyze repeated RTM3004 HRTIM delay sweeps without modifying raw data."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from PIL import Image, ImageDraw

from analyze_delay_sweep import (
    BLUE,
    BLUE_DARK,
    GOLD,
    GREY,
    INK,
    LIGHT_GREY,
    ORANGE,
    WHITE,
    draw_axes,
    draw_marker,
    font,
)


HRTIM_TICK_PS = 100_000.0 / 544.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    return parser.parse_args()


def load_samples(path: Path) -> np.ndarray:
    with path.open("r", encoding="utf-8", newline="") as stream:
        rows = csv.DictReader(stream)
        return np.asarray([float(row["delay_s"]) * 1e12 for row in rows])


def quantile(values: np.ndarray, q: float) -> float:
    return float(np.quantile(values, q, method="linear"))


def lag1_autocorrelation(values: np.ndarray) -> float:
    if values.size < 3 or float(np.std(values)) == 0.0:
        return 0.0
    return float(np.corrcoef(values[:-1], values[1:])[0, 1])


def baseline_standard_errors(
    step_indices: np.ndarray,
    zero_indices: np.ndarray,
    zero_mean_variances: np.ndarray,
) -> np.ndarray:
    errors: list[float] = []
    for step_index in step_indices:
        right = int(np.searchsorted(zero_indices, step_index))
        if right < zero_indices.size and zero_indices[right] == step_index:
            variance = zero_mean_variances[right]
        elif right == 0:
            variance = zero_mean_variances[0]
        elif right == zero_indices.size:
            variance = zero_mean_variances[-1]
        else:
            left = right - 1
            weight_right = (
                (step_index - zero_indices[left])
                / (zero_indices[right] - zero_indices[left])
            )
            weight_left = 1.0 - weight_right
            variance = (
                weight_left**2 * zero_mean_variances[left]
                + weight_right**2 * zero_mean_variances[right]
            )
        errors.append(math.sqrt(float(variance)))
    return np.asarray(errors, dtype=float)


def fit_metrics(frame: pd.DataFrame) -> dict[str, float | int]:
    canonical = frame[
        (frame["actual_delay_ps"] != 0.0) | (frame["set_id"] == "Z00")
    ].copy()
    x = canonical["actual_delay_ps"].to_numpy(dtype=float)
    y = canonical["corrected_mean_ps"].to_numpy(dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    fitted = slope * x + intercept
    ss_res = float(np.sum((y - fitted) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    residual = y - x

    ordered = canonical.sort_values("signed_ticks")
    measured_steps = np.diff(ordered["corrected_mean_ps"].to_numpy(dtype=float))
    dnl = measured_steps - HRTIM_TICK_PS

    return {
        "slope": float(slope),
        "gain_error_percent": float((slope - 1.0) * 100.0),
        "intercept_ps": float(intercept),
        "r_squared": float(1.0 - ss_res / ss_tot),
        "ideal_residual_rmse_ps": float(np.sqrt(np.mean(residual**2))),
        "ideal_residual_max_abs_ps": float(np.max(np.abs(residual))),
        "best_fit_inl_max_abs_ps": float(np.max(np.abs(y - fitted))),
        "dnl_max_abs_ps": float(np.max(np.abs(dnl))),
        "measured_step_mean_ps": float(np.mean(measured_steps)),
        "monotonic_violations": int(np.sum(measured_steps <= 0.0)),
        "measured_span_ps": float(np.ptp(y)),
    }


def draw_chart(
    run_dir: Path,
    metrics: pd.DataFrame,
    comparison: pd.DataFrame,
    repeat_summaries: dict[int, dict[str, Any]],
    output_path: Path,
) -> None:
    image = Image.new("RGB", (2000, 1500), WHITE)
    draw = ImageDraw.Draw(image)
    draw.text(
        (70, 36),
        "STM32G474 repeated delay-sweep analysis",
        fill=INK,
        font=font(36, True),
    )
    draw.text(
        (72, 88),
        (
            f"{run_dir.name} | 2 repeats × 50 steps | "
            "100 samples per step | local zero-reference corrected"
        ),
        fill=GREY,
        font=font(19),
    )
    draw.line((70, 126, 1930, 126), fill=LIGHT_GREY, width=2)

    panel_boxes = [
        (55, 155, 990, 770),
        (1010, 155, 1945, 770),
        (55, 800, 990, 1410),
        (1010, 800, 1945, 1410),
    ]
    colors = {1: BLUE, 2: ORANGE}
    limit_ns = 4.3

    map_xy, _ = draw_axes(
        draw,
        panel_boxes[0],
        "Programmed vs measured delay",
        "Programmed delay (ns)",
        "Zero-corrected mean delay (ns)",
        (-limit_ns, limit_ns),
        (-limit_ns, limit_ns),
        x_ticks=5,
        y_ticks=5,
    )
    draw.line(
        (map_xy(-limit_ns, -limit_ns), map_xy(limit_ns, limit_ns)),
        fill=GREY,
        width=3,
    )
    for repeat in sorted(metrics["repeat"].unique()):
        subset = metrics[
            (metrics["repeat"] == repeat)
            & ((metrics["actual_delay_ps"] != 0.0) | (metrics["set_id"] == "Z00"))
        ].sort_values("actual_delay_ps")
        points = [
            map_xy(float(x / 1000.0), float(y / 1000.0))
            for x, y in zip(
                subset["actual_delay_ps"],
                subset["corrected_mean_ps"],
            )
        ]
        draw.line(points, fill=colors[int(repeat)], width=3)
        for point in points:
            draw_marker(draw, point, colors[int(repeat)], 5)
    draw.text((170, 220), "R1", fill=BLUE, font=font(16, True))
    draw.line((125, 230, 160, 230), fill=BLUE, width=4)
    draw.text((170, 250), "R2", fill=ORANGE, font=font(16, True))
    draw.line((125, 260, 160, 260), fill=ORANGE, width=4)

    canonical_comparison = comparison[
        (comparison["actual_delay_ps"] != 0.0) | (comparison["set_id"] == "Z00")
    ].sort_values("actual_delay_ps")
    delta_limit = max(
        30.0,
        float(np.max(np.abs(canonical_comparison["delta_corrected_mean_ps"]))) + 8.0,
    )
    map_xy, _ = draw_axes(
        draw,
        panel_boxes[1],
        "Repeat-to-repeat shift",
        "Programmed delay (ns)",
        "R2 - R1 corrected mean (ps)",
        (-limit_ns, limit_ns),
        (-delta_limit, delta_limit),
        x_ticks=5,
        y_ticks=5,
    )
    draw.line(
        (map_xy(-limit_ns, 0.0), map_xy(limit_ns, 0.0)),
        fill=INK,
        width=2,
    )
    delta_points = [
        map_xy(float(x / 1000.0), float(y))
        for x, y in zip(
            canonical_comparison["actual_delay_ps"],
            canonical_comparison["delta_corrected_mean_ps"],
        )
    ]
    draw.line(delta_points, fill=BLUE, width=3)
    for point in delta_points:
        draw_marker(draw, point, BLUE, 5)

    jitter_limit = max(40.0, float(metrics["raw_stddev_ps"].max()) + 6.0)
    map_xy, _ = draw_axes(
        draw,
        panel_boxes[2],
        "Per-step timing jitter",
        "Programmed delay (ns)",
        "Sample standard deviation (ps)",
        (-limit_ns, limit_ns),
        (0.0, jitter_limit),
        x_ticks=5,
        y_ticks=5,
    )
    for repeat in sorted(metrics["repeat"].unique()):
        subset = metrics[metrics["repeat"] == repeat].sort_values("actual_delay_ps")
        points = [
            map_xy(float(x / 1000.0), float(y))
            for x, y in zip(subset["actual_delay_ps"], subset["raw_stddev_ps"])
        ]
        for point in points:
            draw_marker(draw, point, colors[int(repeat)], 6)
    draw.text(
        (170, 865),
        (
            f"R1 mean {repeat_summaries[1]['jitter_mean_ps']:.1f} ps  |  "
            f"R2 mean {repeat_summaries[2]['jitter_mean_ps']:.1f} ps"
        ),
        fill=BLUE_DARK,
        font=font(16),
    )

    zero_rows = metrics[metrics["actual_delay_ps"] == 0.0].copy()
    zero_center = float(zero_rows["raw_mean_ps"].mean())
    zero_rows["centered_zero_ps"] = zero_rows["raw_mean_ps"] - zero_center
    zero_limit = max(20.0, float(zero_rows["centered_zero_ps"].abs().max()) + 6.0)
    map_xy, _ = draw_axes(
        draw,
        panel_boxes[3],
        "Zero-reference stability",
        "Step index within repeat",
        "Raw zero mean - global zero mean (ps)",
        (0.0, 49.0),
        (-zero_limit, zero_limit),
        x_ticks=6,
        y_ticks=5,
    )
    draw.line(
        (map_xy(0.0, 0.0), map_xy(49.0, 0.0)),
        fill=INK,
        width=2,
    )
    for repeat in sorted(zero_rows["repeat"].unique()):
        subset = zero_rows[zero_rows["repeat"] == repeat].sort_values("local_index")
        points = [
            map_xy(float(x), float(y))
            for x, y in zip(subset["local_index"], subset["centered_zero_ps"])
        ]
        draw.line(points, fill=colors[int(repeat)], width=4)
        for point in points:
            draw_marker(draw, point, colors[int(repeat)], 7)

    draw.text(
        (70, 1442),
        (
            "Source: 10,000 RTM3004 delay samples | "
            "Baseline: piecewise-linear interpolation of Z00 and ZR1-ZR5"
        ),
        fill=GREY,
        font=font(15),
    )
    image.save(output_path, format="PNG")


def write_notebook(
    output_path: Path,
    run_dir: Path,
    summary: dict[str, Any],
) -> None:
    comparison_path = run_dir / "analysis_repeat" / "repeat_comparison.csv"
    metrics_path = run_dir / "analysis_repeat" / "repeat_metrics.csv"
    notebook = {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# 반복 지연 sweep 분석\n",
                    "\n",
                    "## tl;dr\n",
                    "\n",
                    (
                        f"- R2−R1 보정 평균 변화의 RMS는 "
                        f"`{summary['comparison']['delta_rms_ps']:.2f} ps`, "
                        f"최대 절댓값은 "
                        f"`{summary['comparison']['delta_max_abs_ps']:.2f} ps`입니다.\n"
                    ),
                    (
                        f"- 단계 내부 지터 평균은 R1 "
                        f"`{summary['repeats']['1']['jitter_mean_ps']:.2f} ps`, "
                        f"R2 `{summary['repeats']['2']['jitter_mean_ps']:.2f} ps`입니다.\n"
                    ),
                ],
            },
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "## Context & Methods\n",
                    "\n",
                    f"- 원본 실행: `{run_dir}`\n",
                    "- 각 반복 안의 0ps 기준점들을 단계 인덱스로 선형 보간했습니다.\n",
                    "- 목표값 비교에는 `actual_delay_ps`를 사용했습니다.\n",
                ],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [
                    "from pathlib import Path\n",
                    "import json\n",
                    "import pandas as pd\n",
                    f"run_dir = Path(r'{run_dir}')\n",
                    f"metrics = pd.read_csv(Path(r'{metrics_path}'))\n",
                    f"comparison = pd.read_csv(Path(r'{comparison_path}'))\n",
                    "summary = json.loads((run_dir / 'analysis_repeat' / 'repeat_summary.json').read_text())\n",
                    "summary\n",
                ],
            },
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "## Data\n",
                    "\n",
                    "세부 계산 결과는 `repeat_metrics.csv`와 `repeat_comparison.csv`에 저장됩니다.\n",
                ],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [
                    "comparison[['set_id', 'actual_delay_ps', 'corrected_mean_ps_r1', "
                    "'corrected_mean_ps_r2', 'delta_corrected_mean_ps', "
                    "'raw_stddev_ps_r1', 'raw_stddev_ps_r2']]\n",
                ],
            },
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "## Results\n",
                    "\n",
                    "![반복 sweep 분석](repeat_sweep_analysis.png)\n",
                ],
            },
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "## Takeaways\n",
                    "\n",
                    "- 반복 사이 평균 이동과 단계 내부 지터를 별도로 해석합니다.\n",
                    "- 절대 정확도 오차와 반복성 오차를 혼합하지 않습니다.\n",
                ],
            },
        ],
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    output_path.write_text(
        json.dumps(notebook, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    run_dir = parse_args().run_dir.resolve()
    output_dir = run_dir / "analysis_repeat"
    output_dir.mkdir(exist_ok=True)

    status = json.loads((run_dir / "run_status.json").read_text(encoding="utf-8"))
    with (run_dir / "summary.csv").open(
        "r", encoding="utf-8-sig", newline=""
    ) as stream:
        summary_rows = list(csv.DictReader(stream))

    repeat_counts: dict[int, int] = {}
    records: list[dict[str, Any]] = []
    for raw in summary_rows:
        repeat = int(raw["repeat"])
        local_index = repeat_counts.get(repeat, 0)
        repeat_counts[repeat] = local_index + 1
        prefix = raw["prefix"]
        sample_path = run_dir / prefix / f"{prefix}_delay_samples.csv"
        samples = load_samples(sample_path)
        median = float(np.median(samples))
        mad = float(np.median(np.abs(samples - median)))
        sample_stddev = float(np.std(samples, ddof=1))
        autocorrelation = lag1_autocorrelation(samples)
        effective_sample_count = max(
            1.0,
            min(
                float(samples.size),
                float(samples.size) * (1.0 - autocorrelation)
                / (1.0 + autocorrelation),
            ),
        )
        records.append(
            {
                "index": int(raw["index"]),
                "local_index": local_index,
                "repeat": repeat,
                "set_id": raw["set_id"],
                "prefix": prefix,
                "requested_delay_ps": int(raw["requested_delay_ps"]),
                "signed_ticks": int(raw["signed_ticks"]),
                "actual_delay_ps": float(raw["actual_delay_ps"]),
                "sample_count": int(samples.size),
                "raw_mean_ps": float(np.mean(samples)),
                "raw_median_ps": median,
                "raw_stddev_ps": sample_stddev,
                "lag1_autocorrelation": autocorrelation,
                "effective_sample_count_ar1": effective_sample_count,
                "raw_mean_standard_error_ps": (
                    sample_stddev / math.sqrt(effective_sample_count)
                ),
                "robust_sigma_ps": float(1.4826 * mad),
                "p05_ps": quantile(samples, 0.05),
                "p95_ps": quantile(samples, 0.95),
                "raw_min_ps": float(np.min(samples)),
                "raw_max_ps": float(np.max(samples)),
            }
        )

    metrics = pd.DataFrame.from_records(records)
    for repeat in sorted(metrics["repeat"].unique()):
        mask = metrics["repeat"] == repeat
        subset = metrics.loc[mask]
        zero = subset[subset["actual_delay_ps"] == 0.0].sort_values("local_index")
        zero_mean_variances = (
            zero["raw_mean_standard_error_ps"].to_numpy(dtype=float) ** 2
        )
        baseline = np.interp(
            subset["local_index"].to_numpy(dtype=float),
            zero["local_index"].to_numpy(dtype=float),
            zero["raw_mean_ps"].to_numpy(dtype=float),
        )
        metrics.loc[mask, "baseline_ps"] = baseline
        baseline_errors = baseline_standard_errors(
            subset["local_index"].to_numpy(dtype=float),
            zero["local_index"].to_numpy(dtype=float),
            zero_mean_variances,
        )
        metrics.loc[mask, "baseline_standard_error_ps"] = baseline_errors
        metrics.loc[mask, "corrected_mean_ps"] = (
            subset["raw_mean_ps"].to_numpy(dtype=float) - baseline
        )
        corrected_errors = np.sqrt(
            subset["raw_mean_standard_error_ps"].to_numpy(dtype=float) ** 2
            + baseline_errors**2
        )
        corrected_errors[
            subset["actual_delay_ps"].to_numpy(dtype=float) == 0.0
        ] = 0.0
        metrics.loc[mask, "corrected_mean_standard_error_ps"] = corrected_errors
        metrics.loc[mask, "residual_from_ideal_ps"] = (
            metrics.loc[mask, "corrected_mean_ps"].to_numpy(dtype=float)
            - subset["actual_delay_ps"].to_numpy(dtype=float)
        )

    r1 = metrics[metrics["repeat"] == 1].copy()
    r2 = metrics[metrics["repeat"] == 2].copy()
    comparison = r1.merge(
        r2,
        on=["set_id", "signed_ticks", "actual_delay_ps"],
        suffixes=("_r1", "_r2"),
        validate="one_to_one",
    )
    comparison["delta_raw_mean_ps"] = (
        comparison["raw_mean_ps_r2"] - comparison["raw_mean_ps_r1"]
    )
    comparison["delta_corrected_mean_ps"] = (
        comparison["corrected_mean_ps_r2"] - comparison["corrected_mean_ps_r1"]
    )
    comparison["delta_stddev_ps"] = (
        comparison["raw_stddev_ps_r2"] - comparison["raw_stddev_ps_r1"]
    )
    comparison["delta_standard_error_ps"] = np.sqrt(
        comparison["corrected_mean_standard_error_ps_r1"] ** 2
        + comparison["corrected_mean_standard_error_ps_r2"] ** 2
    )
    comparison["delta_z_score"] = np.divide(
        comparison["delta_corrected_mean_ps"],
        comparison["delta_standard_error_ps"],
        out=np.zeros(len(comparison), dtype=float),
        where=comparison["delta_standard_error_ps"].to_numpy(dtype=float) > 0.0,
    )
    comparison["delta_p_value_approx"] = [
        math.erfc(abs(float(value)) / math.sqrt(2.0))
        for value in comparison["delta_z_score"]
    ]

    repeat_summaries: dict[int, dict[str, Any]] = {}
    for repeat in sorted(metrics["repeat"].unique()):
        subset = metrics[metrics["repeat"] == repeat]
        zero = subset[subset["actual_delay_ps"] == 0.0]
        repeat_summaries[int(repeat)] = {
            "step_count": int(len(subset)),
            "sample_count": int(subset["sample_count"].sum()),
            "zero_reference_count": int(len(zero)),
            "zero_raw_mean_ps": float(zero["raw_mean_ps"].mean()),
            "zero_drift_peak_to_peak_ps": float(np.ptp(zero["raw_mean_ps"])),
            "jitter_mean_ps": float(subset["raw_stddev_ps"].mean()),
            "jitter_median_ps": float(subset["raw_stddev_ps"].median()),
            "jitter_max_ps": float(subset["raw_stddev_ps"].max()),
            "jitter_max_set_id": str(
                subset.loc[subset["raw_stddev_ps"].idxmax(), "set_id"]
            ),
            "lag1_autocorrelation_median": float(
                subset["lag1_autocorrelation"].median()
            ),
            "effective_sample_count_median": float(
                subset["effective_sample_count_ar1"].median()
            ),
            **fit_metrics(subset),
        }

    canonical = comparison[
        (comparison["actual_delay_ps"] != 0.0) | (comparison["set_id"] == "Z00")
    ]
    nonzero_comparison = comparison[comparison["actual_delay_ps"] != 0.0]
    bonferroni_alpha = 0.05 / len(nonzero_comparison)
    deltas = nonzero_comparison["delta_corrected_mean_ps"].to_numpy(dtype=float)
    max_delta_index = (
        nonzero_comparison["delta_corrected_mean_ps"].abs().idxmax()
    )
    all_sample_values: list[np.ndarray] = []
    for record in records:
        prefix = str(record["prefix"])
        all_sample_values.append(
            load_samples(run_dir / prefix / f"{prefix}_delay_samples.csv")
        )
    unique_samples = np.unique(np.concatenate(all_sample_values))
    positive_steps = np.diff(unique_samples)
    positive_steps = positive_steps[positive_steps > 1e-9]

    summary = {
        "run_dir": str(run_dir),
        "run_status": status,
        "data_quality": {
            "summary_rows": int(len(metrics)),
            "repeat_counts": {
                str(key): int(value) for key, value in repeat_counts.items()
            },
            "total_samples": int(metrics["sample_count"].sum()),
            "all_quality_ok": bool(
                all(raw["quality_status"] == "ok" for raw in summary_rows)
            ),
            "mixed_delay_files": len(
                list(run_dir.rglob("*_mixed_delay_samples.csv"))
            ),
            "partial_delay_files": len(
                list(run_dir.rglob("*_partial_delay_samples.csv"))
            ),
            "sample_quantization_ps": (
                float(np.min(positive_steps)) if positive_steps.size else None
            ),
        },
        "repeats": {
            str(key): value for key, value in repeat_summaries.items()
        },
        "comparison": {
            "canonical_delay_count": int(len(canonical)),
            "nonzero_delay_count": int(len(nonzero_comparison)),
            "delta_statistics_scope": "44 nonzero programmed delays",
            "delta_mean_ps": float(np.mean(deltas)),
            "delta_median_ps": float(np.median(deltas)),
            "delta_mean_abs_ps": float(np.mean(np.abs(deltas))),
            "delta_rms_ps": float(np.sqrt(np.mean(deltas**2))),
            "delta_max_abs_ps": float(np.max(np.abs(deltas))),
            "delta_max_set_id": str(comparison.loc[max_delta_index, "set_id"]),
            "delta_max_actual_ps": float(
                comparison.loc[max_delta_index, "actual_delay_ps"]
            ),
            "significant_shift_count_approx_95pct": int(
                np.sum(np.abs(nonzero_comparison["delta_z_score"]) > 1.96)
            ),
            "bonferroni_alpha": float(bonferroni_alpha),
            "bonferroni_significant_shift_count": int(
                np.sum(
                    nonzero_comparison["delta_p_value_approx"]
                    < bonferroni_alpha
                )
            ),
            "raw_zero_offset_shift_ps": float(
                repeat_summaries[2]["zero_raw_mean_ps"]
                - repeat_summaries[1]["zero_raw_mean_ps"]
            ),
            "gain_slope_change": float(
                repeat_summaries[2]["slope"] - repeat_summaries[1]["slope"]
            ),
            "gain_error_change_percentage_points": float(
                repeat_summaries[2]["gain_error_percent"]
                - repeat_summaries[1]["gain_error_percent"]
            ),
            "uncertainty_model": (
                "AR(1)-adjusted sample mean plus interpolated zero-baseline "
                "uncertainty; z threshold is unadjusted for multiple comparisons"
            ),
        },
    }

    metrics.to_csv(output_dir / "repeat_metrics.csv", index=False)
    comparison.to_csv(output_dir / "repeat_comparison.csv", index=False)
    (output_dir / "repeat_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    draw_chart(
        run_dir,
        metrics,
        comparison,
        repeat_summaries,
        output_dir / "repeat_sweep_analysis.png",
    )
    write_notebook(
        output_dir / "repeat_sweep_analysis.ipynb",
        run_dir,
        summary,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
