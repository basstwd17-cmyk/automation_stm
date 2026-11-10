"""Consolidate and visualize A, B, A2, and A_c1c2change delay runs."""

from __future__ import annotations

import csv
import html
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUN_ROOT = PROJECT_ROOT / "output" / "delay_sweep_automation"
OUTPUT_DIR = PROJECT_ROOT / "output" / "c1_c2_comparison" / "A_B_A2_Ac1c2change"

RUNS = {
    "A": {
        "folder": "20260727_095646_A",
        "orientation": 1,
        "description": "기본 C1-C2 연결",
    },
    "B": {
        "folder": "20260727_102320_B",
        "orientation": -1,
        "description": "A/B 교환 연결",
    },
    "A2": {
        "folder": "20260727_104609_A2",
        "orientation": 1,
        "description": "기본 연결 재측정",
    },
    "Ac1c2change": {
        "folder": "20260727_140945_A_c1c2change",
        "orientation": -1,
        "description": "A 상태에서 C1-C2 교환",
    },
}

RUN_ORDER = ["A", "B", "A2", "Ac1c2change"]
CONDITION_ORDER = ["N22", "N11", "N01", "Z00", "P01", "P11", "P22"]
NONZERO_CONDITIONS = ["N22", "N11", "N01", "P01", "P11", "P22"]

COLORS = {
    "ink": "#18212F",
    "muted": "#5F6B7A",
    "grid": "#DCE2E8",
    "axis": "#8592A3",
    "negative": "#2563EB",
    "positive": "#D97706",
    "ideal": "#3F4752",
    "A": "#1D4ED8",
    "B": "#D97706",
    "A2": "#7C3AED",
    "Ac1c2change": "#0F766E",
    "background": "#FFFFFF",
}

MARKERS = {
    "A": "circle",
    "B": "square",
    "A2": "diamond",
    "Ac1c2change": "triangle",
}

DASHES = {
    "A": "",
    "B": "10 6",
    "A2": "3 5",
    "Ac1c2change": "14 5 3 5",
}


def load_samples(path: Path) -> list[float]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        values = [
            float(row["delay_s"]) * 1e12
            for row in csv.DictReader(stream)
        ]
    if not values:
        raise ValueError(f"No delay samples: {path}")
    return values


def load_run(label: str, config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    run_dir = RUN_ROOT / str(config["folder"])
    status = json.loads((run_dir / "run_status.json").read_text(encoding="utf-8"))
    if status.get("status") != "completed":
        raise ValueError(f"{label} is not completed: {status.get('status')}")

    summary_path = run_dir / "summary.csv"
    summary_rows: list[dict[str, Any]] = []
    with summary_path.open("r", encoding="utf-8-sig", newline="") as stream:
        for raw in csv.DictReader(stream):
            if raw["quality_status"] != "ok":
                raise ValueError(f"{label} has non-ok row: {raw['prefix']}")
            prefix = raw["prefix"]
            sample_path = run_dir / prefix / f"{prefix}_delay_samples.csv"
            samples = load_samples(sample_path)
            if len(samples) != int(raw["sample_count"]):
                raise ValueError(f"Sample count mismatch: {sample_path}")
            summary_rows.append(
                {
                    "run": label,
                    "folder": str(config["folder"]),
                    "orientation": int(config["orientation"]),
                    "set_id": raw["set_id"],
                    "repeat": int(raw["repeat"]),
                    "actual_delay_ps": float(raw["actual_delay_ps"]),
                    "raw_mean_ps": statistics.fmean(samples),
                    "raw_stddev_ps": statistics.stdev(samples),
                    "sample_count": len(samples),
                }
            )

    if len(summary_rows) != 70:
        raise ValueError(f"{label} expected 70 rows, found {len(summary_rows)}")

    zeros = {
        row["repeat"]: row["raw_mean_ps"]
        for row in summary_rows
        if row["set_id"] == "Z00"
    }
    if len(zeros) != 10:
        raise ValueError(f"{label} expected 10 Z00 rows, found {len(zeros)}")

    for row in summary_rows:
        relative = (
            row["orientation"]
            * (row["raw_mean_ps"] - zeros[row["repeat"]])
        )
        row["zero_relative_measured_ps"] = relative
        row["error_ps"] = relative - row["actual_delay_ps"]

    inventory = {
        "run": label,
        "folder": str(config["folder"]),
        "description": str(config["description"]),
        "orientation_multiplier": int(config["orientation"]),
        "status": status["status"],
        "condition_rows": len(summary_rows),
        "quality_ok_rows": len(summary_rows),
        "raw_samples": sum(row["sample_count"] for row in summary_rows),
        "raw_z00_mean_ps": statistics.fmean(zeros.values()),
    }
    return summary_rows, inventory


def aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["set_id"]].append(row)

    result: list[dict[str, Any]] = []
    for set_id in CONDITION_ORDER:
        condition_rows = grouped[set_id]
        actual_values = {row["actual_delay_ps"] for row in condition_rows}
        if len(actual_values) != 1:
            raise ValueError(f"Inconsistent actual delay for {set_id}")

        item: dict[str, Any] = {
            "set_id": set_id,
            "actual_delay_ps": actual_values.pop(),
        }
        run_means: list[float] = []
        all_errors: list[float] = []
        for run in RUN_ORDER:
            selected = [row for row in condition_rows if row["run"] == run]
            measured = [row["zero_relative_measured_ps"] for row in selected]
            errors = [row["error_ps"] for row in selected]
            item[f"{run}_measured_ps"] = statistics.fmean(measured)
            item[f"{run}_error_ps"] = statistics.fmean(errors)
            item[f"{run}_repeat_pp_ps"] = max(measured) - min(measured)
            run_means.append(item[f"{run}_measured_ps"])
            all_errors.extend(errors)

        item["four_run_mean_measured_ps"] = statistics.fmean(run_means)
        item["four_run_mean_error_ps"] = (
            item["four_run_mean_measured_ps"] - item["actual_delay_ps"]
        )
        item["four_run_mean_range_ps"] = max(run_means) - min(run_means)
        item["all_repeat_error_min_ps"] = min(all_errors)
        item["all_repeat_error_max_ps"] = max(all_errors)
        item["all_repeat_error_pp_ps"] = max(all_errors) - min(all_errors)
        result.append(item)
    return result


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def text(
    x: float,
    y: float,
    value: str,
    size: int = 24,
    anchor: str = "start",
    fill: str = COLORS["ink"],
    weight: int = 400,
) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" '
        f'font-family="Segoe UI, Arial, sans-serif" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill}">{html.escape(value)}</text>'
    )


def marker(x: float, y: float, kind: str, color: str) -> str:
    if kind == "square":
        return (
            f'<rect x="{x - 6:.1f}" y="{y - 6:.1f}" width="12" height="12" '
            f'fill="{COLORS["background"]}" stroke="{color}" stroke-width="3"/>'
        )
    if kind == "diamond":
        points = f"{x:.1f},{y - 8:.1f} {x + 8:.1f},{y:.1f} {x:.1f},{y + 8:.1f} {x - 8:.1f},{y:.1f}"
        return (
            f'<polygon points="{points}" fill="{COLORS["background"]}" '
            f'stroke="{color}" stroke-width="3"/>'
        )
    if kind == "triangle":
        points = f"{x:.1f},{y - 8:.1f} {x + 8:.1f},{y + 7:.1f} {x - 8:.1f},{y + 7:.1f}"
        return (
            f'<polygon points="{points}" fill="{COLORS["background"]}" '
            f'stroke="{color}" stroke-width="3"/>'
        )
    return (
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" '
        f'fill="{COLORS["background"]}" stroke="{color}" stroke-width="3"/>'
    )


def render_error_chart(summary: list[dict[str, Any]], path: Path) -> None:
    width, height = 1500, 900
    left, right, top, bottom = 300, 1350, 165, 790
    values = [
        row["four_run_mean_error_ps"]
        for row in summary
        if row["set_id"] != "Z00"
    ]
    low = 50 * math.floor((min(values) - 25) / 50)
    high = 50 * math.ceil((max(values) + 25) / 50)

    def xmap(value: float) -> float:
        return left + (value - low) * (right - left) / (high - low)

    rows = [row for row in summary if row["set_id"] != "Z00"]
    step = (bottom - top) / len(rows)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect width="{width}" height="{height}" fill="{COLORS["background"]}"/>',
        text(70, 65, "설정값 대비 측정 지연 오차", 38, weight=700),
        text(
            70,
            110,
            "A/B/A2/Ac1c2change 평균 · 막대=평균 오차 · 선=네 측정 평균의 최소–최대",
            22,
            fill=COLORS["muted"],
        ),
    ]
    for tick in range(int(low), int(high) + 1, 50):
        x = xmap(tick)
        parts.append(
            f'<line x1="{x:.1f}" y1="{top - 25}" x2="{x:.1f}" y2="{bottom}" '
            f'stroke="{COLORS["grid"]}" stroke-width="1"/>'
        )
        parts.append(text(x, bottom + 42, f"{tick:+d}", 20, "middle", COLORS["muted"]))
    zero_x = xmap(0)
    parts.append(
        f'<line x1="{zero_x:.1f}" y1="{top - 30}" x2="{zero_x:.1f}" y2="{bottom}" '
        f'stroke="{COLORS["axis"]}" stroke-width="3"/>'
    )

    for index, row in enumerate(rows):
        y = top + step * (index + 0.5)
        error = row["four_run_mean_error_ps"]
        run_errors = [row[f"{run}_error_ps"] for run in RUN_ORDER]
        color = COLORS["negative"] if error < 0 else COLORS["positive"]
        x0, x1 = sorted((zero_x, xmap(error)))
        parts.append(text(left - 28, y + 8, row["set_id"], 26, "end", weight=700))
        parts.append(
            f'<rect x="{x0:.1f}" y="{y - 24:.1f}" width="{max(2, x1 - x0):.1f}" '
            f'height="48" rx="5" fill="{color}" fill-opacity="0.82"/>'
        )
        range_min, range_max = xmap(min(run_errors)), xmap(max(run_errors))
        parts.extend(
            [
                f'<line x1="{range_min:.1f}" y1="{y:.1f}" x2="{range_max:.1f}" y2="{y:.1f}" '
                f'stroke="{COLORS["ink"]}" stroke-width="4"/>',
                f'<line x1="{range_min:.1f}" y1="{y - 11:.1f}" x2="{range_min:.1f}" y2="{y + 11:.1f}" '
                f'stroke="{COLORS["ink"]}" stroke-width="3"/>',
                f'<line x1="{range_max:.1f}" y1="{y - 11:.1f}" x2="{range_max:.1f}" y2="{y + 11:.1f}" '
                f'stroke="{COLORS["ink"]}" stroke-width="3"/>',
            ]
        )
        if error < -60:
            label_x = xmap(error) + 18
            anchor = "start"
            label_color = COLORS["background"]
        else:
            label_x = xmap(error) + (16 if error >= 0 else -16)
            anchor = "start" if error >= 0 else "end"
            label_color = COLORS["ink"]
        parts.append(
            text(label_x, y + 8, f"{error:+.2f} ps", 24, anchor, label_color, 700)
        )
    parts.append(text((left + right) / 2, height - 42, "측정값 − 설정값 (ps)", 24, "middle", weight=600))
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def render_calibration_chart(summary: list[dict[str, Any]], path: Path) -> None:
    width, height = 1500, 980
    left, right, top, bottom = 170, 1360, 180, 825
    limit = 4500.0

    def xmap(value: float) -> float:
        return left + (value + limit) * (right - left) / (2 * limit)

    def ymap(value: float) -> float:
        return bottom - (value + limit) * (bottom - top) / (2 * limit)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect width="{width}" height="{height}" fill="{COLORS["background"]}"/>',
        text(70, 65, "설정 지연과 측정 지연 비교", 38, weight=700),
        text(
            70,
            110,
            "각 측정은 자체 Z00을 제거하고 C1-C2 방향을 통일함 · 점선은 이상적인 측정값=설정값",
            22,
            fill=COLORS["muted"],
        ),
    ]
    for tick in [-4000, -2000, 0, 2000, 4000]:
        x, y = xmap(tick), ymap(tick)
        parts.extend(
            [
                f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{bottom}" stroke="{COLORS["grid"]}" stroke-width="1"/>',
                f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="{COLORS["grid"]}" stroke-width="1"/>',
                text(x, bottom + 38, f"{tick}", 20, "middle", COLORS["muted"]),
                text(left - 22, y + 7, f"{tick}", 20, "end", COLORS["muted"]),
            ]
        )
    parts.extend(
        [
            f'<line x1="{xmap(-limit):.1f}" y1="{ymap(-limit):.1f}" x2="{xmap(limit):.1f}" y2="{ymap(limit):.1f}" '
            f'stroke="{COLORS["ideal"]}" stroke-width="3" stroke-dasharray="8 7"/>',
            text((left + right) / 2, height - 52, "설정 지연 (ps)", 24, "middle", weight=600),
            f'<text x="42" y="{(top + bottom) / 2:.1f}" transform="rotate(-90 42 {(top + bottom) / 2:.1f})" '
            f'text-anchor="middle" font-family="Segoe UI, Arial, sans-serif" font-size="24" '
            f'font-weight="600" fill="{COLORS["ink"]}">측정 지연 (ps)</text>',
        ]
    )

    legend_x = 760
    for index, run in enumerate(RUN_ORDER):
        lx = legend_x + index * 165
        color = COLORS[run]
        parts.append(
            f'<line x1="{lx}" y1="142" x2="{lx + 44}" y2="142" stroke="{color}" '
            f'stroke-width="3" stroke-dasharray="{DASHES[run]}"/>'
        )
        parts.append(marker(lx + 22, 142, MARKERS[run], color))
        parts.append(text(lx + 54, 149, run, 20, fill=color, weight=600))

    for run in RUN_ORDER:
        points = [
            (xmap(row["actual_delay_ps"]), ymap(row[f"{run}_measured_ps"]))
            for row in summary
        ]
        point_text = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        dash = (
            f' stroke-dasharray="{DASHES[run]}"'
            if DASHES[run]
            else ""
        )
        parts.append(
            f'<polyline points="{point_text}" fill="none" stroke="{COLORS[run]}" '
            f'stroke-width="3"{dash}/>'
        )
        for x, y in points:
            parts.append(marker(x, y, MARKERS[run], COLORS[run]))
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def write_report(
    inventory: list[dict[str, Any]],
    summary: list[dict[str, Any]],
    path: Path,
) -> None:
    nonzero = [row for row in summary if row["set_id"] != "Z00"]
    worst_error = max(nonzero, key=lambda row: abs(row["four_run_mean_error_ps"]))
    widest = max(nonzero, key=lambda row: row["four_run_mean_range_ps"])

    lines = [
        "# C1-C2 Four-Run Comparison",
        "",
        "A, B, A2, Ac1c2change 네 측정을 같은 방향으로 정규화해 비교했다.",
        "각 측정의 같은 repeat에 있는 Z00을 먼저 빼고, 교환 측정은 부호를 반전했다.",
        "",
        "## 데이터 품질",
        "",
        "| 측정 | 상태 | 조건 행 | 원시 샘플 | 원시 Z00 평균 (ps) |",
        "|---|---|---:|---:|---:|",
    ]
    for item in inventory:
        lines.append(
            f"| {item['run']} | {item['status']} | {item['condition_rows']} | "
            f"{item['raw_samples']} | {item['raw_z00_mean_ps']:.2f} |"
        )

    lines.extend(
        [
            "",
            "## 설정값 대비 측정값",
            "",
            "| 조건 | 설정값 (ps) | 네 측정 평균 (ps) | 차이 (ps) | 네 측정 평균 범위 (ps) |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in nonzero:
        lines.append(
            f"| {row['set_id']} | {row['actual_delay_ps']:.2f} | "
            f"{row['four_run_mean_measured_ps']:.2f} | "
            f"**{row['four_run_mean_error_ps']:+.2f}** | "
            f"{row['four_run_mean_range_ps']:.2f} |"
        )

    lines.extend(
        [
            "",
            "## 결론",
            "",
            f"- 가장 큰 설정 오차는 {worst_error['set_id']}의 "
            f"{worst_error['four_run_mean_error_ps']:+.2f} ps다.",
            f"- 네 측정 사이의 최대 평균 범위는 {widest['set_id']}의 "
            f"{widest['four_run_mean_range_ps']:.2f} ps다.",
            "- 네 연결 방식에서 거의 같은 결과가 나왔으므로 연결 교환보다 "
            "delay 크기와 부호에 따른 체계적 오차가 더 크다.",
            "",
            "## 생성 파일",
            "",
            "- `run_inventory.csv`: 네 원본 측정의 상태와 샘플 수",
            "- `four_run_repeat_details.csv`: 280개 조건-repeat 상세값",
            "- `four_run_condition_summary.csv`: 조건별 네 측정 평균과 오차",
            "- `delay_error_comparison.png`: 설정값 대비 오차",
            "- `configured_vs_measured.png`: 설정값과 측정값의 전체 관계",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    for label in RUN_ORDER:
        rows, item = load_run(label, RUNS[label])
        all_rows.extend(rows)
        inventory.append(item)

    summary = aggregate(all_rows)
    write_csv(OUTPUT_DIR / "run_inventory.csv", inventory)
    write_csv(OUTPUT_DIR / "four_run_repeat_details.csv", all_rows)
    write_csv(OUTPUT_DIR / "four_run_condition_summary.csv", summary)
    render_error_chart(summary, OUTPUT_DIR / "delay_error_comparison.svg")
    render_calibration_chart(summary, OUTPUT_DIR / "configured_vs_measured.svg")
    write_report(inventory, summary, OUTPUT_DIR / "README.md")
    print(OUTPUT_DIR)


if __name__ == "__main__":
    main()
