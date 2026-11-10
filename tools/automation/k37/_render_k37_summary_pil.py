"""Render one PNG overview from a completed K37 result folder."""

from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


RESULT_DIR = Path(__file__).resolve().parents[3] / "output/k37_spectrum/20260728_100203_main"
OUTPUT_PATH = RESULT_DIR / "k37_spectrum_summary.png"
BANDS_MHZ = ((6.0, 300.0), (300.0, 600.0), (600.0, 900.0), (900.0, 1200.0))
COLORS = ((37, 99, 235), (220, 38, 38), (5, 150, 105), (124, 58, 237))
GRAY, DARK, MUTED = (209, 213, 219), (31, 41, 55), (75, 85, 99)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(Path(r"C:\Windows\Fonts") / ("segoeuib.ttf" if bold else "segoeui.ttf"), size)


def delay_label(delay_ns: float) -> str:
    return f"{delay_ns:+.3f} ns" if delay_ns else "0 ns"


def load_records() -> list[dict[str, object]]:
    with (RESULT_DIR / "summary.csv").open(encoding="utf-8-sig", newline="") as stream:
        summary = list(csv.DictReader(stream))
    records: list[dict[str, object]] = []
    for row in summary:
        prefix = row["prefix"]
        with (RESULT_DIR / prefix / f"{prefix}_spectrum.csv").open(encoding="utf-8-sig", newline="") as stream:
            spectrum = list(csv.DictReader(stream))
        values = [(float(item["frequency_hz"]) / 1e6, float(item["average_level"])) for item in spectrum]
        records.append({
            "prefix": prefix,
            "channel": row["source"],
            "delay_ns": float(row["actual_delay_ps"]) / 1000.0,
            "captured": int(row["captured_acquisitions"]),
            "planned": int(row["planned_acquisitions"]),
            "bands": [statistics.fmean(level for frequency, level in values if low <= frequency < high) for low, high in BANDS_MHZ],
        })
    return records


def line_chart(image: Image.Image, box: tuple[int, int, int, int], records: list[dict[str, object]], channel: str) -> None:
    draw = ImageDraw.Draw(image)
    x0, y0, x1, y1 = box
    left, top, right, bottom = x0 + 90, y0 + 55, x1 - 35, y1 - 65
    draw.text((x0, y0), f"{channel}: band-averaged Spectrum", fill=DARK, font=font(30, True))
    y_min, y_max = -68.5, -55.5
    for value in (-68, -65, -62, -59, -56):
        y = bottom - (value - y_min) / (y_max - y_min) * (bottom - top)
        draw.line((left, y, right, y), fill=GRAY, width=2)
        draw.text((left - 15, y - 12), f"{value}", fill=MUTED, font=font(18), anchor="ra")
    draw.line((left, top, left, bottom), fill=DARK, width=2)
    draw.line((left, bottom, right, bottom), fill=DARK, width=2)
    labels = [f"{low:.0f}-{high:.0f}" for low, high in BANDS_MHZ]
    xs = [left + index * (right - left) / 3 for index in range(4)]
    for x, label in zip(xs, labels, strict=True):
        draw.text((x, bottom + 16), label, fill=MUTED, font=font(17), anchor="ma")
    draw.text((left - 58, (top + bottom) / 2), "dBV", fill=MUTED, font=font(18), anchor="mm")
    ordered = sorted(records, key=lambda item: float(item["delay_ns"]))
    for color, record in zip(COLORS, ordered, strict=True):
        points = [(x, bottom - (float(value) - y_min) / (y_max - y_min) * (bottom - top)) for x, value in zip(xs, record["bands"], strict=True)]
        draw.line(points, fill=color, width=5)
        for point in points:
            draw.ellipse((point[0] - 6, point[1] - 6, point[0] + 6, point[1] + 6), fill=color)
    for index, record in enumerate(ordered):
        legend_x = left + index * 126
        draw.line((legend_x, y1 - 24, legend_x + 28, y1 - 24), fill=COLORS[index], width=5)
        draw.text((legend_x + 36, y1 - 24), delay_label(float(record["delay_ns"])), fill=DARK, font=font(16), anchor="lm")


def capture_chart(image: Image.Image, box: tuple[int, int, int, int], records: list[dict[str, object]]) -> None:
    draw = ImageDraw.Draw(image)
    x0, y0, x1, y1 = box
    left, top, right, bottom = x0 + 90, y0 + 55, x1 - 35, y1 - 80
    draw.text((x0, y0), "Planned vs actual scope capture count", fill=DARK, font=font(30, True))
    for value in (0, 25, 50, 75, 100):
        y = bottom - value / 100 * (bottom - top)
        draw.line((left, y, right, y), fill=GRAY, width=2)
        draw.text((left - 15, y - 12), str(value), fill=MUTED, font=font(18), anchor="ra")
    draw.line((left, top, left, bottom), fill=DARK, width=2)
    draw.line((left, bottom, right, bottom), fill=DARK, width=2)
    step = (right - left) / len(records)
    for index, record in enumerate(records):
        center = left + step * (index + 0.5)
        planned_top = bottom - float(record["planned"]) / 100 * (bottom - top)
        captured_top = bottom - float(record["captured"]) / 100 * (bottom - top)
        draw.rectangle((center - 29, planned_top, center + 29, bottom), fill=GRAY)
        color = COLORS[0] if record["channel"] == "CH1" else COLORS[1]
        draw.rectangle((center - 20, captured_top, center + 20, bottom), fill=color)
        draw.text((center, captured_top - 10), str(record["captured"]), fill=DARK, font=font(19, True), anchor="ms")
        draw.multiline_text((center, bottom + 18), str(record["prefix"]).replace("_T", "\nT"), fill=MUTED, font=font(15), anchor="ma", align="center")
    draw.text((left, y1 - 22), "Gray: planned 100   Blue: CH1 actual   Red: CH2 actual", fill=MUTED, font=font(18))


def main() -> None:
    global RESULT_DIR, OUTPUT_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, nargs="?", default=RESULT_DIR)
    args = parser.parse_args()
    RESULT_DIR = args.run_dir.resolve()
    OUTPUT_PATH = RESULT_DIR / "k37_spectrum_summary.png"
    records = load_records()
    image = Image.new("RGB", (2200, 1500), "white")
    draw = ImageDraw.Draw(image)
    draw.text((70, 35), "K37 Spectrum Overview", fill=DARK, font=font(48, True))
    draw.text((70, 96), "0-1.2 GHz, RBW 5.99 MHz | diagnostic data; not a completed 100-capture average", fill=MUTED, font=font(24))
    line_chart(image, (70, 170, 1080, 850), [record for record in records if record["channel"] == "CH1"], "CH1")
    line_chart(image, (1120, 170, 2130, 850), [record for record in records if record["channel"] == "CH2"], "CH2")
    capture_chart(image, (70, 900, 2130, 1430), records)
    image.save(OUTPUT_PATH, "PNG", optimize=True)
    print(OUTPUT_PATH)


if __name__ == "__main__":
    main()
