"""K15 input-reference acquisition with a continuous external input.

This program never sends a ``WGEN`` command.  It observes only:

* C1: OUT3 / TD1
* C2: OUT4 / TD2
* C3: external input reference

The input source may therefore be a different board or an independently
configured generator. It is deliberately separate from runner.py, whose
autonomous sweep also manages a generator.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


AUTOMATION_DIR = Path(__file__).resolve().parents[1]
if str(AUTOMATION_DIR) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_DIR))

from common.plan import (
    SweepStep,
    build_steps,
    firmware_header_matches,
    load_config,
    validate_config,
)
from common.rtm3004 import RTM3004, ScopeError
from k15.runner import _edge_metrics, _quantile


HOST = "192.168.50.3"
DEFAULT_SEGMENTS = 3
MAIN_SEGMENTS = 1000
CHANNELS = (1, 2, 3)
FIXED_THRESHOLD_V = 1.65
DEFAULT_INPUT_FREQUENCY_HZ = 10.0
K15_CONFIG_PATH = Path(__file__).with_name("config_input_reference.json")
FIRMWARE_HEADER_PATH = AUTOMATION_DIR.parents[1] / "Core" / "Inc" / "autonomous_sweep.h"
STEP_IDLE_GAP_S = 0.50
HISTORY_EXPORT_RETRIES = 3


def apply(scope: RTM3004, command: str) -> None:
    """Apply a scope-only command and associate an error with that command."""
    scope.scpi.write(command)
    scope.scpi.query("*OPC?")
    try:
        scope.check_errors()
    except ScopeError as error:
        raise ScopeError(f"RTM3004 rejected setup command {command!r}: {error}") from error


def configure(scope: RTM3004) -> None:
    """Set only acquisition/channel/trigger state; leave WGEN untouched."""
    commands = [
        "*CLS",
        "STOP",
        "ACQ:SEGM:STAT OFF",
        "CHAN1:STAT ON",
        "CHAN2:STAT ON",
        "CHAN3:STAT ON",
        "CHAN4:STAT OFF",
        "CHAN1:COUP DC",
        "CHAN1:SCAL 1",
        "CHAN1:OFFS 0",
        "CHAN1:POS 0",
        "CHAN1:BAND FULL",
        "CHAN1:SKEW 0",
        "CHAN1:TYPE SAMP",
        "CHAN2:COUP DC",
        "CHAN2:SCAL 1",
        "CHAN2:OFFS 0",
        "CHAN2:POS 0",
        "CHAN2:BAND FULL",
        "CHAN2:SKEW 0",
        "CHAN2:TYPE SAMP",
        # C3 can use a probe type that rejects COUP/BAND writes.  Retain its
        # hardware-specific coupling and set only the verified display range.
        "CHAN3:SCAL 1",
        "CHAN3:POS 0",
        "ACQ:MEM:MODE DMEM",
        "ACQ:POIN:AUT OFF",
        "ACQ:POIN 5000",
        # Center first.  This avoids the RTM3004 -222 error if a previous
        # manual view used a much slower timebase and displaced the position.
        "TIM:POS 0",
        "TIM:SCAL 1e-8",
        "TIM:POS 4.5e-8",
        "TRIG:A:TYPE EDGE",
        "TRIG:A:SOUR CH3",
        "TRIG:A:EDGE:SLOP POS",
        "TRIG:A:LEV1 1.65",
        "TRIG:A:MODE NORM",
        "REFL:REL:MODE USER",
        "REFL:REL:LOW 10",
        "REFL:REL:MIDD 50",
        "REFL:REL:UPP 90",
        "MEAS1:TIM:AUTO OFF",
        "FORM REAL,32",
        "FORM:BORD LSBF",
        "ACQ:SEGM:STAT ON",
    ]
    for command in commands:
        apply(scope, command)

    actual_scale = scope.scpi.query_float("TIM:SCAL?")
    if not math.isclose(actual_scale, 1e-8, rel_tol=0.01, abs_tol=1e-15):
        raise ScopeError(
            f"RTM3004 did not retain 10 ns/div (read {actual_scale:.6g} s/div)"
        )
    scope._fast_segmentation = True  # arm_segmented validates this mode.


def wait_for_segments(scope: RTM3004, expected: int, timeout_s: float) -> int:
    """Wait for external trigger events without querying or changing WGEN."""
    deadline = time.monotonic() + timeout_s
    last_available = -1
    last_state = ""
    waiting = False
    while time.monotonic() < deadline:
        last_state, waiting = scope._acquisition_status()
        available = scope.available_segments()
        if available != last_available:
            print(f"Progress: {available}/{expected} segments", flush=True)
            last_available = available
        if last_state.startswith("COMP"):
            if available < expected:
                raise ScopeError(
                    "RTM3004 reported completed history before all segments were "
                    f"available: {available}/{expected}"
                )
            # K15 can report the final available count while its History data
            # is still being committed.  Do not issue CHAN:HIST/CHAN:DATA
            # commands until the acquisition completion operation has ended.
            scope.scpi.query("*OPC?")
            scope.check_errors()
            return available
        if last_state.startswith("STOP") and not waiting:
            if available >= expected:
                # Some RTM3004 firmware revisions use STOP rather than COMP
                # after a finite segmented acquisition.  The OPC barrier is
                # still required before the History memory is readable.
                scope.scpi.query("*OPC?")
                scope.check_errors()
                return available
            break
        time.sleep(0.20)
    raise ScopeError(
        "Timed out or stopped before K15 completed: "
        f"{max(last_available, 0)}/{expected} segments, "
        f"state={last_state!r}, WTRIG={int(waiting)}. "
        "Check only the external C3 pulse and its 1.65 V rising trigger."
    )


def percentile(values: list[float], fraction: float) -> float:
    return _quantile(values, fraction)


def write_results(
    scope: RTM3004,
    segment_count: int,
    output_dir: Path,
    step: SweepStep | None = None,
    history_ready: bool = False,
    fast_history_read: bool = False,
) -> dict[str, Any]:
    """Export C1/C2/C3 50%-crossing timing and report the requested metrics."""
    output_dir.mkdir(parents=True, exist_ok=False)
    samples_path = output_dir / "input_output_segments.csv"
    d1_values: list[float] = []
    d2_values: list[float] = []
    delta_values: list[float] = []

    with samples_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "segment", "t50_c1_s", "t50_c2_s", "t50_c3_s",
            "d1_c1_minus_input_s", "d2_c2_minus_input_s",
            "delta_c2_minus_c1_s", "c1_amplitude_v", "c2_amplitude_v",
            "c3_amplitude_v", "c1_rise_10_90_s", "c2_rise_10_90_s",
            "c3_rise_10_90_s",
        ])
        # The normal capture path requires one final completion barrier. A
        # recovery caller has already proved the oldest retained history
        # entry is readable; issuing *OPC? again can hang on RTM3004 after an
        # interrupted remote run even though the history itself is valid.
        if not history_ready:
            scope.scpi.query("*OPC?")
            scope.check_errors()
        for segment in range(1, segment_count + 1):
            # RTM history selection is zero for newest and negative backwards.
            history_index = segment - segment_count
            waveforms = None
            last_error: ScopeError | None = None
            for attempt in range(1, HISTORY_EXPORT_RETRIES + 1):
                try:
                    waveforms = scope.read_history_segment_channels(
                        history_index,
                        "MAX",
                        CHANNELS,
                        verify_selection=not fast_history_read,
                        wait_for_selection=not fast_history_read,
                        check_errors=not fast_history_read,
                    )
                    break
                except ScopeError as error:
                    last_error = error
                    if attempt < HISTORY_EXPORT_RETRIES:
                        print(
                            f"History retry {attempt}/{HISTORY_EXPORT_RETRIES - 1}: "
                            f"segment {segment}/{segment_count}",
                            flush=True,
                        )
                        time.sleep(0.5)
            if waveforms is None:
                raise ScopeError(
                    "K15 History export failed at "
                    f"segment {segment}/{segment_count} "
                    f"(HIST:CURR {history_index}) after "
                    f"{HISTORY_EXPORT_RETRIES} attempts: {last_error}"
                ) from last_error
            c1 = _edge_metrics(waveforms[1], FIXED_THRESHOLD_V)
            c2 = _edge_metrics(waveforms[2], FIXED_THRESHOLD_V)
            c3 = _edge_metrics(waveforms[3], FIXED_THRESHOLD_V)
            d1 = c1.relative_50_time_s - c3.relative_50_time_s
            d2 = c2.relative_50_time_s - c3.relative_50_time_s
            delta = c2.relative_50_time_s - c1.relative_50_time_s
            d1_values.append(d1)
            d2_values.append(d2)
            delta_values.append(delta)
            writer.writerow([
                segment, c1.relative_50_time_s, c2.relative_50_time_s,
                c3.relative_50_time_s, d1, d2, delta, c1.amplitude_v,
                c2.amplitude_v, c3.amplitude_v, c1.rise_time_s,
                c2.rise_time_s, c3.rise_time_s,
            ])
            if segment % 25 == 0 or segment == segment_count:
                print(f"Export: {segment}/{segment_count}", flush=True)

    def stats(values: list[float]) -> dict[str, float]:
        return {
            "mean_ps": statistics.mean(values) * 1e12,
            "rms_ps": statistics.stdev(values) * 1e12 if len(values) > 1 else 0.0,
            "p99_minus_p1_ps": (percentile(values, 0.99) - percentile(values, 0.01)) * 1e12,
            "min_ps": min(values) * 1e12,
            "max_ps": max(values) * 1e12,
        }

    summary: dict[str, Any] = {
        "measurement": {
            "samples": segment_count,
            "edge_definition": "rising 50% of each waveform amplitude",
            "channels": {"c1": "OUT3 / TD1", "c2": "OUT4 / TD2", "c3": "external input"},
            "timebase_s_per_div": 1e-8,
            "trigger": "C3 rising, 1.65 V, normal",
        },
        "d1_c1_minus_input": stats(d1_values),
        "d2_c2_minus_input": stats(d2_values),
        "delta_c2_minus_c1": stats(delta_values),
    }
    if step is not None:
        summary["sweep_step"] = {
            "index": step.index,
            "id": step.set_id,
            "requested_delay_ps": step.delay_ps,
            "signed_ticks": step.signed_ticks,
            "actual_delay_ps": step.actual_delay_ps,
        }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def wait_for_board_step_transition() -> None:
    """Give firmware the required PB9 idle time before the next burst.

    The board applies the next autonomous delay after it has observed no input
    rising edge for 300 ms.  The extra margin protects against the last pulse
    and lets the HRTIM register writes settle before K15 is armed again.
    """
    print(
        "Stop the external input now. Keep it OFF for at least 300 ms, "
        "then press Enter to continue."
    )
    input()
    time.sleep(STEP_IDLE_GAP_S)


def run_external_sweep(scope: RTM3004, config_path: Path, timeout_s: float | None) -> None:
    """Measure each firmware delay while the operator supplies finite bursts."""
    config = load_config(config_path)
    steps = build_steps(config)
    validate_config(config, steps)
    segment_count = int(config["acquisition"]["waveforms_per_step"])
    firmware_pulses = int(config["autonomous"].get("pulses_per_step", segment_count))
    transition_guard_pulses = int(config["autonomous"].get("transition_guard_pulses", 30))
    if not firmware_header_matches(
        FIRMWARE_HEADER_PATH, steps, firmware_pulses, transition_guard_pulses
    ):
        raise ScopeError(
            "config_input_reference.json differs from Core/Inc/autonomous_sweep.h. "
            "Build and flash the matching firmware plan before sweeping."
        )
    if timeout_s is None:
        frequency_hz = float(config["generator"]["frequency_hz"])
        timeout_s = max(30.0, segment_count * 1.5 / frequency_hz + 10.0)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = AUTOMATION_DIR / "output" / "k15_external_input_sweep" / stamp
    output_root.mkdir(parents=True, exist_ok=False)
    (output_root / "manifest.json").write_text(
        json.dumps(
            {
                "mode": "external_input_operator_burst",
                "config_path": str(config_path),
                "segments_per_step": segment_count,
                "required_input_idle_gap_ms": 300,
                "steps": [
                    {
                        "index": step.index,
                        "id": step.set_id,
                        "requested_delay_ps": step.delay_ps,
                        "signed_ticks": step.signed_ticks,
                        "actual_delay_ps": step.actual_delay_ps,
                    }
                    for step in steps
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    summary_rows: list[dict[str, Any]] = []

    print(
        f"Sweep plan: {len(steps)} steps x {segment_count} pulses. "
        "The program never controls the external input source."
    )
    input("Keep the external input OFF, RESET the delay board, then press Enter: ")
    for position, step in enumerate(steps):
        step_dir = output_root / step.prefix
        print(
            f"\n[{position + 1}/{len(steps)}] {step.prefix}: "
            f"requested {step.delay_ps} ps, actual {step.actual_delay_ps:.3f} ps"
        )
        preflight = scope.arm_segmented(segment_count)
        (step_dir.parent / f"{step.prefix}_preflight.json").write_text(
            json.dumps(preflight, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        input(
            f"K15 armed. Start exactly {segment_count} external input rising edges "
            "for this step, then stop the input: "
        )
        captured = wait_for_segments(scope, segment_count, timeout_s)
        print(f"CAPTURE OK: {captured}/{segment_count} segments")
        step_dir.mkdir()
        result = write_results(scope, segment_count, step_dir, step)
        summary_rows.append(
            {
                "step": step.prefix,
                "requested_delay_ps": step.delay_ps,
                "actual_delay_ps": step.actual_delay_ps,
                "d1_mean_ps": result["d1_c1_minus_input"]["mean_ps"],
                "d1_rms_ps": result["d1_c1_minus_input"]["rms_ps"],
                "d2_mean_ps": result["d2_c2_minus_input"]["mean_ps"],
                "d2_rms_ps": result["d2_c2_minus_input"]["rms_ps"],
                "delta_mean_ps": result["delta_c2_minus_c1"]["mean_ps"],
                "delta_rms_ps": result["delta_c2_minus_c1"]["rms_ps"],
            }
        )
        if position + 1 < len(steps):
            wait_for_board_step_transition()

    with (output_root / "sweep_summary.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summary_rows[0]))
        writer.writeheader()
        writer.writerows(summary_rows)
    print(f"SWEEP OK: {output_root}")


def main() -> int:
    parser = argparse.ArgumentParser(description="K15 external-input C1/C2/C3 measurement")
    parser.add_argument("--host", default=None, help="Override the scope host from --config")
    parser.add_argument("--segments", type=int, default=DEFAULT_SEGMENTS)
    parser.add_argument("--main", action="store_true", help="capture 1000 segments")
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="run all firmware steps using operator-provided finite input bursts",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=K15_CONFIG_PATH,
        help="firmware-plan JSON used for --sweep",
    )
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("--capture-only", action="store_true", help="do not export waveforms/results")
    args = parser.parse_args()
    if args.sweep and args.main:
        parser.error("--sweep and --main cannot be used together")
    segment_count = MAIN_SEGMENTS if args.main else args.segments
    if segment_count <= 0:
        parser.error("--segments must be positive")
    timeout_s = (
        args.timeout
        if args.timeout is not None
        else max(30.0, segment_count * 1.5 / DEFAULT_INPUT_FREQUENCY_HZ + 10.0)
    )

    scope: RTM3004 | None = None
    preserve_history_on_exit = False
    try:
        scope_host = args.host or str(load_config(args.config)["scope"]["host"])
        scope = RTM3004(scope_host, timeout_s=20.0)
        print(f"Connected: {scope.identity()}")
        print(f"Options: {scope.verify_k15_option()}")
        configure(scope)
        print("Scope configured only. No WGEN command was sent.")
        if args.sweep:
            run_external_sweep(scope, args.config, args.timeout)
            return 0
        input(
            "Leave the continuous external input ON. Press RESET on the delay "
            "board, then press Enter immediately to arm K15: "
        )
        preflight = scope.arm_segmented(segment_count)
        print(f"K15 armed: {preflight}")
        print(f"Collecting the next {segment_count} external input edges.")
        captured = wait_for_segments(scope, segment_count, timeout_s)
        print(f"CAPTURE OK: {captured}/{segment_count} segments")
        # A completed segmented acquisition is stopped by the scope itself.
        # Keep its K15 History addressable until a full export has succeeded;
        # otherwise a transient History read failure would discard the only
        # copy of the captured 1000 records during finally cleanup.
        preserve_history_on_exit = True
        if not args.capture_only:
            time.sleep(0.5)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_dir = AUTOMATION_DIR / "output" / "k15_external_input" / stamp
            write_results(scope, segment_count, output_dir, history_ready=True)
            print(f"Saved: {output_dir}")
            preserve_history_on_exit = False
        return 0
    except Exception as error:
        print(f"CAPTURE FAILED: {error}")
        return 1
    finally:
        if scope is not None:
            if preserve_history_on_exit:
                print(
                    "K15 History retained on the scope for recovery; "
                    "do not press Clear/Auto Setup before running the recovery exporter.",
                    flush=True,
                )
            else:
                try:
                    # This runs only after a successful export.  The external
                    # input source is intentionally never controlled here.
                    scope.scpi.write("STOP")
                    scope.scpi.write("ACQ:SEGM:STAT OFF")
                except Exception:
                    # Closing the socket is still safe if the scope was
                    # manually changed while the run was completing.
                    pass
            scope.close()


if __name__ == "__main__":
    raise SystemExit(main())
