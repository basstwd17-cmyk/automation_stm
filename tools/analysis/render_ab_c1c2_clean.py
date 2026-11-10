"""Render a clean white-background C1/C2 A/B comparison figure as SVG."""
from __future__ import annotations

import csv
import html
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "output" / "delay_sweep_automation" / "analysis_ab_c1c2_20260727" / "ab_c1c2_summary.csv"
OUTPUT = DATA.parent / "ab_c1c2_comparison_clean.svg"


def text(x, y, value, size=16, color="#344054", weight=400, anchor="start"):
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="Arial, Malgun Gothic, sans-serif" '
        f'font-size="{size}" fill="{color}" font-weight="{weight}" text-anchor="{anchor}">'
        f'{html.escape(str(value))}</text>'
    )


def main():
    with DATA.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows.sort(key=lambda row: float(row["actual_delay_ps"]))

    for row in rows:
        for key in row:
            if key != "condition_id":
                row[key] = float(row[key])

    width, height = 1600, 1000
    blue, orange, ink = "#3B82F6", "#F97316", "#101828"
    muted, grid, card = "#667085", "#E4E7EC", "#FFFFFF"
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#F7F8FA"/>',
        text(64, 60, "C1/C2 A/B Delay Comparison", 28, ink, 700),
        text(64, 91, "설정 지연 정확도와 10회 반복 재현성", 16, muted, 400),
        '<rect x="1160" y="40" width="166" height="34" rx="17" fill="#EFF6FF"/>',
        text(1243, 63, "7 delay × 10 repeat", 13, "#1D4ED8", 600, "middle"),
        '<rect x="1338" y="40" width="198" height="34" rx="17" fill="#FFF7ED"/>',
        text(1437, 63, "총 28,000 waveforms", 13, "#C2410C", 600, "middle"),
    ]

    # Top line chart.
    x0, y0, cw, ch = 90, 180, 1420, 360
    svg += [
        f'<rect x="{x0-30}" y="130" width="{cw+60}" height="470" rx="14" fill="{card}" stroke="#EAECF0"/>',
        text(x0, 163, "설정 지연과 A/B 보정 지연", 18, ink, 700),
        text(x0, 188, "단위: ns · 점선은 이상적인 설정값", 13, muted),
    ]

    ymin, ymax = -4.8, 4.8
    x_values = [row["actual_delay_ps"] / 1000 for row in rows]
    X = lambda value: x0 + x_values.index(value) / (len(x_values) - 1) * cw
    Y = lambda value: y0 + ch - (value - ymin) / (ymax - ymin) * ch

    for tick in (-4, -2, 0, 2, 4):
        sy = Y(tick)
        svg += [
            f'<line x1="{x0}" y1="{sy:.1f}" x2="{x0+cw}" y2="{sy:.1f}" stroke="{grid}"/>',
            text(x0-14, sy+5, f"{tick:+d}", 13, muted, 400, "end"),
        ]
    for row in rows:
        sx = X(row["actual_delay_ps"] / 1000)
        svg += [
            f'<line x1="{sx:.1f}" y1="{y0}" x2="{sx:.1f}" y2="{y0+ch}" stroke="#F2F4F7"/>',
            text(sx, y0+ch+28, f'{row["actual_delay_ps"]/1000:+.3f}', 13, muted, 400, "middle"),
        ]
    svg += [
        text(x0+cw/2, y0+ch+58, "실제 설정 지연 (ns)", 14, muted, 500, "middle"),
    ]
    ideal_points = " ".join(f'{X(value):.1f},{Y(value):.1f}' for value in x_values)
    svg.append(f'<polyline points="{ideal_points}" fill="none" stroke="#98A2B3" stroke-width="2" stroke-dasharray="7 7"/>')
    corrected_points = " ".join(
        f'{X(row["actual_delay_ps"]/1000):.1f},{Y(row["true_delay_ps"]/1000):.1f}' for row in rows
    )
    svg.append(f'<polyline points="{corrected_points}" fill="none" stroke="{blue}" stroke-width="3"/>')
    for row in rows:
        sx = X(row["actual_delay_ps"] / 1000)
        sy = Y(row["true_delay_ps"] / 1000)
        svg += [
            f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="6" fill="white" stroke="{blue}" stroke-width="3"/>',
        ]
    svg += [
        f'<line x1="{x0+1040}" y1="157" x2="{x0+1076}" y2="157" stroke="#98A2B3" stroke-width="2" stroke-dasharray="7 6"/>',
        text(x0+1086, 162, "설정 지연", 13, muted),
        f'<line x1="{x0+1180}" y1="157" x2="{x0+1216}" y2="157" stroke="{blue}" stroke-width="3"/>',
        f'<circle cx="{x0+1198}" cy="157" r="4" fill="white" stroke="{blue}" stroke-width="2"/>',
        text(x0+1226, 162, "A/B 보정 지연", 13, muted),
    ]

    # Bottom signed-error bar chart.
    bx, by, bw, bh = 90, 690, 920, 190
    svg += [
        '<rect x="60" y="630" width="1000" height="310" rx="14" fill="white" stroke="#EAECF0"/>',
        text(bx, 670, "설정값 대비 보정 오차", 18, ink, 700),
        text(bx, 696, "단위: ps · 0에 가까울수록 정확", 13, muted),
    ]
    elo, ehi = -220, 220
    EY = lambda value: by + bh - (value - elo) / (ehi - elo) * bh
    zero = EY(0)
    for tick in (-200, -100, 0, 100, 200):
        sy = EY(tick)
        svg += [
            f'<line x1="{bx}" y1="{sy:.1f}" x2="{bx+bw}" y2="{sy:.1f}" stroke="{grid if tick else "#98A2B3"}"/>',
            text(bx-12, sy+5, f"{tick:+d}", 12, muted, 400, "end"),
        ]
    step = bw / len(rows)
    for index, row in enumerate(rows):
        value = row["true_delay_error_ps"]
        cx = bx + step * (index + 0.5)
        top = min(zero, EY(value))
        bar_height = max(2, abs(EY(value) - zero))
        color = blue if value >= 0 else orange
        svg += [
            f'<rect x="{cx-27:.1f}" y="{top:.1f}" width="54" height="{bar_height:.1f}" rx="5" fill="{color}" opacity="0.90"/>',
            text(cx, top-10 if value >= 0 else top+bar_height+20, f"{value:+.0f}", 13, color, 700, "middle"),
            text(cx, by+bh+27, f'{row["actual_delay_ps"]/1000:+.3f}', 12, muted, 400, "middle"),
        ]

    # KPI summary card.
    kx, ky = 1100, 630
    svg += [
        f'<rect x="{kx}" y="{ky}" width="436" height="310" rx="14" fill="white" stroke="#EAECF0"/>',
        text(kx+30, ky+40, "핵심 지표", 18, ink, 700),
    ]
    metrics = [
        ("고정 측정 경로 스큐", "1.364 ns", "A/B 평균으로 분리"),
        ("A/B 보정 최대 오차", "197.9 ps", "전체 지연 범위"),
        ("보정값 10회 반복 σ", "≤ 5.2 ps", "조건별 반복 간 편차"),
        ("채널 교환 잔차", "≤ 42 ps", "부호 반전 검증"),
    ]
    for i, (name, value, note) in enumerate(metrics):
        yy = ky + 82 + i * 58
        if i:
            svg.append(f'<line x1="{kx+30}" y1="{yy-30}" x2="{kx+406}" y2="{yy-30}" stroke="#F2F4F7"/>')
        svg += [text(kx+30, yy, name, 14, muted), text(kx+406, yy, value, 18, blue if i != 1 else orange, 700, "end"), text(kx+30, yy+21, note, 11, "#98A2B3")]

    svg += [
        text(64, 978, "측정 정의: C2 rising 50% − C1 rising 50%  |  각 폴더 7조건 × 10반복 × 100파형  |  A/B 보정 = (A − B) / 2", 13, muted),
        "</svg>",
    ]
    OUTPUT.write_text("\n".join(svg), encoding="utf-8")
    print(OUTPUT)


if __name__ == "__main__":
    main()
