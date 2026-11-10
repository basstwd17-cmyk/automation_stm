"""Fast three-channel external-input sweep using RTM3004 measurement statistics.

Unlike the K15 history runner, this program does not download 3 x N waveform
records. The RTM3004 evaluates three simultaneous delay measurements and the
PC downloads only their N scalar values while the input runs continuously:

    MEAS1: C1 - C3 (OUT3 - input)
    MEAS2: C2 - C3 (OUT4 - input)
    MEAS3: C2 - C1 (relative output delay)

No WGEN command is sent. The firmware holds each compare setting for the
capture count plus a guard count, then advances on its own. The guard lets the
scope finish each capture before the board changes to the next setting.
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


HOST = "192.168.50.3"
CONFIG_PATH = Path(__file__).with_name("config_input_reference.json")
FIRMWARE_HEADER_PATH = AUTOMATION_DIR.parents[1] / "Core" / "Inc" / "autonomous_sweep.h"
MEAN_CLOSURE_TOLERANCE_PS = 500.0

# (measurement slot, label, first source, second source).  RTM delay is the
# second-source 50% rising edge minus the first-source 50% rising edge.
MEASUREMENTS = (
    (1, "d1_c1_minus_input", 3, 1),
    (2, "d2_c2_minus_input", 3, 2),
    (3, "delta_c2_minus_c1", 1, 2),
)

# C1/C2 can swap order when the programmed signed delay changes.  The input
# is always earlier than either output, so its two measurements can select the
# first edge of both channels.  For C2-C1, select C1 from the left and choose
# the C2 edge nearest to it, which keeps the same pulse pair for either sign.
MEASUREMENT_DIRECTIONS = {
    1: "FRFI,FRFI",
    2: "FRFI,FRFI",
    3: "FRFI,NEAR",
}


def apply(scope: RTM3004, command: str) -> None:
    scope.scpi.write(command)
    scope.scpi.query("*OPC?")
    try:
        scope.check_errors()
    except ScopeError as error:
        raise ScopeError(f"RTM3004 rejected setup command {command!r}: {error}") from error


def configure(scope: RTM3004) -> None:
    """Configure normal Nx Single statistics; never configure WGEN."""
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
        # Keep C3's probe/impedance state under operator control.  Only set
        # the known working display range used by the trigger.
        "CHAN3:SCAL 1",
        "CHAN3:POS 0",
        "ACQ:POIN:AUT OFF",
        "ACQ:POIN 5000",
        "TIM:POS 0",
        "TIM:SCAL 2e-8",
        # Keep the input edge near the stable diagnostic view already used
        # by the external-input K15 capture.  The expected outputs (about
        # 60--70 ns after C3 at the minimum-delay setting) remain in view.
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
        "FORM REAL,32",
        "FORM:BORD LSBF",
    ]
    for slot, _, first, second in MEASUREMENTS:
        commands.extend(
            (
                f"MEAS{slot}:ENAB ON",
                f"MEAS{slot}:MAIN DEL",
                f"MEAS{slot}:SOUR CH{first},CH{second}",
                f"MEAS{slot}:DEL:SLOP POS,POS",
                # Input-to-output measurements use the first edges in the
                # record. Output-to-output must keep the nearer C2 edge so
                # it remains valid for both positive and negative delays.
                f"MEAS{slot}:DEL:DIR {MEASUREMENT_DIRECTIONS[slot]}",
                f"MEAS{slot}:STAT ON",
                f"MEAS{slot}:TIM:AUTO OFF",
            )
        )
    for command in commands:
        apply(scope, command)

    if not math.isclose(scope.scpi.query_float("TIM:SCAL?"), 2e-8, rel_tol=0.01):
        raise ScopeError("RTM3004 did not retain the requested 20 ns/div view")


def measurement_count(scope: RTM3004, slot: int) -> int:
    return int(float(scope.scpi.query(f"MEAS{slot}:RES:WFMCount?")))


def arm(scope: RTM3004, waveform_count: int) -> None:
    scope.scpi.write("STOP")
    scope.scpi.query("*OPC?")
    for slot, _, _, _ in MEASUREMENTS:
        scope.scpi.write(f"MEAS{slot}:STAT:RES")
    scope.scpi.query("*OPC?")
    for slot, _, _, _ in MEASUREMENTS:
        if measurement_count(scope, slot) != 0:
            raise ScopeError(f"MEAS{slot} statistics did not reset to zero")

    scope.scpi.write(f"ACQ:NSIN:COUN {waveform_count}")
    scope.scpi.query("*OPC?")
    configured_count = int(float(scope.scpi.query("ACQ:NSIN:COUN?")))
    if configured_count != waveform_count:
        raise ScopeError(
            f"Nx Single count mismatch: expected {waveform_count}, read {configured_count}"
        )
    scope.scpi.write("SING")
    deadline = time.monotonic() + 5.0
    last_state = ""
    last_counts = {slot: 0 for slot, *_ in MEASUREMENTS}
    while time.monotonic() < deadline:
        state, waiting = scope._acquisition_status()
        last_state = state
        last_counts = {slot: measurement_count(scope, slot) for slot, *_ in MEASUREMENTS}
        if state.startswith("RUN") or waiting:
            scope.check_errors()
            return
        # With a continuous source, a short Nx Single acquisition can finish
        # before the first SCPI status poll. It is already a valid capture
        # state; wait_for_samples() performs the final valid-sample check.
        if (state.startswith("COMP") or state.startswith("STOP")) and any(
            last_counts.values()
        ):
            scope.check_errors()
            return
        time.sleep(0.02)
    raise ScopeError(
        "RTM3004 did not arm Nx Single statistics acquisition "
        f"(state={last_state!r}, counts={last_counts})"
    )


def wait_for_samples(scope: RTM3004, minimum_valid: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last_count = -1
    while time.monotonic() < deadline:
        state, waiting = scope._acquisition_status()
        count = measurement_count(scope, 1)
        if count != last_count:
            print(f"Progress: {count}/{minimum_valid} valid samples", flush=True)
            last_count = count
        if state.startswith("COMP") or (state.startswith("STOP") and not waiting):
            scope.scpi.query("*OPC?")
            counts = {slot: measurement_count(scope, slot) for slot, *_ in MEASUREMENTS}
            if any(count < minimum_valid for count in counts.values()):
                raise ScopeError(
                    f"Measurement count too small: need at least {minimum_valid}, read {counts}"
                )
            scope.check_errors()
            return
        time.sleep(0.20)
    raise ScopeError(
        f"Timed out waiting for samples: {max(last_count, 0)}/{minimum_valid}; "
        f"state={state!r}, WTRIG={int(waiting)}"
    )


def read_values(scope: RTM3004, slot: int, expected: int) -> list[float]:
    raw = scope.scpi.query(f"MEAS{slot}:STAT:VAL:ALL?")
    values = [
        float(value)
        for value in raw.split(",")
        if value.strip() and math.isfinite(float(value)) and abs(float(value)) < 1e36
    ]
    if len(values) < expected:
        raise ScopeError(
            f"MEAS{slot} returned {len(values)} delay values; need at least {expected}"
        )
    # The first post-reset frame can be a valid trigger but have no valid
    # output-edge pair. Keep the newest complete set. This also caps the
    # 1001-trigger acquisition at RTM3004's 1000-value statistics buffer.
    return values[-expected:]


def value_stats(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)

    def quantile(fraction: float) -> float:
        position = fraction * (len(ordered) - 1)
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return ordered[lower]
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    return {
        "mean_ps": statistics.mean(values) * 1e12,
        "rms_ps": statistics.stdev(values) * 1e12 if len(values) > 1 else 0.0,
        "p99_minus_p1_ps": (quantile(0.99) - quantile(0.01)) * 1e12,
        "min_ps": min(values) * 1e12,
        "max_ps": max(values) * 1e12,
    }


def mean_closure_error_ps(statistics_by_name: dict[str, dict[str, float]]) -> float:
    """Return C2-C1 minus the two input-referenced mean delays in ps."""
    return (
        statistics_by_name["delta_c2_minus_c1"]["mean_ps"]
        - (
            statistics_by_name["d2_c2_minus_input"]["mean_ps"]
            - statistics_by_name["d1_c1_minus_input"]["mean_ps"]
        )
    )


def save_step(
    scope: RTM3004,
    step: SweepStep,
    expected: int,
    step_dir: Path,
) -> dict[str, dict[str, float]]:
    step_dir.mkdir()
    values_by_name = {
        name: read_values(scope, slot, expected)
        for slot, name, _, _ in MEASUREMENTS
    }
    with (step_dir / "delay_samples.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow(["sample", *values_by_name])
        for index in range(expected):
            writer.writerow([index + 1, *[values_by_name[name][index] for name in values_by_name]])

    statistics_by_name = {name: value_stats(values) for name, values in values_by_name.items()}
    closure_error_ps = mean_closure_error_ps(statistics_by_name)
    (step_dir / "summary.json").write_text(
        json.dumps(
            {
                "step": {
                    "index": step.index,
                    "id": step.set_id,
                    "requested_delay_ps": step.delay_ps,
                    "signed_ticks": step.signed_ticks,
                    "actual_delay_ps": step.actual_delay_ps,
                },
                "samples": expected,
                "edge_definition": (
                    "scope delay, rising 50%; input-reference FRFI/FRFI, "
                    "output-pair FRFI/NEAR"
                ),
                "mean_closure_error_ps": closure_error_ps,
                "statistics": statistics_by_name,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return statistics_by_name


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast external-input C1/C2/C3 statistics sweep")
    parser.add_argument("--smoke", action="store_true", help="capture 3 samples at Z00 only")
    parser.add_argument("--timeout", type=float, default=None)
    args = parser.parse_args()

    config = load_config(CONFIG_PATH)
    steps = build_steps(config)
    validate_config(config, steps)
    full_count = int(config["acquisition"]["waveforms_per_step"])
    firmware_pulses = int(config["autonomous"].get("pulses_per_step", full_count))
    guard_pulses = int(config["autonomous"]["transition_guard_pulses"])
    frequency_hz = float(config["generator"]["frequency_hz"])
    if guard_pulses < 1:
        raise ValueError("autonomous.transition_guard_pulses must be positive")
    sample_count = 3 if args.smoke else full_count
    # One warm-up acquisition absorbs a possible post-reset frame without a
    # valid C1/C2 edge. RTM3004 retains only 1000 statistical values, hence
    # the main plan is intentionally capped at 1000 valid samples above.
    trigger_count = sample_count + 1
    steps_to_run = steps[:1] if args.smoke else steps
    if not firmware_header_matches(
        FIRMWARE_HEADER_PATH, steps, firmware_pulses, guard_pulses
    ):
        print("FAILED: config_input_reference.json does not match autonomous_sweep.h")
        return 1
    timeout_s = args.timeout or max(30.0, trigger_count * 1.5 / frequency_hz + 10.0)

    scope: RTM3004 | None = None
    try:
        scope = RTM3004(HOST, timeout_s=20.0)
        print(f"Connected: {scope.identity()}")
        print("Fast statistics mode: no K15 history waveform export and no WGEN command")
        configure(scope)
        print("Scope configured: MEAS1=C1-C3, MEAS2=C2-C3, MEAS3=C2-C1")
        input(
            "Leave the continuous external input ON. Press RESET on the delay "
            "board, then press Enter immediately: "
        )

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_root = AUTOMATION_DIR / "output" / "external_input_fast_statistics" / stamp
        output_root.mkdir(parents=True, exist_ok=False)
        all_rows: list[dict[str, Any]] = []
        for position, step in enumerate(steps_to_run):
            print(f"\n[{position + 1}/{len(steps_to_run)}] {step.prefix}")
            if position > 0:
                # At capture completion the board can still be on the prior
                # step's guard edges. Waiting a full guard interval is safe
                # even if the scope missed a few pulses while it was arming.
                wait_s = guard_pulses / frequency_hz + 0.20
                print(f"Waiting {wait_s:.1f} s for autonomous step transition")
                time.sleep(wait_s)
            arm(scope, trigger_count)
            print(
                f"Scope armed: collecting {sample_count} valid samples from the next "
                f"{trigger_count} continuous input edges"
            )
            wait_for_samples(scope, sample_count, timeout_s)
            step_dir = output_root / step.prefix
            results = save_step(scope, step, sample_count, step_dir)
            all_rows.append(
                {
                    "step": step.prefix,
                    "requested_delay_ps": step.delay_ps,
                    "actual_delay_ps": step.actual_delay_ps,
                    **{
                        f"{name}_{field}": stats[field]
                        for name, stats in results.items()
                        for field in ("mean_ps", "rms_ps", "p99_minus_p1_ps")
                    },
                }
            )
            print(json.dumps(results, ensure_ascii=False, indent=2))
            closure_error_ps = mean_closure_error_ps(results)
            if abs(closure_error_ps) > MEAN_CLOSURE_TOLERANCE_PS:
                raise ScopeError(
                    "Delay measurements do not close: C2-C1 differs from "
                    f"(C2-C3)-(C1-C3) by {closure_error_ps:.1f} ps. "
                    "Do not use this run for jitter; inspect the three waveforms."
                )

        with (output_root / "sweep_summary.csv").open(
            "w", encoding="utf-8-sig", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=list(all_rows[0]))
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"COMPLETED: {output_root}")
        return 0
    except Exception as error:
        print(f"FAILED: {error}")
        return 1
    finally:
        if scope is not None:
            try:
                scope.scpi.write("STOP")
            finally:
                scope.close()


if __name__ == "__main__":
    raise SystemExit(main())
