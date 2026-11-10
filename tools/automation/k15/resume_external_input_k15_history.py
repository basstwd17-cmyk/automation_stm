"""Resume a partial full-waveform K15 History export with bounded sessions."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import time
from pathlib import Path


AUTOMATION_DIR = Path(__file__).resolve().parents[1]
if str(AUTOMATION_DIR) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_DIR))

from common.rtm3004 import RTM3004
from k15.external_input_k15 import CHANNELS, FIXED_THRESHOLD_V, HOST
from k15.runner import _edge_metrics, _quantile


SESSION_SEGMENTS = 100
FIELDNAMES = [
    "segment", "t50_c1_s", "t50_c2_s", "t50_c3_s",
    "d1_c1_minus_input_s", "d2_c2_minus_input_s",
    "delta_c2_minus_c1_s", "c1_amplitude_v", "c2_amplitude_v",
    "c3_amplitude_v", "c1_rise_10_90_s", "c2_rise_10_90_s",
    "c3_rise_10_90_s",
]


def completed_segments(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        return 0
    expected = 1
    for row in rows:
        if int(row["segment"]) != expected:
            raise RuntimeError(f"{path}: segment sequence is not contiguous at {expected}")
        expected += 1
    return len(rows)


def connect(required_segments: int) -> RTM3004:
    # The retained history count was verified before starting recovery. On
    # this RTM3004, asking HIST:TSR:ALL? again during a long export can time
    # out despite individual History records still being perfectly readable.
    del required_segments
    return RTM3004(HOST, timeout_s=20.0)


def append_segment(writer: csv.DictWriter, scope: RTM3004, segment: int, total: int) -> None:
    waveforms = scope.read_history_segment_channels(
        segment - total,
        "MAX",
        CHANNELS,
        verify_selection=True,
        wait_for_selection=False,
        check_errors=False,
    )
    c1 = _edge_metrics(waveforms[1], FIXED_THRESHOLD_V)
    c2 = _edge_metrics(waveforms[2], FIXED_THRESHOLD_V)
    c3 = _edge_metrics(waveforms[3], FIXED_THRESHOLD_V)
    writer.writerow(
        {
            "segment": segment,
            "t50_c1_s": c1.relative_50_time_s,
            "t50_c2_s": c2.relative_50_time_s,
            "t50_c3_s": c3.relative_50_time_s,
            "d1_c1_minus_input_s": c1.relative_50_time_s - c3.relative_50_time_s,
            "d2_c2_minus_input_s": c2.relative_50_time_s - c3.relative_50_time_s,
            "delta_c2_minus_c1_s": c2.relative_50_time_s - c1.relative_50_time_s,
            "c1_amplitude_v": c1.amplitude_v,
            "c2_amplitude_v": c2.amplitude_v,
            "c3_amplitude_v": c3.amplitude_v,
            "c1_rise_10_90_s": c1.rise_time_s,
            "c2_rise_10_90_s": c2.rise_time_s,
            "c3_rise_10_90_s": c3.rise_time_s,
        }
    )


def stats(values: list[float]) -> dict[str, float]:
    return {
        "mean_ps": statistics.mean(values) * 1e12,
        "rms_ps": statistics.stdev(values) * 1e12,
        "p99_minus_p1_ps": (_quantile(values, 0.99) - _quantile(values, 0.01)) * 1e12,
        "min_ps": min(values) * 1e12,
        "max_ps": max(values) * 1e12,
    }


def write_summary(path: Path, expected: int) -> None:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != expected:
        raise RuntimeError(f"Expected {expected} rows, found {len(rows)}")
    result = {
        "measurement": {
            "samples": expected,
            "edge_definition": "rising 50% of each waveform amplitude",
            "channels": {"c1": "OUT3 / TD1", "c2": "OUT4 / TD2", "c3": "external input"},
            "export_mode": "resumed K15 History, raw C1/C2/C3 waveforms",
        },
        "d1_c1_minus_input": stats([float(row["d1_c1_minus_input_s"]) for row in rows]),
        "d2_c2_minus_input": stats([float(row["d2_c2_minus_input_s"]) for row in rows]),
        "delta_c2_minus_c1": stats([float(row["delta_c2_minus_c1_s"]) for row in rows]),
    }
    (path.parent / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume K15 full-history export")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--segments", type=int, required=True)
    args = parser.parse_args()
    if args.segments <= 0:
        parser.error("--segments must be positive")
    if not args.csv_path.is_file():
        parser.error(f"missing CSV: {args.csv_path}")

    start = completed_segments(args.csv_path) + 1
    print(f"Resuming at segment {start}/{args.segments}", flush=True)
    scope: RTM3004 | None = None
    try:
        with args.csv_path.open("a", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDNAMES)
            for segment in range(start, args.segments + 1):
                if scope is None or (segment - start) % SESSION_SEGMENTS == 0:
                    if scope is not None:
                        scope.close()
                    scope = connect(args.segments)
                    print(f"Connected for segments {segment}..{min(segment + SESSION_SEGMENTS - 1, args.segments)}", flush=True)
                for attempt in range(1, 4):
                    try:
                        append_segment(writer, scope, segment, args.segments)
                        stream.flush()
                        break
                    except Exception as error:
                        if scope is not None:
                            scope.close()
                            scope = None
                        if attempt == 3:
                            raise RuntimeError(f"segment {segment} failed after retries: {error}") from error
                        print(f"Retry {attempt}/2 at segment {segment}: {error}", flush=True)
                        time.sleep(0.5)
                        scope = connect(args.segments)
                if segment % 25 == 0 or segment == args.segments:
                    print(f"Export: {segment}/{args.segments}", flush=True)
        write_summary(args.csv_path, args.segments)
        print(f"COMPLETED: {args.csv_path.parent}", flush=True)
        return 0
    finally:
        if scope is not None:
            scope.close()


if __name__ == "__main__":
    raise SystemExit(main())
