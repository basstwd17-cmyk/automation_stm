"""Verify the RTM3004 input-trigger path before a K15 measurement.

This captures one CH3-triggered history segment from the RTM3004 waveform
generator.  It deliberately does not reset or otherwise control the STM32:
the purpose is to isolate the generator -> IN1/CH3 -> scope trigger chain.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path


AUTOMATION_DIR = Path(__file__).resolve().parents[1]
if str(AUTOMATION_DIR) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_DIR))

from common.plan import load_config
from common.rtm3004 import RTM3004


DEFAULT_CONFIG = Path(__file__).resolve().with_name("config_input_reference.json")


def _status(scope: RTM3004) -> dict[str, object]:
    acquisition_state, waiting_for_trigger = scope._acquisition_status()
    return {
        "acquisition_state": acquisition_state,
        "waiting_for_trigger": waiting_for_trigger,
        "available_segments": scope.available_segments(),
        "trigger_source": scope.scpi.query("TRIG:A:SOUR?"),
        "trigger_slope": scope.scpi.query("TRIG:A:EDGE:SLOP?"),
        "trigger_level_v": float(scope.scpi.query("TRIG:A:LEV1?")),
        "ch3_enabled": int(float(scope.scpi.query("CHAN3:STAT?"))),
        "ch3_scale_v_per_div": float(scope.scpi.query("CHAN3:SCAL?")),
        "generator_burst": int(float(scope.scpi.query("WGEN:BURS?"))),
        "generator_output": int(float(scope.scpi.query("WGEN:OUTP?"))),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--timeout", type=float, default=3.0)
    args = parser.parse_args()

    config = load_config(args.config)
    runtime_config = copy.deepcopy(config)
    runtime_config["acquisition"]["waveforms_per_step"] = 1
    scope_config = runtime_config["scope"]
    scope = RTM3004(
        host=str(scope_config["host"]),
        port=int(scope_config.get("port", 5025)),
        timeout_s=float(scope_config.get("timeout_s", 5.0)),
    )

    try:
        scope.configure(runtime_config, fast_segmentation=True)
        scope.prepare_generator_before_reset()
        scope.arm_segmented(1)
        armed = _status(scope)

        scope.scpi.write("WGEN:OUTP ON")
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            after = _status(scope)
            if after["available_segments"] == 1 or str(after["acquisition_state"]).startswith("COMP"):
                break
            time.sleep(0.05)
        else:
            after = _status(scope)

        scope.check_errors()
        print(json.dumps({"armed": armed, "after_output_enable": after}, indent=2))
        return 0 if after["available_segments"] == 1 else 2
    finally:
        try:
            scope.emergency_stop()
        finally:
            scope.close()


if __name__ == "__main__":
    raise SystemExit(main())
