"""Minimal RTM3004 K15 smoke test using the front-panel Gen output.

This intentionally does not import runner.py, flash firmware, modify the
autonomous sweep, or remotely enable the function-generator output.  It only
proves that three CH3-referenced history segments can be captured while the
operator enables the already configured front-panel Gen output.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from threading import Event


AUTOMATION_DIR = Path(__file__).resolve().parents[1]
if str(AUTOMATION_DIR) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_DIR))

from common.rtm3004 import RTM3004, ScopeError


HOST = "192.168.50.3"
SEGMENTS = 3


def apply(scope: RTM3004, command: str) -> None:
    """Apply one finite setup command and fail at its exact location."""
    scope.scpi.write(command)
    scope.scpi.query("*OPC?")
    try:
        scope.check_errors()
    except ScopeError as error:
        raise ScopeError(f"RTM3004 rejected {command!r}: {error}") from error


def configure(scope: RTM3004) -> None:
    # A reset is required to make 10 ns/div deterministic after a manual
    # 10 ms/div view.  Do not issue *OPC? immediately after *RST: this
    # firmware can report a stale -410 query-interrupted error then.
    scope.scpi.write("*RST")
    time.sleep(2.0)

    commands = [
        "*CLS",
        "STOP",
        "WGEN:OUTP OFF",
        "WGEN:BURS OFF",
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
        # Retain C3's probe-specific coupling; only make its trigger view
        # match the manually verified 1 V/div, zero-position state.
        "CHAN3:SCAL 1",
        "CHAN3:POS 0",
        "ACQ:SEGM:STAT OFF",
        "ACQ:MEM:MODE DMEM",
        "ACQ:POIN:AUT OFF",
        "ACQ:POIN 5000",
        "TIM:POS 0",
        "TIM:SCAL 1e-8",
        "TIM:POS 4.5e-8",
        "TRIG:A:TYPE EDGE",
        "TRIG:A:SOUR CH3",
        "TRIG:A:EDGE:SLOP POS",
        "TRIG:A:LEV1 1.65",
        "TRIG:A:MODE NORM",
        # Configure Gen parameters, but leave physical output control to the
        # front panel after the SCPI connection is released.
        "WGEN:FUNC PULS",
        "WGEN:FREQ 10",
        "WGEN:VOLT 3.3",
        "WGEN:VOLT:OFFS 1.65",
        "WGEN:FUNC:PULS:DCYC 50",
        "WGEN:OUTP:LOAD HIGH",
        "WGEN:BURS OFF",
        "WGEN:OUTP OFF",
        "ACQ:SEGM:STAT ON",
    ]
    for command in commands:
        apply(scope, command)

    if abs(scope.scpi.query_float("TIM:SCAL?") - 1e-8) > 1e-12:
        raise ScopeError("RTM3004 did not retain the requested 10 ns/div scale")


def main() -> int:
    scope = RTM3004(HOST, timeout_s=15.0)
    try:
        print(f"Connected: {scope.identity()}")
        print(f"Options: {scope.verify_k15_option()}")
        configure(scope)
        print("Scope configured: 10 ns/div, CH3 rising trigger at 1.65 V")
        print("Remote session released. Turn front-panel Gen output ON, then RESET the board.")
        scope.close()
        scope = None  # type: ignore[assignment]
        input("When CH3 pulse is visible, press Enter to arm and capture 3 segments: ")

        scope = RTM3004(HOST, timeout_s=15.0)
        output = int(float(scope.scpi.query("WGEN:OUTP?")))
        burst = int(float(scope.scpi.query("WGEN:BURS?")))
        if output != 1 or burst != 0:
            raise ScopeError(
                "Front-panel Gen was not enabled as continuous output: "
                f"BURS={burst}, OUTP={output}"
            )

        preflight = scope.arm_segmented(SEGMENTS)
        print(f"Armed: {preflight}")
        result = scope.acquire_segments(
            segment_count=SEGMENTS,
            timeout_s=12.0,
            abort_event=Event(),
            enable_output=False,
        )
        print(f"CAPTURE OK: {result.waveform_count}/{SEGMENTS} segments")
        return 0
    except Exception as error:
        print(f"CAPTURE FAILED: {error}")
        return 1
    finally:
        if scope is not None:
            try:
                scope.emergency_stop()
            finally:
                scope.close()


if __name__ == "__main__":
    raise SystemExit(main())
