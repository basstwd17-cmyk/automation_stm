#!/usr/bin/env python3
"""Read the current RTM3004 setup without changing acquisition settings."""

from __future__ import annotations

import argparse
import json
from typing import Any

try:
    from .rtm3004_scpi import DEFAULT_HOST, DEFAULT_PORT, ScpiSocket
except ImportError:
    from rtm3004_scpi import DEFAULT_HOST, DEFAULT_PORT, ScpiSocket


def read_scope(host: str, port: int, timeout: float) -> dict[str, Any]:
    with ScpiSocket(host, port, timeout) as scope:
        return {
            "host": host,
            "port": port,
            "identity": scope.query("*IDN?"),
            "options": scope.query("*OPT?").split(","),
            "channel_1": {
                "enabled": bool(int(scope.query("CHAN1:STAT?"))),
                "scale_v_per_div": float(scope.query("CHAN1:SCAL?")),
                "coupling": scope.query("CHAN1:COUP?"),
            },
            "channel_2": {
                "enabled": bool(int(scope.query("CHAN2:STAT?"))),
                "scale_v_per_div": float(scope.query("CHAN2:SCAL?")),
                "coupling": scope.query("CHAN2:COUP?"),
            },
            "timebase": {
                "scale_s_per_div": float(scope.query("TIM:SCAL?")),
                "position_s": float(scope.query("TIM:POS?")),
            },
            "acquisition": {
                "points": int(scope.query("ACQ:POIN?")),
                "type": scope.query("ACQ:TYPE?"),
                "sample_rate_sa_per_s": float(scope.query("ACQ:SRAT?")),
            },
            "trigger": {
                "mode": scope.query("TRIG:A:MODE?"),
                "source": scope.query("TRIG:A:SOUR?"),
                "type": scope.query("TRIG:A:TYPE?"),
                "channel_1_level_v": float(scope.query("TRIG:A:LEV1?")),
            },
            "system_error": scope.query("SYST:ERR?"),
        }


def print_summary(data: dict[str, Any]) -> None:
    ch1 = data["channel_1"]
    ch2 = data["channel_2"]
    timebase = data["timebase"]
    acquisition = data["acquisition"]
    trigger = data["trigger"]

    print(f"RTM3004  : {data['identity']}")
    print(f"Endpoint : {data['host']}:{data['port']}")
    print(f"Options  : {','.join(data['options'])}")
    print(
        "Channels : "
        f"C1={'ON' if ch1['enabled'] else 'OFF'} "
        f"{ch1['scale_v_per_div'] * 1e3:g} mV/div {ch1['coupling']}, "
        f"C2={'ON' if ch2['enabled'] else 'OFF'} "
        f"{ch2['scale_v_per_div'] * 1e3:g} mV/div {ch2['coupling']}"
    )
    print(
        "Timebase : "
        f"{timebase['scale_s_per_div'] * 1e9:g} ns/div, "
        f"position {timebase['position_s'] * 1e9:g} ns"
    )
    print(
        "Acquire  : "
        f"{acquisition['sample_rate_sa_per_s'] / 1e9:g} GSa/s, "
        f"{acquisition['points']} points, {acquisition['type']}"
    )
    print(
        "Trigger  : "
        f"{trigger['mode']} {trigger['type']} {trigger['source']}, "
        f"C1 level {trigger['channel_1_level_v']:g} V"
    )
    print(f"SCPI     : {data['system_error']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read the current RTM3004 setup over raw SCPI/TCP."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data = read_scope(args.host, args.port, args.timeout)
    if args.as_json:
        print(json.dumps(data, indent=2))
    else:
        print_summary(data)


if __name__ == "__main__":
    main()
