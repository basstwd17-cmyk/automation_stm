"""Generate a selected measurement plan without accessing hardware."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from common.plan import build_steps, firmware_plan_signature, load_config, validate_config, write_firmware_header


def prepare(config_path: Path, output: Path) -> dict[str, object]:
    config = load_config(config_path)
    steps = build_steps(config)
    validate_config(config, steps)
    autonomous = config.get("autonomous", {})
    mode = autonomous.get("transition_mode", "idle_gap")
    if mode not in ("idle_gap", "rising_edge_count"):
        raise ValueError(f"Unsupported transition_mode: {mode}")
    pulses = int(config["acquisition"]["waveforms_per_step"])
    guard = None
    if mode == "rising_edge_count":
        pulses = int(autonomous.get("pulses_per_step", pulses))
        guard = int(autonomous.get("transition_guard_pulses", 30))
        if pulses < int(config["acquisition"]["waveforms_per_step"]) or guard <= 0:
            raise ValueError("Continuous plans need enough capture pulses and a positive guard")
    output.parent.mkdir(parents=True, exist_ok=True)
    changed = write_firmware_header(output, steps, pulses, guard)
    return {
        "config": str(config_path), "output": str(output), "changed": changed,
        "transition_mode": mode, "steps": len(steps), "pulses_per_step": pulses,
        "transition_guard_pulses": guard,
        "signature": firmware_plan_signature(steps, pulses, guard),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2] / "Core/Inc/autonomous_sweep.h")
    args = parser.parse_args()
    print(json.dumps(prepare(args.config, args.output), indent=2))


if __name__ == "__main__":
    main()
