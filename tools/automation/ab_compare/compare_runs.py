"""Compare direct (A) and swapped (B) C1-C2 delay measurements."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _load_delay_samples(path: Path) -> list[float]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        values = [
            float(row["delay_s"]) * 1e12
            for row in csv.DictReader(stream)
        ]
    if not values:
        raise ValueError(f"원시 지연 샘플이 없습니다: {path}")
    return values


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="A/B 단자 교환 측정에서 DUT 지연과 측정 경로 지연을 분리합니다."
    )
    parser.add_argument(
        "--a",
        type=Path,
        nargs="+",
        required=True,
        help="교환 전 A 측정 폴더. A1/A2처럼 여러 폴더를 지정할 수 있습니다.",
    )
    parser.add_argument(
        "--b",
        type=Path,
        nargs="+",
        required=True,
        help="교환 후 B 측정 폴더.",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _load_rows(run_dir: Path) -> dict[tuple[str, int], dict[str, Any]]:
    run_dir = run_dir.expanduser().resolve()
    summary_path = run_dir / "summary.csv"
    status_path = run_dir / "run_status.json"
    if not summary_path.is_file():
        raise FileNotFoundError(f"summary.csv가 없습니다: {run_dir}")
    if status_path.is_file():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("status") != "completed":
            raise ValueError(f"완료되지 않은 측정입니다: {run_dir}")

    rows: dict[tuple[str, int], dict[str, Any]] = {}
    with summary_path.open("r", encoding="utf-8-sig", newline="") as stream:
        for raw in csv.DictReader(stream):
            if raw.get("quality_status") != "ok":
                raise ValueError(
                    f"quality_status가 ok가 아닙니다: {run_dir} / {raw.get('prefix')}"
                )
            key = (str(raw["set_id"]), int(raw["repeat"]))
            if key in rows:
                raise ValueError(f"중복 조건이 있습니다: {run_dir} / {key}")
            prefix = str(raw["prefix"])
            sample_path = run_dir / prefix / f"{prefix}_delay_samples.csv"
            if not sample_path.is_file():
                raise FileNotFoundError(f"원시 지연 샘플 파일이 없습니다: {sample_path}")
            samples_ps = _load_delay_samples(sample_path)
            declared_count = int(raw["sample_count"])
            if len(samples_ps) != declared_count:
                raise ValueError(
                    f"summary와 원시 sample 수가 다릅니다: {sample_path}; "
                    f"{declared_count} != {len(samples_ps)}"
                )
            rows[key] = {
                "set_id": key[0],
                "repeat": key[1],
                "actual_delay_ps": float(raw["actual_delay_ps"]),
                "measured_mean_ps": statistics.fmean(samples_ps),
                "measured_stddev_ps": (
                    statistics.stdev(samples_ps) if len(samples_ps) > 1 else 0.0
                ),
                "sample_count": len(samples_ps),
            }
    if not rows:
        raise ValueError(f"유효한 측정 행이 없습니다: {run_dir}")
    return rows


def _load_group(run_dirs: list[Path]) -> list[dict[tuple[str, int], dict[str, Any]]]:
    loaded = [_load_rows(path) for path in run_dirs]
    expected = set(loaded[0])
    for path, rows in zip(run_dirs[1:], loaded[1:]):
        if set(rows) != expected:
            missing = sorted(expected - set(rows))
            extra = sorted(set(rows) - expected)
            raise ValueError(
                f"A/B 조건 구성이 다릅니다: {path}; missing={missing}, extra={extra}"
            )
    return loaded


def _mean_row(
    runs: list[dict[tuple[str, int], dict[str, Any]]],
    key: tuple[str, int],
) -> dict[str, float]:
    selected = [run[key] for run in runs]
    actual_values = [float(row["actual_delay_ps"]) for row in selected]
    if max(actual_values) - min(actual_values) > 1e-6:
        raise ValueError(f"actual_delay_ps가 측정 사이에 다릅니다: {key}")
    means = [float(row["measured_mean_ps"]) for row in selected]
    standard_errors = [
        float(row["measured_stddev_ps"]) / math.sqrt(int(row["sample_count"]))
        for row in selected
    ]
    return {
        "actual_delay_ps": statistics.fmean(actual_values),
        "measured_mean_ps": statistics.fmean(means),
        "mean_standard_error_ps": math.sqrt(
            sum(value * value for value in standard_errors)
        ) / len(standard_errors),
    }


def _sample_stddev(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 else 0.0


def compare(
    a_dirs: list[Path],
    b_dirs: list[Path],
    output_dir: Path,
) -> Path:
    a_runs = _load_group(a_dirs)
    b_runs = _load_group(b_dirs)
    if set(a_runs[0]) != set(b_runs[0]):
        raise ValueError("A와 B의 set_id/repeat 구성이 서로 다릅니다.")

    output_dir.mkdir(parents=True, exist_ok=False)
    detail_rows: list[dict[str, Any]] = []
    for key in sorted(a_runs[0], key=lambda item: (item[1], item[0])):
        a = _mean_row(a_runs, key)
        b = _mean_row(b_runs, key)
        if abs(a["actual_delay_ps"] - b["actual_delay_ps"]) > 1e-6:
            raise ValueError(f"A와 B의 actual_delay_ps가 다릅니다: {key}")

        # A = DUT + PATH, B = -DUT + PATH
        dut_delay_ps = 0.5 * (a["measured_mean_ps"] - b["measured_mean_ps"])
        path_skew_ps = 0.5 * (a["measured_mean_ps"] + b["measured_mean_ps"])
        dut_standard_error_ps = 0.5 * math.hypot(
            a["mean_standard_error_ps"],
            b["mean_standard_error_ps"],
        )
        detail_rows.append(
            {
                "set_id": key[0],
                "repeat": key[1],
                "actual_delay_ps": a["actual_delay_ps"],
                "a_measured_ps": a["measured_mean_ps"],
                "b_measured_ps": b["measured_mean_ps"],
                "dut_delay_ps": dut_delay_ps,
                "path_skew_ps": path_skew_ps,
                "dut_error_ps": dut_delay_ps - a["actual_delay_ps"],
                "dut_standard_error_ps": dut_standard_error_ps,
            }
        )

    zero_by_repeat = {
        int(row["repeat"]): float(row["dut_delay_ps"])
        for row in detail_rows
        if row["set_id"] == "Z00"
    }
    expected_repeats = {int(row["repeat"]) for row in detail_rows}
    if set(zero_by_repeat) != expected_repeats:
        raise ValueError("모든 repeat에 Z00 기준점이 있어야 zero 보정을 계산할 수 있습니다.")
    for row in detail_rows:
        zero_offset_ps = zero_by_repeat[int(row["repeat"])]
        zero_corrected_delay_ps = float(row["dut_delay_ps"]) - zero_offset_ps
        row["repeat_zero_offset_ps"] = zero_offset_ps
        row["zero_corrected_dut_delay_ps"] = zero_corrected_delay_ps
        row["zero_corrected_error_ps"] = (
            zero_corrected_delay_ps - float(row["actual_delay_ps"])
        )

    detail_path = output_dir / "ab_deembedded_rows.csv"
    with detail_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(detail_rows[0]))
        writer.writeheader()
        writer.writerows(detail_rows)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in detail_rows:
        grouped[str(row["set_id"])].append(row)
    condition_rows: list[dict[str, Any]] = []
    for set_id, rows in sorted(grouped.items()):
        dut_values = [float(row["dut_delay_ps"]) for row in rows]
        path_values = [float(row["path_skew_ps"]) for row in rows]
        error_values = [float(row["dut_error_ps"]) for row in rows]
        corrected_error_values = [
            float(row["zero_corrected_error_ps"]) for row in rows
        ]
        condition_rows.append(
            {
                "set_id": set_id,
                "actual_delay_ps": statistics.fmean(
                    float(row["actual_delay_ps"]) for row in rows
                ),
                "repeat_count": len(rows),
                "dut_mean_ps": statistics.fmean(dut_values),
                "dut_repeat_stddev_ps": _sample_stddev(dut_values),
                "dut_error_mean_ps": statistics.fmean(error_values),
                "zero_corrected_error_mean_ps": statistics.fmean(
                    corrected_error_values
                ),
                "path_skew_mean_ps": statistics.fmean(path_values),
                "path_skew_peak_to_peak_ps": max(path_values) - min(path_values),
            }
        )

    condition_path = output_dir / "ab_deembedded_conditions.csv"
    with condition_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(condition_rows[0]))
        writer.writeheader()
        writer.writerows(condition_rows)

    zero_rows = grouped.get("Z00", [])
    condition_path_values = [
        float(row["path_skew_mean_ps"]) for row in condition_rows
    ]
    payload = {
        "formula": {
            "a": "DUT + PATH",
            "b": "-DUT + PATH",
            "dut_delay": "(A - B) / 2",
            "path_skew": "(A + B) / 2",
        },
        "a_runs": [str(path.expanduser().resolve()) for path in a_dirs],
        "b_runs": [str(path.expanduser().resolve()) for path in b_dirs],
        "row_count": len(detail_rows),
        "condition_count": len(condition_rows),
        "overall_path_skew_mean_ps": statistics.fmean(
            float(row["path_skew_ps"]) for row in detail_rows
        ),
        "condition_path_skew_peak_to_peak_ps": (
            max(condition_path_values) - min(condition_path_values)
        ),
        "zero_delay_dut_offset_mean_ps": (
            statistics.fmean(float(row["dut_delay_ps"]) for row in zero_rows)
            if zero_rows
            else None
        ),
    }
    (output_dir / "ab_comparison_summary.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return output_dir


def main() -> None:
    args = _parse_args()
    output_dir = (
        args.output.expanduser().resolve()
        if args.output
        else PROJECT_ROOT
        / "output"
        / "ab_compare"
        / "comparisons"
        / datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    result = compare(args.a, args.b, output_dir)
    print(f"A/B 비교 완료: {result}")


if __name__ == "__main__":
    main()
