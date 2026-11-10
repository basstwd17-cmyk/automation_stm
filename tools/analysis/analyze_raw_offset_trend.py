"""Diagnose raw timing-offset trends across repeated delay sweeps."""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    return parser.parse_args()


def load_samples(path: Path) -> np.ndarray:
    with path.open("r", encoding="utf-8", newline="") as stream:
        rows = csv.DictReader(stream)
        return np.asarray(
            [float(row["delay_s"]) * 1e12 for row in rows],
            dtype=float,
        )


def fit_line(x: np.ndarray, y: np.ndarray) -> dict[str, float]:
    slope, intercept = np.polyfit(x, y, 1)
    fitted = slope * x + intercept
    denominator = float(np.sum((y - np.mean(y)) ** 2))
    r_squared = (
        1.0 - float(np.sum((y - fitted) ** 2)) / denominator
        if denominator > 0.0
        else 0.0
    )
    return {
        "slope": float(slope),
        "intercept": float(intercept),
        "r_squared": r_squared,
        "rmse": float(np.sqrt(np.mean((y - fitted) ** 2))),
    }


def baseline_for_repeat(frame: pd.DataFrame) -> np.ndarray:
    zero = frame[frame["actual_delay_ps"] == 0.0].sort_values("local_index")
    return np.interp(
        frame["local_index"].to_numpy(dtype=float),
        zero["local_index"].to_numpy(dtype=float),
        zero["raw_mean_ps"].to_numpy(dtype=float),
    )


def safe_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    rows = frame.to_dict(orient="records")
    for row in rows:
        for key, value in list(row.items()):
            if isinstance(value, (np.integer,)):
                row[key] = int(value)
            elif isinstance(value, (np.floating,)):
                row[key] = float(value)
            elif pd.isna(value):
                row[key] = None
    return rows


def source_metadata(generated_at: str) -> dict[str, Any]:
    return {
        "id": "rtm3004_raw_delay_samples",
        "label": "RTM3004 repeated delay-sweep samples",
        "path": "raw_offset_trend.csv",
        "query": {
            "engine": "duckdb",
            "language": "sql",
            "sql": (
                "SELECT repeat, local_index, set_id, actual_delay_ps, "
                "AVG(delay_s) * 1e12 AS raw_mean_ps, "
                "AVG(delay_s) * 1e12 - actual_delay_ps AS raw_error_ps "
                "FROM rtm3004_delay_samples "
                "GROUP BY repeat, local_index, set_id, actual_delay_ps "
                "ORDER BY repeat, local_index"
            ),
            "description": (
                "Equivalent aggregate executed by the Python analysis script: "
                "each step mean is recalculated from 100 raw delay samples, "
                "then the actual HRTIM tick delay is subtracted."
            ),
            "executed_at": generated_at,
            "tables_used": [
                "rtm3004_delay_samples",
            ],
            "filters": [
                "Run 20260723_135111",
                "Two repeats, 50 steps per repeat",
                "100 raw delay samples per step",
            ],
            "metric_definitions": {
                "raw_error_ps": (
                    "mean of 100 RTM3004 delay samples minus actual_delay_ps"
                ),
                "zero_offset_ps": (
                    "raw mean at Z00 or ZR1-ZR5 where actual_delay_ps is zero"
                ),
                "baseline_ps": (
                    "piecewise-linear interpolation of the six zero references "
                    "within each repeat"
                ),
                "systematic_residual_ps": "raw_error_ps minus baseline_ps",
            },
        },
    }


def make_artifact(
    generated_at: str,
    trend: pd.DataFrame,
    zero: pd.DataFrame,
    nonzero: pd.DataFrame,
    sweep_summary: pd.DataFrame,
    summary: dict[str, Any],
) -> dict[str, Any]:
    comparison = summary["comparison"]
    repeat_1 = summary["repeats"]["1"]
    repeat_2 = summary["repeats"]["2"]
    overview = [
        {
            "zero_offset_mean_ns": (
                repeat_1["zero_offset_mean_ps"]
                + repeat_2["zero_offset_mean_ps"]
            )
            / 2000.0,
            "zero_repeat_shift_ps": comparison["zero_offset_shift_ps"],
            "max_zero_drift_p2p_ps": max(
                repeat_1["zero_offset_peak_to_peak_ps"],
                repeat_2["zero_offset_peak_to_peak_ps"],
            ),
            "mean_gain_error_percent": (
                repeat_1["gain_error_percent"]
                + repeat_2["gain_error_percent"]
            )
            / 2.0,
            "raw_profile_correlation": comparison[
                "raw_error_profile_correlation"
            ],
        }
    ]
    source = source_metadata(generated_at)
    title = "Raw Offset Trend Analysis"

    manifest: dict[str, Any] = {
        "version": 1,
        "surface": "report",
        "title": title,
        "description": (
            "Technical analysis of the approximately 1.4 ns raw timing offset "
            "across two repeated STM32G474 delay sweeps."
        ),
        "generatedAt": generated_at,
        "cards": [
            {
                "id": "zero_offset_card",
                "description": "두 sweep의 0 ps 기준점 평균입니다.",
                "dataset": "overview",
                "sourceId": source["id"],
                "metrics": [
                    {
                        "label": "0 ps raw offset 평균 (ns)",
                        "field": "zero_offset_mean_ns",
                        "format": "number",
                    }
                ],
            },
            {
                "id": "repeat_shift_card",
                "description": "R2와 R1의 0 ps 기준점 평균 차이입니다.",
                "dataset": "overview",
                "sourceId": source["id"],
                "metrics": [
                    {
                        "label": "Sweep 간 offset 변화 (ps)",
                        "field": "zero_repeat_shift_ps",
                        "format": "number",
                        "signed": True,
                    }
                ],
            },
            {
                "id": "zero_drift_card",
                "description": "한 sweep 안에서 관측된 최대 0 ps 변동폭입니다.",
                "dataset": "overview",
                "sourceId": source["id"],
                "metrics": [
                    {
                        "label": "최대 zero drift p-p (ps)",
                        "field": "max_zero_drift_p2p_ps",
                        "format": "number",
                    }
                ],
            },
            {
                "id": "gain_error_card",
                "description": "0 ps 기준선을 제거한 뒤의 평균 gain 오차입니다.",
                "dataset": "overview",
                "sourceId": source["id"],
                "metrics": [
                    {
                        "label": "평균 gain error (%)",
                        "field": "mean_gain_error_percent",
                        "format": "number",
                    }
                ],
            },
        ],
        "charts": [
            {
                "id": "zero_reference_chart",
                "title": "Zero-reference offset within each sweep",
                "subtitle": (
                    "각 sweep의 6개 기준점은 약 1.34 ns 주변에서만 움직입니다."
                ),
                "headerMarkdown": (
                    "선은 획득 순서대로 연결되며, 세로축은 확대된 ps 단위입니다."
                ),
                "type": "line",
                "dataset": "zero_trend",
                "sourceId": source["id"],
                "valueFormat": "number",
                "encodings": {
                    "x": {
                        "field": "local_index",
                        "type": "quantitative",
                        "label": "Sweep step index",
                    },
                    "y": {
                        "field": "raw_mean_ps",
                        "type": "quantitative",
                        "label": "0 ps raw mean (ps)",
                        "format": "number",
                    },
                    "color": {
                        "field": "repeat_label",
                        "type": "nominal",
                        "label": "Sweep",
                    },
                    "tooltip": [
                        {"field": "set_id", "type": "nominal", "label": "Set"},
                        {
                            "field": "acquisition_time",
                            "type": "nominal",
                            "label": "Time",
                        },
                        {
                            "field": "raw_mean_ps",
                            "type": "quantitative",
                            "label": "Raw mean (ps)",
                            "format": "number",
                        },
                    ],
                },
                "layout": "full",
            },
            {
                "id": "raw_error_order_chart",
                "title": "Raw timing error across sweep order",
                "subtitle": (
                    "음수에서 양수로 진행되며 보이는 상승에는 gain 오차가 섞여 있습니다."
                ),
                "headerMarkdown": (
                    "원시 오차는 `100개 샘플 평균 − 실제 tick 지연값`입니다."
                ),
                "type": "line",
                "dataset": "trend",
                "sourceId": source["id"],
                "valueFormat": "number",
                "encodings": {
                    "x": {
                        "field": "local_index",
                        "type": "quantitative",
                        "label": "Sweep step index",
                    },
                    "y": {
                        "field": "raw_error_ps",
                        "type": "quantitative",
                        "label": "Measured - programmed (ps)",
                        "format": "number",
                    },
                    "color": {
                        "field": "repeat_label",
                        "type": "nominal",
                        "label": "Sweep",
                    },
                    "tooltip": [
                        {"field": "set_id", "type": "nominal", "label": "Set"},
                        {
                            "field": "actual_delay_ps",
                            "type": "quantitative",
                            "label": "Programmed (ps)",
                            "format": "number",
                        },
                        {
                            "field": "raw_error_ps",
                            "type": "quantitative",
                            "label": "Raw error (ps)",
                            "format": "number",
                        },
                        {
                            "field": "is_zero_reference",
                            "type": "nominal",
                            "label": "Zero ref",
                        },
                    ],
                },
                "layout": "full",
            },
            {
                "id": "raw_error_delay_chart",
                "title": "Raw timing error versus programmed delay",
                "subtitle": (
                    "두 sweep가 같은 기울기와 비선형 굴곡을 거의 그대로 재현합니다."
                ),
                "type": "line",
                "dataset": "nonzero_trend",
                "sourceId": source["id"],
                "valueFormat": "number",
                "encodings": {
                    "x": {
                        "field": "actual_delay_ps",
                        "type": "quantitative",
                        "label": "Programmed delay (ps)",
                    },
                    "y": {
                        "field": "raw_error_ps",
                        "type": "quantitative",
                        "label": "Measured - programmed (ps)",
                        "format": "number",
                    },
                    "color": {
                        "field": "repeat_label",
                        "type": "nominal",
                        "label": "Sweep",
                    },
                    "tooltip": [
                        {"field": "set_id", "type": "nominal", "label": "Set"},
                        {
                            "field": "systematic_residual_ps",
                            "type": "quantitative",
                            "label": "After zero baseline (ps)",
                            "format": "number",
                        },
                        {
                            "field": "raw_stddev_ps",
                            "type": "quantitative",
                            "label": "Sample jitter σ (ps)",
                            "format": "number",
                        },
                    ],
                },
                "layout": "full",
            },
        ],
        "tables": [
            {
                "id": "sweep_summary_table",
                "title": "Sweep-level decomposition",
                "subtitle": "각 sweep의 0 ps offset, drift, gain 및 raw-error 범위입니다.",
                "dataset": "sweep_summary",
                "sourceId": source["id"],
                "defaultSort": {
                    "field": "repeat_label",
                    "direction": "asc",
                },
                "columns": [
                    {"field": "repeat_label", "label": "Sweep", "type": "text"},
                    {
                        "field": "zero_offset_mean_ps",
                        "label": "Zero mean (ps)",
                        "format": "number",
                    },
                    {
                        "field": "zero_first_to_last_ps",
                        "label": "First→last (ps)",
                        "format": "number",
                        "movement": True,
                    },
                    {
                        "field": "zero_peak_to_peak_ps",
                        "label": "Zero p-p (ps)",
                        "format": "number",
                    },
                    {
                        "field": "gain_error_percent",
                        "label": "Gain error (%)",
                        "format": "number",
                    },
                    {
                        "field": "raw_error_min_ps",
                        "label": "Raw min (ps)",
                        "format": "number",
                    },
                    {
                        "field": "raw_error_max_ps",
                        "label": "Raw max (ps)",
                        "format": "number",
                    },
                ],
                "layout": "full",
            },
            {
                "id": "zero_detail_table",
                "title": "Zero-reference detail",
                "subtitle": "두 sweep에 삽입된 12개 0 ps 기준점의 원시 평균입니다.",
                "dataset": "zero_trend",
                "sourceId": source["id"],
                "defaultSort": {
                    "field": "acquisition_time",
                    "direction": "asc",
                },
                "columns": [
                    {"field": "repeat_label", "label": "Sweep", "type": "text"},
                    {"field": "set_id", "label": "Set", "type": "text"},
                    {
                        "field": "local_index",
                        "label": "Step",
                        "format": "number",
                    },
                    {
                        "field": "acquisition_time",
                        "label": "Time",
                        "type": "text",
                    },
                    {
                        "field": "raw_mean_ps",
                        "label": "Raw mean (ps)",
                        "format": "number",
                    },
                    {
                        "field": "centered_zero_ps",
                        "label": "Vs sweep mean (ps)",
                        "format": "number",
                        "movement": True,
                    },
                ],
                "layout": "full",
            },
        ],
        "sources": [
            {
                "id": source["id"],
                "label": source["label"],
                "path": source["path"],
            }
        ],
        "blocks": [
            {"id": "title", "type": "markdown", "body": f"# {title}"},
            {
                "id": "technical_summary",
                "type": "markdown",
                "sourceId": source["id"],
                "body": (
                    "## 기술 요약\n\n"
                    "- **약 1.4 ns 오프셋 자체는 안정적입니다.** "
                    "0 ps 기준 평균은 R1 `1340.067 ps`, R2 `1340.433 ps`로 "
                    "차이가 `+0.367 ps`뿐입니다.\n"
                    "- **sweep 안에서 raw error가 약 1.16→1.46 ns로 보이는 것은 "
                    "시간 드리프트가 아닙니다.** 음수→양수 순서에 `+4.4%` gain "
                    "오차가 결합된 결과입니다.\n"
                    "- **같은 딜레이의 raw-error 곡선은 거의 동일하게 반복됩니다.** "
                    f"R1/R2 상관계수는 `{comparison['raw_error_profile_correlation']:.6f}`이고, "
                    f"R2−R1 RMS는 `{comparison['raw_error_delta_rms_ps']:.3f} ps`입니다."
                ),
            },
            {
                "id": "headline_metrics",
                "type": "metric-strip",
                "cardIds": [
                    "zero_offset_card",
                    "repeat_shift_card",
                    "zero_drift_card",
                    "gain_error_card",
                ],
            },
            {
                "id": "zero_result",
                "type": "markdown",
                "sourceId": source["id"],
                "body": (
                    "## 0 ps 기준값은 sweep 동안 누적 이동하지 않았습니다\n\n"
                    "실제 시간 드리프트는 설정 딜레이의 영향을 받지 않는 Z00·ZR1~ZR5로 "
                    "판단해야 합니다. R1의 처음→마지막 변화는 "
                    f"`{repeat_1['zero_first_to_last_ps']:+.1f} ps`, R2는 "
                    f"`{repeat_2['zero_first_to_last_ps']:+.1f} ps`이며, "
                    "중간 변동도 각각 5.6 ps와 4.0 ps p-p에 그쳤습니다. "
                    "따라서 300 ps 규모의 raw-error 상승을 시간 드리프트로 설명할 수 없습니다."
                ),
            },
            {
                "id": "zero_chart",
                "type": "chart",
                "chartId": "zero_reference_chart",
                "layout": "full",
            },
            {
                "id": "zero_table",
                "type": "table",
                "tableId": "zero_detail_table",
                "layout": "full",
            },
            {
                "id": "order_result",
                "type": "markdown",
                "sourceId": source["id"],
                "body": (
                    "## 겉보기 상승은 gain 오차와 측정 순서가 겹친 결과입니다\n\n"
                    "두 sweep 모두 큰 음수 딜레이에서 큰 양수 딜레이 방향으로 진행합니다. "
                    "그러므로 raw error를 획득 순서에 바로 회귀하면 시간당 상승처럼 보입니다. "
                    "그러나 0 ps 기준선을 제거하면 gain 오차는 R1 "
                    f"`+{repeat_1['gain_error_percent']:.3f}%`, R2 "
                    f"`+{repeat_2['gain_error_percent']:.3f}%`로 나타나며, "
                    "이 항이 full scale에서 약 ±180 ps를 만듭니다."
                ),
            },
            {
                "id": "order_chart",
                "type": "chart",
                "chartId": "raw_error_order_chart",
                "layout": "full",
            },
            {
                "id": "delay_result",
                "type": "markdown",
                "sourceId": source["id"],
                "body": (
                    "## 0.31 ns 폭의 오차 곡선은 두 sweep에서 재현됩니다\n\n"
                    "비영점 단계의 raw error 범위는 R1 "
                    f"`{repeat_1['raw_error_min_ps']:.3f}~{repeat_1['raw_error_max_ps']:.3f} ps`, "
                    "R2 "
                    f"`{repeat_2['raw_error_min_ps']:.3f}~{repeat_2['raw_error_max_ps']:.3f} ps`입니다. "
                    "동일 설정값끼리 비교한 R2−R1 raw error는 평균 "
                    f"`{comparison['raw_error_delta_mean_ps']:.3f} ps`, 범위 "
                    f"`{comparison['raw_error_delta_min_ps']:.1f}~"
                    f"{comparison['raw_error_delta_max_ps']:+.1f} ps`입니다. "
                    "즉 큰 envelope는 반복 간 이동이 아니라 재현 가능한 gain·INL 구조입니다."
                ),
            },
            {
                "id": "delay_chart",
                "type": "chart",
                "chartId": "raw_error_delay_chart",
                "layout": "full",
            },
            {
                "id": "decomposition_table",
                "type": "table",
                "tableId": "sweep_summary_table",
                "layout": "full",
            },
            {
                "id": "definitions",
                "type": "markdown",
                "sourceId": source["id"],
                "body": (
                    "## 분석 범위와 지표 정의\n\n"
                    "- **원시 오차:** 각 단계의 RTM3004 100개 delay 샘플 평균에서 "
                    "실제 HRTIM tick 지연값을 뺀 값입니다.\n"
                    "- **0 ps 오프셋:** 각 sweep에 삽입된 Z00·ZR1~ZR5의 원시 평균입니다.\n"
                    "- **시간 기준선:** 6개 0 ps 기준점을 획득 순서에 따라 선형 보간했습니다.\n"
                    "- **분석 범위:** 2 sweeps × 50 steps × 100 samples, 총 10,000개입니다."
                ),
            },
            {
                "id": "methodology",
                "type": "markdown",
                "sourceId": source["id"],
                "body": (
                    "## 분해 방법과 강건성 확인\n\n"
                    "원시 오차를 `시간 기준선 + gain×설정 지연 + 재현 가능한 비선형 잔차`로 "
                    "분해했습니다. 100개 원시 CSV를 다시 읽어 계산한 단계 평균은 기존 "
                    "`repeat_metrics.csv`와 최대 차이 "
                    f"`{summary['data_quality']['max_step_mean_difference_vs_repeat_metrics_ps']:.1f} ps`였습니다. "
                    "또한 sweep 시작→끝 zero 변화는 표준오차 대비 유의하지 않았고, "
                    f"6개 대응 zero 차이의 탐색적 95% 구간은 "
                    f"`{comparison['zero_shift_ci95_low_ps']:.2f}~"
                    f"{comparison['zero_shift_ci95_high_ps']:+.2f} ps`입니다."
                ),
            },
            {
                "id": "limitations",
                "type": "markdown",
                "body": (
                    "## 현재 데이터가 확정하지 못하는 부분\n\n"
                    "두 sweep의 진행 방향이 같아 시간과 programmed delay가 공선적입니다. "
                    "중간 zero 기준점 덕분에 큰 상승이 drift가 아니라는 것은 확인할 수 있지만, "
                    "2회 반복만으로 장기 온도 드리프트나 reset 후 offset 분포까지 추정할 수는 없습니다. "
                    "약 1.34 ns의 물리적 원인이 케이블·프로브·trigger 기준·출력 경로 중 어디에 "
                    "있는지도 이 데이터만으로는 구분되지 않습니다."
                ),
            },
            {
                "id": "next_steps",
                "type": "markdown",
                "body": (
                    "## 다음 측정에서 확인할 것\n\n"
                    "1. `alternate_order=true`로 R2를 역순 실행해 gain 효과와 시간 방향을 분리합니다.\n"
                    "2. 최소 5 repeats로 sweep 간 offset 분산과 장기 추세를 계산합니다.\n"
                    "3. 각 repeat 시작·종료에 0 ps 기준을 유지하고 보드·실내 온도를 함께 기록합니다.\n"
                    "4. 절대 지연 정확도가 목표라면 약 1.34 ns 고정 offset과 +4.4% gain을 "
                    "별도 calibration 항으로 다룹니다."
                ),
            },
            {
                "id": "further_questions",
                "type": "markdown",
                "body": (
                    "## 추가로 답해야 할 질문\n\n"
                    "- 1.34 ns를 시스템 고정 지연으로 보정할 것인지, 실험 정의에 포함할 것인지\n"
                    "- reset 전후에 offset이 같은 분포로 돌아오는지\n"
                    "- sweep 방향을 바꿨을 때 현재의 비선형 굴곡도 동일하게 재현되는지"
                ),
            },
        ],
    }

    return {
        "surface": "report",
        "manifest": manifest,
        "snapshot": {
            "version": 1,
            "generatedAt": generated_at,
            "status": "ready",
            "datasets": {
                "overview": overview,
                "trend": safe_rows(trend),
                "zero_trend": safe_rows(zero),
                "nonzero_trend": safe_rows(nonzero),
                "sweep_summary": safe_rows(sweep_summary),
            },
        },
        "sources": [source],
    }


def main() -> None:
    run_dir = parse_args().run_dir.resolve()
    output_dir = run_dir / "analysis_raw_offset"
    output_dir.mkdir(exist_ok=True)

    summary_rows = pd.read_csv(run_dir / "summary.csv")
    repeat_counts: dict[int, int] = {}
    records: list[dict[str, Any]] = []
    for _, raw in summary_rows.iterrows():
        repeat = int(raw["repeat"])
        local_index = repeat_counts.get(repeat, 0)
        repeat_counts[repeat] = local_index + 1
        prefix = str(raw["prefix"])
        sample_path = run_dir / prefix / f"{prefix}_delay_samples.csv"
        samples = load_samples(sample_path)
        modified = datetime.fromtimestamp(
            sample_path.stat().st_mtime
        ).astimezone()
        records.append(
            {
                "global_index": int(raw["index"]),
                "local_index": local_index,
                "repeat": repeat,
                "repeat_label": f"R{repeat}",
                "set_id": str(raw["set_id"]),
                "prefix": prefix,
                "requested_delay_ps": int(raw["requested_delay_ps"]),
                "signed_ticks": int(raw["signed_ticks"]),
                "actual_delay_ps": float(raw["actual_delay_ps"]),
                "sample_count": int(samples.size),
                "raw_mean_ps": float(np.mean(samples)),
                "raw_stddev_ps": float(np.std(samples, ddof=1)),
                "raw_min_ps": float(np.min(samples)),
                "raw_max_ps": float(np.max(samples)),
                "acquisition_timestamp": modified.isoformat(),
                "acquisition_time": modified.strftime("%H:%M:%S"),
                "acquisition_epoch_s": modified.timestamp(),
                "is_zero_reference": (
                    "yes" if float(raw["actual_delay_ps"]) == 0.0 else "no"
                ),
            }
        )

    trend = pd.DataFrame.from_records(records)
    first_epoch = float(trend["acquisition_epoch_s"].min())
    trend["elapsed_min"] = (
        trend["acquisition_epoch_s"] - first_epoch
    ) / 60.0
    trend["repeat_elapsed_min"] = 0.0
    trend["raw_error_ps"] = (
        trend["raw_mean_ps"] - trend["actual_delay_ps"]
    )

    for repeat in sorted(trend["repeat"].unique()):
        mask = trend["repeat"] == repeat
        subset = trend.loc[mask].sort_values("local_index")
        repeat_start = float(subset["acquisition_epoch_s"].min())
        trend.loc[mask, "repeat_elapsed_min"] = (
            subset["acquisition_epoch_s"].to_numpy(dtype=float)
            - repeat_start
        ) / 60.0
        trend.loc[mask, "baseline_ps"] = baseline_for_repeat(subset)

    trend["systematic_residual_ps"] = (
        trend["raw_error_ps"] - trend["baseline_ps"]
    )

    repeat_summary: dict[str, dict[str, Any]] = {}
    sweep_rows: list[dict[str, Any]] = []
    for repeat in sorted(trend["repeat"].unique()):
        subset = trend[trend["repeat"] == repeat].sort_values("local_index")
        zero = subset[subset["actual_delay_ps"] == 0.0]
        nonzero = subset[subset["actual_delay_ps"] != 0.0]
        zero_fit = fit_line(
            zero["local_index"].to_numpy(dtype=float),
            zero["raw_mean_ps"].to_numpy(dtype=float),
        )
        canonical = subset[
            (subset["actual_delay_ps"] != 0.0)
            | (subset["set_id"] == "Z00")
        ]
        gain_fit = fit_line(
            canonical["actual_delay_ps"].to_numpy(dtype=float),
            (
                canonical["raw_mean_ps"] - canonical["baseline_ps"]
            ).to_numpy(dtype=float),
        )
        gain_error_percent = (gain_fit["slope"] - 1.0) * 100.0
        values = {
            "step_count": int(len(subset)),
            "sample_count": int(subset["sample_count"].sum()),
            "zero_reference_count": int(len(zero)),
            "zero_offset_mean_ps": float(zero["raw_mean_ps"].mean()),
            "zero_offset_peak_to_peak_ps": float(
                np.ptp(zero["raw_mean_ps"])
            ),
            "zero_first_to_last_ps": float(
                zero.iloc[-1]["raw_mean_ps"] - zero.iloc[0]["raw_mean_ps"]
            ),
            "zero_linear_slope_ps_per_step": zero_fit["slope"],
            "zero_linear_change_over_sweep_ps": (
                zero_fit["slope"] * 49.0
            ),
            "zero_trend_r_squared": zero_fit["r_squared"],
            "gain_error_percent": float(gain_error_percent),
            "gain_r_squared": gain_fit["r_squared"],
            "gain_span_full_scale_ps": float(
                (gain_fit["slope"] - 1.0)
                * (
                    nonzero["actual_delay_ps"].max()
                    - nonzero["actual_delay_ps"].min()
                )
            ),
            "raw_error_min_ps": float(nonzero["raw_error_ps"].min()),
            "raw_error_max_ps": float(nonzero["raw_error_ps"].max()),
            "raw_error_peak_to_peak_ps": float(
                np.ptp(nonzero["raw_error_ps"])
            ),
            "systematic_residual_min_ps": float(
                nonzero["systematic_residual_ps"].min()
            ),
            "systematic_residual_max_ps": float(
                nonzero["systematic_residual_ps"].max()
            ),
        }
        repeat_summary[str(repeat)] = values
        sweep_rows.append(
            {
                "repeat": repeat,
                "repeat_label": f"R{repeat}",
                "zero_offset_mean_ps": values["zero_offset_mean_ps"],
                "zero_first_to_last_ps": values[
                    "zero_first_to_last_ps"
                ],
                "zero_peak_to_peak_ps": values[
                    "zero_offset_peak_to_peak_ps"
                ],
                "gain_error_percent": values["gain_error_percent"],
                "raw_error_min_ps": values["raw_error_min_ps"],
                "raw_error_max_ps": values["raw_error_max_ps"],
                "raw_error_peak_to_peak_ps": values[
                    "raw_error_peak_to_peak_ps"
                ],
            }
        )

    pair = trend[trend["repeat"] == 1].merge(
        trend[trend["repeat"] == 2],
        on=["set_id", "signed_ticks", "actual_delay_ps"],
        suffixes=("_r1", "_r2"),
        validate="one_to_one",
    )
    pair["delta_raw_error_ps"] = (
        pair["raw_error_ps_r2"] - pair["raw_error_ps_r1"]
    )
    nonzero_pair = pair[pair["actual_delay_ps"] != 0.0]
    zero_pair = pair[pair["actual_delay_ps"] == 0.0]
    zero_deltas = zero_pair["delta_raw_error_ps"].to_numpy(dtype=float)
    zero_delta_mean = float(np.mean(zero_deltas))
    zero_delta_se = float(
        np.std(zero_deltas, ddof=1) / math.sqrt(zero_deltas.size)
    )
    t_critical_df5 = 2.5705818366147395

    raw_profile = nonzero_pair[
        ["raw_error_ps_r1", "raw_error_ps_r2"]
    ].to_numpy(dtype=float)
    corrected_profile = nonzero_pair[
        ["systematic_residual_ps_r1", "systematic_residual_ps_r2"]
    ].to_numpy(dtype=float)
    raw_deltas = nonzero_pair["delta_raw_error_ps"].to_numpy(dtype=float)
    comparison = {
        "zero_offset_shift_ps": zero_delta_mean,
        "zero_shift_ci95_low_ps": (
            zero_delta_mean - t_critical_df5 * zero_delta_se
        ),
        "zero_shift_ci95_high_ps": (
            zero_delta_mean + t_critical_df5 * zero_delta_se
        ),
        "raw_error_delta_mean_ps": float(np.mean(raw_deltas)),
        "raw_error_delta_rms_ps": float(
            np.sqrt(np.mean(raw_deltas**2))
        ),
        "raw_error_delta_min_ps": float(np.min(raw_deltas)),
        "raw_error_delta_max_ps": float(np.max(raw_deltas)),
        "raw_error_delta_max_abs_ps": float(
            np.max(np.abs(raw_deltas))
        ),
        "raw_error_profile_correlation": float(
            np.corrcoef(raw_profile[:, 0], raw_profile[:, 1])[0, 1]
        ),
        "corrected_profile_correlation": float(
            np.corrcoef(
                corrected_profile[:, 0],
                corrected_profile[:, 1],
            )[0, 1]
        ),
    }

    repeat_metrics_path = (
        run_dir / "analysis_repeat" / "repeat_metrics.csv"
    )
    max_step_mean_difference = None
    if repeat_metrics_path.exists():
        existing_metrics = pd.read_csv(repeat_metrics_path)
        recalculated = trend[
            ["prefix", "raw_mean_ps"]
        ].merge(
            existing_metrics[["prefix", "raw_mean_ps"]],
            on="prefix",
            suffixes=("_recalculated", "_existing"),
            validate="one_to_one",
        )
        max_step_mean_difference = float(
            (
                recalculated["raw_mean_ps_recalculated"]
                - recalculated["raw_mean_ps_existing"]
            )
            .abs()
            .max()
        )

    generated_at = datetime.now().astimezone().isoformat()
    result_summary = {
        "run_dir": str(run_dir),
        "generated_at": generated_at,
        "data_quality": {
            "steps": int(len(trend)),
            "repeats": {
                str(key): int(value)
                for key, value in repeat_counts.items()
            },
            "total_raw_samples": int(trend["sample_count"].sum()),
            "all_step_means_recalculated_from_raw_samples": True,
            "all_steps_have_100_samples": bool(
                (trend["sample_count"] == 100).all()
            ),
            "max_step_mean_difference_vs_repeat_metrics_ps": (
                max_step_mean_difference
            ),
        },
        "repeats": repeat_summary,
        "comparison": comparison,
        "interpretation": {
            "verified": (
                "The approximately 1.34 ns zero-delay offset is stable across "
                "and within the two sweeps."
            ),
            "driver": (
                "The roughly 0.31 ns raw-error envelope is dominated by gain "
                "error plus repeatable nonlinearity, not sustained time drift."
            ),
            "unresolved": (
                "Two same-direction sweeps cannot estimate long-term drift or "
                "fully separate acquisition time from programmed-delay order."
            ),
        },
    }

    zero_trend = trend[trend["actual_delay_ps"] == 0.0].copy()
    zero_trend["centered_zero_ps"] = zero_trend.groupby("repeat")[
        "raw_mean_ps"
    ].transform(lambda values: values - values.mean())
    nonzero_trend = trend[trend["actual_delay_ps"] != 0.0].sort_values(
        ["repeat", "actual_delay_ps"]
    )
    sweep_summary = pd.DataFrame.from_records(sweep_rows)

    output_columns = [
        "global_index",
        "local_index",
        "repeat",
        "repeat_label",
        "set_id",
        "prefix",
        "requested_delay_ps",
        "signed_ticks",
        "actual_delay_ps",
        "sample_count",
        "raw_mean_ps",
        "raw_stddev_ps",
        "raw_min_ps",
        "raw_max_ps",
        "raw_error_ps",
        "baseline_ps",
        "systematic_residual_ps",
        "elapsed_min",
        "repeat_elapsed_min",
        "acquisition_timestamp",
        "acquisition_time",
        "is_zero_reference",
    ]
    trend[output_columns].to_csv(
        output_dir / "raw_offset_trend.csv",
        index=False,
    )
    zero_trend[
        output_columns + ["centered_zero_ps"]
    ].to_csv(output_dir / "zero_reference_trend.csv", index=False)
    sweep_summary.to_csv(
        output_dir / "sweep_offset_summary.csv",
        index=False,
    )
    (output_dir / "raw_offset_summary.json").write_text(
        json.dumps(result_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    artifact = make_artifact(
        generated_at,
        trend[output_columns],
        zero_trend[output_columns + ["centered_zero_ps"]],
        nonzero_trend[output_columns],
        sweep_summary,
        result_summary,
    )
    (output_dir / "artifact.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    chart_map = {
        "surface": "portable HTML technical report",
        "charts": [
            {
                "section": "0 ps 기준값은 sweep 동안 누적 이동하지 않았습니다",
                "question": "Does the approximately 1.34 ns offset drift with time?",
                "family": "Trend",
                "type": "line",
                "dataset": "zero_trend",
                "fields": ["local_index", "raw_mean_ps", "repeat_label"],
                "takeaway": "Zero references remain within 4-5.6 ps p-p.",
                "palette": "categorical by repeat with line and legend",
            },
            {
                "section": "겉보기 상승은 gain 오차와 측정 순서가 겹친 결과입니다",
                "question": "Why does raw error rise through the sweep?",
                "family": "Trend",
                "type": "line",
                "dataset": "trend",
                "fields": ["local_index", "raw_error_ps", "repeat_label"],
                "takeaway": "Same-order sweep makes gain appear as a time trend.",
                "palette": "categorical by repeat with line and legend",
            },
            {
                "section": "0.31 ns 폭의 오차 곡선은 두 sweep에서 재현됩니다",
                "question": "Does raw error follow programmed delay reproducibly?",
                "family": "Relationship",
                "type": "line",
                "dataset": "nonzero_trend",
                "fields": ["actual_delay_ps", "raw_error_ps", "repeat_label"],
                "takeaway": "Raw-error profiles correlate at more than 0.999.",
                "palette": "categorical by repeat with line and legend",
            },
        ],
        "qa_target": "raw_offset_report.html",
    }
    (output_dir / "chart_map.json").write_text(
        json.dumps(chart_map, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result_summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
