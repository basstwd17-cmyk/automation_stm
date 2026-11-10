"""Measurement-plan construction and validation shared by all modes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


HRTIM_TICKS_PER_100_PS = 544
HRTIM_PS_DIVISOR = 100_000
HRTIM_MAX_TICKS = 0xFFDF
HRTIM_FIXED_TIMING_TICKS = 128 + 109 + 96


@dataclass(frozen=True)
class SweepStep:
    index: int
    set_id: str
    delay_ps: int
    repeat: int
    prefix: str
    signed_ticks: int
    actual_delay_ps: float


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def signed_ticks_for_ps(delay_ps: int) -> int:
    magnitude = (abs(delay_ps) * HRTIM_TICKS_PER_100_PS + 50_000) // HRTIM_PS_DIVISOR
    if delay_ps < 0:
        return -magnitude
    return magnitude


def actual_delay_ps(signed_ticks: int) -> float:
    return signed_ticks * HRTIM_PS_DIVISOR / HRTIM_TICKS_PER_100_PS


def build_steps(config: dict[str, Any], repeats: int | None = None) -> list[SweepStep]:
    sweep = config["sweep"]
    base_steps = sweep["steps"]
    repeat_count = int(sweep["repeats"] if repeats is None else repeats)
    alternate_order = bool(sweep.get("alternate_order", True))

    if repeat_count < 1:
        raise ValueError("repeats must be at least 1")
    if not base_steps:
        raise ValueError("sweep.steps must not be empty")

    steps: list[SweepStep] = []
    for repeat_index in range(repeat_count):
        ordered = list(base_steps)
        if alternate_order and (repeat_index % 2 == 1):
            ordered.reverse()

        for raw in ordered:
            delay_ps = int(raw["delay_ps"])
            set_id = str(raw["id"])
            ticks = signed_ticks_for_ps(delay_ps)
            suffix = f"_R{repeat_index + 1}" if repeat_count > 1 else ""
            tick_prefix = "T" + f"{abs(ticks):03d}"
            prefix = f"{set_id}_{tick_prefix}{suffix}"
            steps.append(
                SweepStep(
                    index=len(steps),
                    set_id=set_id,
                    delay_ps=delay_ps,
                    repeat=repeat_index + 1,
                    prefix=prefix,
                    signed_ticks=ticks,
                    actual_delay_ps=actual_delay_ps(ticks),
                )
            )

    return steps


def validate_config(config: dict[str, Any], steps: list[SweepStep]) -> None:
    acquisitions = int(config["acquisition"]["waveforms_per_step"])
    frequency_hz = float(config["generator"]["frequency_hz"])
    record_length = int(config["acquisition"]["record_length"])

    if not 1 <= acquisitions <= 1023:
        raise ValueError("waveforms_per_step must be between 1 and 1023")
    if frequency_hz <= 0:
        raise ValueError("generator frequency must be positive")
    if record_length < 5000:
        raise ValueError("RTM3004 record_length must be at least 5000")
    if not steps:
        raise ValueError("measurement plan is empty")
    if len(steps) > 4096:
        raise ValueError("autonomous sweep supports at most 4096 steps")
    for step in steps:
        if abs(step.signed_ticks) + HRTIM_FIXED_TIMING_TICKS > HRTIM_MAX_TICKS:
            raise ValueError(f"{step.prefix}: delay exceeds the HRTIM counter range")


def firmware_plan_signature(
    steps: list[SweepStep],
    pulses_per_step: int,
    transition_guard_pulses: int | None = None,
) -> str:
    payload = {
        "format": 1,
        "pulses_per_step": int(pulses_per_step),
        "delays_ps": [step.delay_ps for step in steps],
    }
    if transition_guard_pulses is not None:
        payload["transition_guard_pulses"] = int(transition_guard_pulses)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def render_firmware_header(
    steps: list[SweepStep],
    pulses_per_step: int,
    transition_guard_pulses: int | None = None,
) -> str:
    signature = firmware_plan_signature(steps, pulses_per_step, transition_guard_pulses)
    values = []
    for index, step in enumerate(steps):
        comma = "," if index + 1 < len(steps) else ""
        values.append(f"  {step.delay_ps}L{comma}  /* step {index} */")

    header = [
            "/* Generated from a tools/automation measurement config. */",
            "#ifndef AUTONOMOUS_SWEEP_H",
            "#define AUTONOMOUS_SWEEP_H",
            "",
            "#include <stdint.h>",
            "",
            f'#define AUTONOMOUS_SWEEP_PLAN_SIGNATURE "{signature}"',
            f"#define AUTONOMOUS_SWEEP_PULSES_PER_STEP {pulses_per_step}U",
        ]
    if transition_guard_pulses is not None:
        header.append(
            f"#define AUTONOMOUS_SWEEP_TRANSITION_GUARD_PULSES {transition_guard_pulses}U"
        )
    header.extend(
        [
            f"#define AUTONOMOUS_SWEEP_STEP_COUNT {len(steps)}U",
            "",
            "static const int32_t autonomous_sweep_delay_ps[",
            "    AUTONOMOUS_SWEEP_STEP_COUNT] =",
            "{",
            *values,
            "};",
            "",
            "#endif /* AUTONOMOUS_SWEEP_H */",
            "",
        ]
    )
    return "\n".join(header)


def firmware_header_matches(
    path: Path,
    steps: list[SweepStep],
    pulses_per_step: int,
    transition_guard_pulses: int | None = None,
) -> bool:
    if not path.exists():
        return False
    return path.read_text(encoding="ascii") == render_firmware_header(
        steps,
        pulses_per_step,
        transition_guard_pulses,
    )


def write_firmware_header(
    path: Path,
    steps: list[SweepStep],
    pulses_per_step: int,
    transition_guard_pulses: int | None = None,
) -> bool:
    content = render_firmware_header(steps, pulses_per_step, transition_guard_pulses)
    if path.exists() and path.read_text(encoding="ascii") == content:
        return False
    path.write_text(content, encoding="ascii")
    return True


def write_manifest(path: Path, config: dict[str, Any], steps: list[SweepStep]) -> None:
    payload = {
        "config": config,
        "steps": [asdict(step) for step in steps],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
