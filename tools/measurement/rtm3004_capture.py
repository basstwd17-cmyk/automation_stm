#!/usr/bin/env python3
"""Capture the displayed RTM3004 C1/C2 waveforms into a CSV file."""

from __future__ import annotations

import argparse
import csv
import json
import math
import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .rtm3004_scpi import DEFAULT_HOST, DEFAULT_PORT, ScpiSocket
except ImportError:
    from rtm3004_scpi import DEFAULT_HOST, DEFAULT_PORT, ScpiSocket


@dataclass(frozen=True)
class Trace:
    channel: int
    x_start_s: float
    x_stop_s: float
    x_increment_s: float
    points: int
    values_v: tuple[float, ...]


def parse_header(response: str) -> tuple[float, float, int, int]:
    fields = [field.strip() for field in response.split(",")]
    if len(fields) != 4:
        raise ValueError(f"Unexpected waveform header: {response!r}")
    return float(fields[0]), float(fields[1]), int(fields[2]), int(fields[3])


def read_trace(
    scope: ScpiSocket,
    channel: int,
    byte_order: str,
    max_points: int,
) -> Trace:
    x_start, x_stop, header_points, acquisition_count = parse_header(
        scope.query(f"CHAN{channel}:DATA:HEAD?")
    )
    selected_points = int(scope.query(f"CHAN{channel}:DATA:POIN?"))
    x_increment = float(scope.query(f"CHAN{channel}:DATA:XINC?"))

    if acquisition_count != 1:
        raise ValueError(
            f"C{channel} contains {acquisition_count} acquisitions; "
            "segmented capture is not supported yet"
        )
    if header_points != selected_points:
        raise ValueError(
            f"C{channel} header reports {header_points} points but "
            f"DATA:POINts reports {selected_points}"
        )
    if header_points > max_points:
        raise ValueError(
            f"C{channel} contains {header_points} points, above the "
            f"--max-points limit of {max_points}"
        )

    payload = scope.query_binary_block(f"CHAN{channel}:DATA?")
    expected_bytes = header_points * 4
    if len(payload) != expected_bytes:
        raise ValueError(
            f"C{channel} returned {len(payload)} bytes; expected {expected_bytes} "
            "for REAL,32 data"
        )

    endian = "<" if byte_order.upper().startswith("LSB") else ">"
    values = struct.unpack(f"{endian}{header_points}f", payload)
    return Trace(
        channel=channel,
        x_start_s=x_start,
        x_stop_s=x_stop,
        x_increment_s=x_increment,
        points=header_points,
        values_v=values,
    )


def verify_common_time_axis(c1: Trace, c2: Trace) -> None:
    if c1.points != c2.points:
        raise ValueError(f"C1/C2 point counts differ: {c1.points} != {c2.points}")
    if not math.isclose(c1.x_start_s, c2.x_start_s, rel_tol=0.0, abs_tol=1e-15):
        raise ValueError("C1/C2 start times differ")
    if not math.isclose(
        c1.x_increment_s, c2.x_increment_s, rel_tol=0.0, abs_tol=1e-18
    ):
        raise ValueError("C1/C2 sample intervals differ")


def first_rising_crossing(trace: Trace) -> tuple[float | None, float]:
    low = min(trace.values_v)
    high = max(trace.values_v)
    threshold = (low + high) / 2.0

    for index in range(1, trace.points):
        y0 = trace.values_v[index - 1]
        y1 = trace.values_v[index]
        if y0 < threshold <= y1 and y1 != y0:
            fraction = (threshold - y0) / (y1 - y0)
            crossing_index = index - 1 + fraction
            return trace.x_start_s + crossing_index * trace.x_increment_s, threshold
    return None, threshold


def default_output_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(__file__).resolve().parent / "captures" / f"rtm3004_{timestamp}.csv"


def write_capture(csv_path: Path, c1: Trace, c2: Trace, metadata: dict[str, Any]) -> Path:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(("time_s", "c1_voltage_v", "c2_voltage_v"))
        for index, (c1_value, c2_value) in enumerate(zip(c1.values_v, c2.values_v)):
            time_s = c1.x_start_s + index * c1.x_increment_s
            writer.writerow((f"{time_s:.12e}", f"{c1_value:.9g}", f"{c2_value:.9g}"))

    metadata_path = csv_path.with_suffix(".json")
    with metadata_path.open("w", encoding="utf-8") as output:
        json.dump(metadata, output, indent=2)
        output.write("\n")
    return metadata_path


def capture(host: str, port: int, timeout: float, max_points: int) -> tuple[Trace, Trace, dict[str, Any]]:
    with ScpiSocket(host, port, timeout) as scope:
        identity = scope.query("*IDN?")
        original_format = scope.query("FORM?")
        byte_order = scope.query("FORM:BORD?")

        try:
            scope.write("FORM REAL,32")
            c1 = read_trace(scope, 1, byte_order, max_points)
            c2 = read_trace(scope, 2, byte_order, max_points)
        finally:
            scope.write(f"FORM {original_format}")

        verify_common_time_axis(c1, c2)
        system_error = scope.query("SYST:ERR?")

    metadata = {
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "instrument": identity,
        "endpoint": f"{host}:{port}",
        "transfer_format_used": "REAL,32",
        "transfer_format_restored": original_format,
        "byte_order": byte_order,
        "points": c1.points,
        "x_start_s": c1.x_start_s,
        "x_stop_s": c1.x_stop_s,
        "x_increment_s": c1.x_increment_s,
        "system_error": system_error,
        "note": "C1 and C2 were read sequentially; a running acquisition is not frozen.",
    }
    return c1, c2, metadata


def print_summary(csv_path: Path, metadata_path: Path, c1: Trace, c2: Trace) -> None:
    c1_edge, c1_threshold = first_rising_crossing(c1)
    c2_edge, c2_threshold = first_rising_crossing(c2)

    print(f"CSV      : {csv_path}")
    print(f"Metadata : {metadata_path}")
    print(
        f"Time axis: {c1.x_start_s * 1e9:.3f} ns to "
        f"{c1.x_stop_s * 1e9:.3f} ns, "
        f"{c1.x_increment_s * 1e12:.3f} ps/sample, {c1.points} points"
    )
    print(
        f"C1 range : {min(c1.values_v):.4g} V to {max(c1.values_v):.4g} V, "
        f"50% threshold {c1_threshold:.4g} V"
    )
    print(
        f"C2 range : {min(c2.values_v):.4g} V to {max(c2.values_v):.4g} V, "
        f"50% threshold {c2_threshold:.4g} V"
    )
    if c1_edge is not None:
        print(f"C1 edge  : {c1_edge * 1e9:.4f} ns")
    else:
        print("C1 edge  : no rising 50% crossing found")
    if c2_edge is not None:
        print(f"C2 edge  : {c2_edge * 1e9:.4f} ns")
    else:
        print("C2 edge  : no rising 50% crossing found")
    if c1_edge is not None and c2_edge is not None:
        print(f"C2 - C1  : {(c2_edge - c1_edge) * 1e9:.4f} ns")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture the currently displayed RTM3004 C1/C2 data over SCPI/TCP."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--max-points", type=int, default=100_000)
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_path = args.output.resolve() if args.output else default_output_path()
    c1, c2, metadata = capture(args.host, args.port, args.timeout, args.max_points)
    metadata_path = write_capture(output_path, c1, c2, metadata)
    print_summary(output_path, metadata_path, c1, c2)


if __name__ == "__main__":
    main()
