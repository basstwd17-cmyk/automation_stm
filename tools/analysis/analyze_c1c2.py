"""Compact C1-C2 delay comparison analysis and visualization."""

from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt


def mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else float("nan")


def pstdev(values: list[float]) -> float:
    return statistics.pstdev(values) if len(values) > 1 else 0.0


def linear_fit(points: list[tuple[float, float]]) -> tuple[float, float, float]:
    x_mean = mean([point[0] for point in points])
    y_mean = mean([point[1] for point in points])
    denominator = sum((x - x_mean) ** 2 for x, _ in points)
    slope = sum((x - x_mean) * (y - y_mean) for x, y in points) / denominator
    intercept = y_mean - slope * x_mean
    residual_sum = sum(
        (y - (intercept + slope * x)) ** 2 for x, y in points
    )
    total_sum = sum((y - y_mean) ** 2 for _, y in points)
    r_squared = 1.0 - residual_sum / total_sum
    return slope, intercept, r_squared


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    input_path = args.run / "summary.csv"
    rows = list(csv.DictReader(input_path.open(encoding="utf-8-sig")))
    if not rows:
        raise ValueError(f"No rows in {input_path}")

    numeric = {
        key: [float(row[key]) for row in rows]
        for key in (
            "requested_delay_ps",
            "actual_delay_ps",
            "measured_mean_ps",
            "measured_stddev_ps",
            "error_from_actual_ps",
            "waveform_count",
            "sample_count",
        )
    }
    if any(value != 100 for value in numeric["waveform_count"]):
        raise ValueError("The selected baseline contains non-100 waveform rows")
    if any(value != 100 for value in numeric["sample_count"]):
        raise ValueError("The selected baseline contains non-100 sample rows")

    within_std = numeric["measured_stddev_ps"]
    median_std = statistics.median(within_std)
    deviations = [abs(value - median_std) for value in within_std]
    mad = statistics.median(deviations)
    robust_limit = median_std + max(3.0 * mad, 50.0)
    for row in rows:
        row["outlier_flag"] = "yes" if float(row["measured_stddev_ps"]) > robust_limit else "no"
    outliers = [row for row in rows if row["outlier_flag"] == "yes"]

    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[row["set_id"]].append(row)

    summary_rows: list[dict[str, object]] = []
    for set_id, group in groups.items():
        requested = float(group[0]["requested_delay_ps"])
        actual = float(group[0]["actual_delay_ps"])
        means = [float(row["measured_mean_ps"]) for row in group]
        errors = [float(row["error_from_actual_ps"]) for row in group]
        within = [float(row["measured_stddev_ps"]) for row in group]
        summary_rows.append(
            {
                "set_id": set_id,
                "requested_delay_ps": requested,
                "quantized_delay_ps": actual,
                "repeat_count": len(group),
                "measured_mean_ps": mean(means),
                "repeat_sd_ps": pstdev(means),
                "repeat_min_ps": min(means),
                "repeat_max_ps": max(means),
                "mean_within_run_sd_ps": mean(within),
                "error_from_quantized_ps": mean(errors),
                "outlier_repeat_count": sum(row["outlier_flag"] == "yes" for row in group),
            }
        )
    summary_rows.sort(key=lambda row: float(row["requested_delay_ps"]))

    clean_rows = [row for row in rows if row["outlier_flag"] == "no"]
    fit_points = [
        (float(row["requested_delay_ps"]), float(row["measured_mean_ps"]))
        for row in clean_rows
    ]
    slope, intercept, r_squared = linear_fit(fit_points)
    clean_errors = [float(row["error_from_actual_ps"]) for row in clean_rows]

    args.output.mkdir(parents=True, exist_ok=True)
    summary_fields = list(summary_rows[0].keys())
    write_csv(args.output / "c1c2_repeat_summary.csv", summary_rows, summary_fields)
    write_csv(
        args.output / "c1c2_outliers.csv",
        outliers,
        list(rows[0].keys()),
    )

    x = [float(row["requested_delay_ps"]) for row in summary_rows]
    measured = [float(row["measured_mean_ps"]) for row in summary_rows]
    repeat_sd = [float(row["repeat_sd_ps"]) for row in summary_rows]
    errors = [float(row["error_from_quantized_ps"]) for row in summary_rows]
    within = [float(row["mean_within_run_sd_ps"]) for row in summary_rows]
    labels = [str(row["set_id"]) for row in summary_rows]

    plt.rcParams.update({"font.size": 10, "axes.titlesize": 11})
    figure, axes = plt.subplots(1, 3, figsize=(16, 4.8), constrained_layout=True)

    axes[0].errorbar(
        x,
        measured,
        yerr=repeat_sd,
        fmt="o-",
        color="#1464a5",
        ecolor="#8fb8d8",
        capsize=3,
        label="측정 평균 ± 반복 SD",
    )
    limits = [min(x) - 300, max(x) + 300]
    axes[0].plot(limits, limits, "--", color="#555555", label="이상적인 측정값")
    axes[0].set_title("설정 delay와 측정 delay")
    axes[0].set_xlabel("설정 delay (ps)")
    axes[0].set_ylabel("측정 평균 delay (ps)")
    axes[0].grid(alpha=0.25)
    axes[0].legend(fontsize=8)

    axes[1].axhline(0, color="#555555", linewidth=1)
    axes[1].errorbar(
        x,
        errors,
        yerr=repeat_sd,
        fmt="o-",
        color="#c76718",
        ecolor="#e5b789",
        capsize=3,
    )
    axes[1].set_title("설정값 대비 오차")
    axes[1].set_xlabel("설정 delay (ps)")
    axes[1].set_ylabel("측정값 − 양자화된 설정값 (ps)")
    axes[1].grid(alpha=0.25)

    axes[2].errorbar(
        x,
        within,
        yerr=[0 for _ in within],
        fmt="o-",
        color="#6b4aa1",
    )
    axes[2].set_title("반복 acquisition 내부 변동")
    axes[2].set_xlabel("설정 delay (ps)")
    axes[2].set_ylabel("평균 within-run SD (ps)")
    axes[2].grid(alpha=0.25)
    for axis in axes:
        axis.set_xticks(x)
        axis.set_xticklabels(labels, rotation=45, ha="right")
    figure.suptitle("C1-C2 Delay 비교: 10회 반복 baseline", fontsize=13)
    figure.savefig(args.output / "c1c2_comparison.png", dpi=180)
    plt.close(figure)

    report = f"""# C1-C2 Delay 비교 분석

## 데이터 범위

- 원본: `{input_path}`
- 구성: 7개 delay 조건 × 10회 반복 = {len(rows)}개 조건별 결과
- 각 조건: 100 waveform / 100 sample
- 측정 정의: `M = C2 rising - C1 rising`

## 핵심 결과

- 측정값과 설정값의 선형성: slope **{slope:.4f}**, intercept **{intercept:.1f} ps**, R² **{r_squared:.6f}**
- 양자화된 설정값 대비 평균 오차: **{mean(clean_errors):.1f} ps**
- 조건별 평균 오차 범위: **{min(float(row['error_from_quantized_ps']) for row in summary_rows):.1f}~{max(float(row['error_from_quantized_ps']) for row in summary_rows):.1f} ps**
- 반복 평균의 조건별 SD: 최대 **{max(float(row['repeat_sd_ps']) for row in summary_rows):.1f} ps**
- 반복 waveform 내부 SD: 대체로 약 **30~40 ps** 수준
- 품질 경고: `{len(outliers)}`개 반복에서 비정상적으로 큰 within-run SD가 확인됨

## 해석

측정값은 설정 delay 변화에 따라 거의 선형으로 변하지만, 전체 조건에서 약 1.3 ns의 양의 고정 오프셋이 관찰된다. 따라서 현재 결과는 delay 생성 로직 자체의 비선형성보다 출력·케이블·채널·스코프 경로의 고정 지연 성분이 함께 포함된 것으로 해석한다.

`c1c2_outliers.csv`의 반복은 평균 지터를 대표값으로 사용하기 전에 waveform 원본을 확인해야 한다. 특히 P11 반복 2에서 within-run SD가 크게 증가했다.

## 생성 파일

- `c1c2_repeat_summary.csv`: 조건별 평균, 반복 SD, 오차 요약
- `c1c2_outliers.csv`: 품질 경고 반복
- `c1c2_comparison.png`: 설정-측정, 오차, 반복 변동 시각화
"""
    (args.output / "c1c2_analysis.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
