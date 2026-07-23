"""Minimal raw-SCPI driver for the R&S RTM3004 used by this project."""

from __future__ import annotations

import csv
import math
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Callable


class ScopeError(RuntimeError):
    pass


class RunAborted(RuntimeError):
    pass


@dataclass(frozen=True)
class MeasurementStats:
    actual_s: float
    mean_s: float
    stddev_s: float
    minimum_s: float
    maximum_s: float
    waveform_count: int


@dataclass(frozen=True)
class AcquisitionResult:
    waveform_count: int
    completion_reason: str
    warning: str | None = None
    start_mode: str = "software_trigger"


class ScpiSocket:
    def __init__(self, host: str, port: int, timeout_s: float) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self._socket = socket.create_connection((host, port), timeout=timeout_s)
        self._socket.settimeout(timeout_s)
        self._reader = self._socket.makefile("rb")

    def write(self, command: str) -> None:
        self._socket.sendall((command + "\n").encode("ascii"))

    def query(self, command: str) -> str:
        self.write(command)
        response = self._reader.readline()
        if not response:
            raise ScopeError(f"No SCPI response for {command}")
        return response.decode("ascii", errors="replace").strip()

    def query_float(self, command: str) -> float:
        value = float(self.query(command))
        if not math.isfinite(value) or abs(value) > 1e36:
            raise ScopeError(f"Invalid result for {command}: {value}")
        return value

    def query_binary_block(self, command: str) -> bytes:
        self.write(command)
        marker = self._reader.read(1)
        if marker != b"#":
            raise ScopeError(f"Invalid binary block marker for {command}: {marker!r}")
        digits_raw = self._reader.read(1)
        if not digits_raw or not digits_raw.isdigit():
            raise ScopeError(f"Invalid binary block length for {command}")
        digits = int(digits_raw)
        byte_count = int(self._reader.read(digits).decode("ascii"))
        payload = self._reader.read(byte_count)
        if len(payload) != byte_count:
            raise ScopeError(
                f"Incomplete binary block for {command}: {len(payload)}/{byte_count}"
            )
        terminator = self._reader.read(1)
        if terminator not in (b"\n", b"\r"):
            raise ScopeError(f"Invalid binary block terminator for {command}")
        if terminator == b"\r" and self._reader.read(1) != b"\n":
            raise ScopeError(f"Invalid binary block line ending for {command}")
        return payload

    def close(self) -> None:
        try:
            self._reader.close()
        finally:
            self._socket.close()

    def __enter__(self) -> "ScpiSocket":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


class RTM3004:
    _WAITING_FOR_TRIGGER_BIT = 1 << 3

    def __init__(self, host: str, port: int = 5025, timeout_s: float = 5.0) -> None:
        self.scpi = ScpiSocket(host, port, timeout_s)
        self._arm_settle_s = 0.5
        self._output_enable_guard_s = 0.3
        self._generator_period_s = 0.1
        self._waveforms_per_step = 1

    def close(self) -> None:
        self.scpi.close()

    def identity(self) -> str:
        return self.scpi.query("*IDN?")

    def check_errors(self) -> None:
        error = self.scpi.query("SYST:ERR:ALL?")
        if not error.startswith("0,"):
            raise ScopeError(f"RTM3004 SCPI error: {error}")

    def configure(self, config: dict[str, Any]) -> None:
        generator = config["generator"]
        acquisition = config["acquisition"]
        self._arm_settle_s = max(
            0.0, float(acquisition.get("arm_settle_s", 0.5))
        )
        generator_frequency_hz = float(generator["frequency_hz"])
        self._generator_period_s = 1.0 / generator_frequency_hz
        self._waveforms_per_step = int(acquisition["waveforms_per_step"])

        # Cancel any interrupted burst before changing generator settings.
        for command in ("*CLS", "WGEN:OUTP OFF", "WGEN:BURS OFF", "STOP"):
            self.scpi.write(command)
        self.scpi.query("*OPC?")
        self.check_errors()

        commands = [
            "CHAN1:STAT ON",
            "CHAN2:STAT ON",
            "CHAN1:COUP DC",
            "CHAN2:COUP DC",
            f"CHAN1:SCAL {float(acquisition['channel_scale_v_per_div']):.12g}",
            f"CHAN2:SCAL {float(acquisition['channel_scale_v_per_div']):.12g}",
            "CHAN1:OFFS 0",
            "CHAN2:OFFS 0",
            f"CHAN1:POS {float(acquisition['channel_position_div']):.12g}",
            f"CHAN2:POS {float(acquisition['channel_position_div']):.12g}",
            "CHAN1:BAND FULL",
            "CHAN2:BAND FULL",
            "CHAN1:SKEW 0",
            "CHAN2:SKEW 0",
            "CHAN1:TYPE SAMP",
            "CHAN2:TYPE SAMP",
            f"TIM:SCAL {float(acquisition['time_scale_s_per_div']):.12g}",
            f"TIM:POS {float(acquisition['time_position_s']):.12g}",
            f"ACQ:POIN {int(acquisition['record_length'])}",
            "ACQ:SEGM:STAT OFF",
            "TRIG:A:TYPE EDGE",
            "TRIG:A:SOUR CH1",
            "TRIG:A:EDGE:SLOP POS",
            f"TRIG:A:LEV1 {float(acquisition['trigger_level_v']):.12g}",
            "TRIG:A:MODE NORM",
            "REFL:REL:MODE USER",
            "REFL:REL:LOW 10",
            "REFL:REL:MIDD 50",
            "REFL:REL:UPP 90",
            "MEAS1:ENAB ON",
            "MEAS1:MAIN DEL",
            "MEAS1:SOUR CH1,CH2",
            "MEAS1:DEL:SLOP POS,POS",
            "MEAS1:DEL:DIR NEAR,NEAR",
            "MEAS1:STAT ON",
            "MEAS1:TIM:AUTO ON",
            "FORM REAL,32",
            "FORM:BORD LSBF",
            "HCOP:LANG PNG",
            "HCOP:COL:SCH COL",
            "HCOP:CWIN ON",
            "WGEN:FUNC PULS",
            f"WGEN:FREQ {float(generator['frequency_hz']):.12g}",
            f"WGEN:VOLT {float(generator['amplitude_vpp']):.12g}",
            f"WGEN:VOLT:OFFS {float(generator['offset_v']):.12g}",
            f"WGEN:FUNC:PULS:DCYC {float(generator['duty_percent']):.12g}",
            "WGEN:OUTP:LOAD HIGH",
            f"WGEN:BURS:NCYC {self._waveforms_per_step}",
            "WGEN:BURS:PHAS 180",
            "WGEN:BURS:TRIG:MODE SING",
            "WGEN:BURS OFF",
            "WGEN:OUTP OFF",
        ]
        for command in commands:
            self.scpi.write(command)
        self.scpi.query("*OPC?")
        self.check_errors()

    def verify_generator_state(self, output_enabled: bool) -> str:
        burst_enabled = int(float(self.scpi.query("WGEN:BURS?")))
        trigger_mode = self.scpi.query("WGEN:BURS:TRIG:MODE?").upper()
        burst_cycles = int(float(self.scpi.query("WGEN:BURS:NCYC?")))
        output_state = int(float(self.scpi.query("WGEN:OUTP?")))
        self.check_errors()

        expected_output = 1 if output_enabled else 0
        if (
            burst_enabled != 1
            or not trigger_mode.startswith("SING")
            or burst_cycles != self._waveforms_per_step
            or output_state != expected_output
        ):
            raise ScopeError(
                "RTM3004 generator state mismatch: expected "
                f"BURS=1, MODE=SING, NCYC={self._waveforms_per_step}, "
                f"OUTP={expected_output}; read BURS={burst_enabled}, "
                f"MODE={trigger_mode}, NCYC={burst_cycles}, OUTP={output_state}"
            )

        return (
            f"BURS={burst_enabled}, MODE={trigger_mode}, "
            f"NCYC={burst_cycles}, OUTP={output_state}"
        )

    def prepare_generator_before_reset(self) -> str:
        """Configure and verify an idle single-burst state with output off."""
        self.scpi.write("WGEN:OUTP OFF")
        self.scpi.write("WGEN:BURS OFF")
        self.scpi.query("*OPC?")

        self.scpi.write(f"WGEN:BURS:NCYC {self._waveforms_per_step}")
        self.scpi.write("WGEN:BURS:TRIG:MODE SING")
        self.scpi.write("WGEN:BURS ON")
        self.scpi.write("WGEN:BURS:TRIG:MODE SING")
        self.scpi.query("*OPC?")
        return self.verify_generator_state(output_enabled=False)

    def verify_idle_generator_output(self) -> str:
        """Verify that enabled SING output stays idle before a burst."""
        state = self.verify_generator_state(output_enabled=True)
        acquisition_state, waiting_for_trigger = self._acquisition_status()
        if not (
            acquisition_state.startswith("RUN") or waiting_for_trigger
        ):
            raise ScopeError("RTM3004 is not armed for the pre-trigger check")

        deadline = time.monotonic() + self._output_enable_guard_s
        while time.monotonic() < deadline:
            waveform_count = self._waveform_count()
            if waveform_count != 0:
                self.emergency_stop()
                raise ScopeError(
                    "RTM3004 recorded "
                    f"{waveform_count} waveform(s) while verified SING output "
                    "was waiting for the burst trigger"
                )
            time.sleep(0.02)
        return state

    def arm_step(self, waveform_count: int) -> None:
        """Arm an Nx Single acquisition that stops after real trigger events."""
        if waveform_count <= 0:
            raise ValueError("waveform_count must be positive")

        self.scpi.write("STOP")
        self.scpi.query("*OPC?")
        self.scpi.write("MEAS1:STAT:RES")
        self.scpi.query("*OPC?")

        reset_deadline = time.monotonic() + 2.0
        while time.monotonic() < reset_deadline:
            if self._waveform_count() == 0:
                break
            time.sleep(0.02)
        else:
            raise ScopeError("RTM3004 statistics did not reset to zero")

        self.scpi.write(f"ACQ:NSIN:COUN {waveform_count}")
        self.scpi.query("*OPC?")
        configured_count = int(float(self.scpi.query("ACQ:NSIN:COUN?")))
        fast_segmentation = int(float(self.scpi.query("ACQ:SEGM:STAT?")))
        self.check_errors()
        if configured_count != waveform_count:
            raise ScopeError(
                "RTM3004 Nx Single count mismatch: expected "
                f"{waveform_count}, read {configured_count}"
            )
        if fast_segmentation != 0:
            raise ScopeError(
                "RTM3004 Fast Segmentation must be OFF so every Nx Single "
                "waveform contributes to measurement statistics"
            )

        self.scpi.write("SING")
        arm_deadline = time.monotonic() + 5.0
        last_state = ""
        waiting_for_trigger = False
        while time.monotonic() < arm_deadline:
            last_state, waiting_for_trigger = self._acquisition_status()
            if last_state.startswith("RUN") or waiting_for_trigger:
                break
            time.sleep(0.02)
        else:
            raise ScopeError(
                "RTM3004 did not arm the Nx Single acquisition "
                f"(state={last_state!r}, WTRIG={int(waiting_for_trigger)})"
            )
        self.check_errors()

        if self._waveform_count() != 0:
            self.scpi.write("STOP")
            self.scpi.query("*OPC?")
            raise ScopeError("RTM3004 Nx Single acquisition was not armed at zero")

    def trigger_burst(self) -> None:
        self.scpi.write("WGEN:BURS:TRIG:SING")

    def _waveform_count(self) -> int:
        count_raw = float(self.scpi.query("MEAS1:RES:WFMCount?"))
        if not math.isfinite(count_raw) or count_raw > 1e36:
            return 0
        return int(count_raw)

    def _acquisition_status(self) -> tuple[str, bool]:
        state = self.scpi.query("ACQ:STAT?").upper()
        operation_condition = int(
            float(self.scpi.query("STAT:OPER:COND?"))
        )
        waiting_for_trigger = bool(
            operation_condition & self._WAITING_FOR_TRIGGER_BIT
        )
        return state, waiting_for_trigger

    def _stop_acquisition(self) -> None:
        self.scpi.write("STOP")
        self.scpi.query("*OPC?")

    def _wait_for_burst_end(
        self,
        burst_started_at: float,
        waveform_count: int,
        abort_event: Event,
    ) -> None:
        burst_end = (
            burst_started_at
            + waveform_count * self._generator_period_s
            + max(0.2, 2.0 * self._generator_period_s)
        )
        while time.monotonic() < burst_end:
            if abort_event.is_set():
                self.emergency_stop()
                raise RunAborted("Measurement aborted")
            time.sleep(0.05)

    def acquire_waveforms(
        self,
        waveform_count: int,
        timeout_s: float,
        abort_event: Event,
        progress: Callable[[float], None] | None = None,
        enable_output: bool = False,
    ) -> AcquisitionResult:
        if waveform_count <= 0:
            raise ValueError("waveform_count must be positive")

        deadline = time.monotonic() + timeout_s
        if abort_event.is_set():
            self.emergency_stop()
            raise RunAborted("Measurement aborted")
        initial_state, waiting_for_trigger = self._acquisition_status()
        if not (initial_state.startswith("RUN") or waiting_for_trigger):
            raise ScopeError("RTM3004 stopped before the burst")

        if enable_output:
            self.verify_generator_state(output_enabled=False)
            burst_started_at = time.monotonic()
            self.scpi.write("WGEN:OUTP ON")
            time.sleep(0.1)

            start_mode = "output_enable_auto_burst"
            observe_deadline = time.monotonic() + max(
                1.0,
                5.0 * self._generator_period_s,
            )
            while time.monotonic() < observe_deadline:
                if abort_event.is_set():
                    self.emergency_stop()
                    raise RunAborted("Measurement aborted")
                if self._waveform_count() > 0:
                    break
                time.sleep(0.1)
            else:
                self.trigger_burst()
                burst_started_at = time.monotonic()
                start_mode = "software_trigger_after_output_enable"
        else:
            self.trigger_burst()
            burst_started_at = time.monotonic()
            start_mode = "software_trigger"
        last_count = -1
        last_state = "RUN"
        completion_reason = "complete"
        warning: str | None = None
        next_count_query = 0.0
        while time.monotonic() < deadline:
            if abort_event.is_set():
                self.emergency_stop()
                raise RunAborted("Measurement aborted")

            last_state, waiting_for_trigger = self._acquisition_status()
            if last_state.startswith("COMP"):
                break
            if last_state.startswith("BRE"):
                stopped_count = self._waveform_count()
                if stopped_count <= 0:
                    raise ScopeError(
                        "RTM3004 stopped before recording a waveform "
                        f"(state={last_state!r})"
                    )
                completion_reason = "scope_stopped"
                warning = (
                    "RTM3004 stopped the Nx Single acquisition after "
                    f"{stopped_count}/{waveform_count} waveforms"
                )
                self._stop_acquisition()
                self._wait_for_burst_end(
                    burst_started_at,
                    waveform_count,
                    abort_event,
                )
                break

            if last_state.startswith("STOP") and not waiting_for_trigger:
                stopped_count = self._waveform_count()
                if stopped_count >= waveform_count:
                    completion_reason = "complete_stopped"
                    break

            now = time.monotonic()
            if now >= next_count_query:
                count = self._waveform_count()
                if count != last_count:
                    if progress is not None:
                        progress(min(count, waveform_count) / waveform_count)
                    last_count = count
                if count > waveform_count:
                    completion_reason = "overrun"
                    warning = (
                        f"RTM3004 recorded {count}/{waveform_count} waveforms"
                    )
                    self._stop_acquisition()
                    self._wait_for_burst_end(
                        burst_started_at,
                        waveform_count,
                        abort_event,
                    )
                    break
                next_count_query = now + 0.5

            time.sleep(0.2)

        else:
            timed_out_count = self._waveform_count()
            if timed_out_count <= 0:
                self.emergency_stop()
                raise ScopeError(
                    "Timed out waiting for the RTM3004 Nx Single acquisition: "
                    f"0/{waveform_count} waveforms, "
                    f"state={last_state!r}, WTRIG={int(waiting_for_trigger)}"
                )
            completion_reason = "timeout"
            warning = (
                "Timed out after "
                f"{timed_out_count}/{waveform_count} waveforms, "
                f"state={last_state!r}"
            )
            self._stop_acquisition()
            self._wait_for_burst_end(
                burst_started_at,
                waveform_count,
                abort_event,
            )

        self.scpi.query("*OPC?")

        finalize_deadline = time.monotonic() + 3.0
        final_count = self._waveform_count()
        while (
            completion_reason == "complete"
            and final_count < waveform_count
            and time.monotonic() < finalize_deadline
        ):
            if abort_event.is_set():
                self.emergency_stop()
                raise RunAborted("Measurement aborted")
            time.sleep(0.05)
            final_count = self._waveform_count()

        self.check_errors()
        if final_count != waveform_count and warning is None:
            warning = (
                "RTM3004 Nx Single acquisition completed with "
                f"{final_count}/{waveform_count} waveforms"
            )
            completion_reason = "count_mismatch"
        if progress is not None:
            progress(min(final_count, waveform_count) / waveform_count)
        return AcquisitionResult(
            waveform_count=final_count,
            completion_reason=completion_reason,
            warning=warning,
            start_mode=start_mode,
        )

    def read_stats(self) -> MeasurementStats:
        return MeasurementStats(
            actual_s=self.scpi.query_float("MEAS1:RES:ACT?"),
            mean_s=self.scpi.query_float("MEAS1:RES:AVG?"),
            stddev_s=self.scpi.query_float("MEAS1:RES:STDDev?"),
            minimum_s=self.scpi.query_float("MEAS1:RES:NPEak?"),
            maximum_s=self.scpi.query_float("MEAS1:RES:PPEak?"),
            waveform_count=int(float(self.scpi.query("MEAS1:RES:WFMCount?"))),
        )

    def read_stats_values(self) -> list[float]:
        raw = self.scpi.query("MEAS1:STAT:VAL:ALL?")
        values = []
        for raw_value in raw.split(","):
            value = float(raw_value)
            if math.isfinite(value) and abs(value) < 1e36:
                values.append(value)
        return values

    @staticmethod
    def write_stats_values(path: Path, values: list[float]) -> None:
        with path.open("w", encoding="ascii", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["sample", "delay_s"])
            for index, value in enumerate(values, 1):
                writer.writerow([index, value])

    def save_stats_values(self, path: Path, expected_count: int) -> list[float]:
        values = self.read_stats_values()
        if len(values) != expected_count:
            raise ScopeError(
                f"Expected {expected_count} delay samples, received {len(values)}"
            )
        self.write_stats_values(path, values)
        return values

    def save_partial_stats_values(self, path: Path) -> int:
        values = self.read_stats_values()
        if values:
            self.write_stats_values(path, values)
        return len(values)

    def save_screenshot(self, path: Path) -> None:
        payload = self.scpi.query_binary_block("HCOP:DATA?")
        self.check_errors()
        if len(payload) < 24 or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ScopeError("RTM3004 returned invalid PNG screenshot data")
        path.write_bytes(payload)

    def _read_waveform(
        self,
        channel: int,
        point_selection: str,
    ) -> tuple[list[float], list[float]]:
        self.scpi.write(f"CHAN{channel}:DATA:POIN {point_selection}")
        header = [
            float(value)
            for value in self.scpi.query(f"CHAN{channel}:DATA:HEAD?").split(",")
        ]
        payload = self.scpi.query_binary_block(f"CHAN{channel}:DATA?")
        values = [value[0] for value in struct.iter_unpack("<f", payload)]
        expected = int(header[2]) * int(header[3])
        if len(values) != expected:
            raise ScopeError(
                f"CHAN{channel} waveform length mismatch: {len(values)} != {expected}"
            )
        if len(values) < 2:
            times = [header[0]] * len(values)
        else:
            increment = (header[1] - header[0]) / (len(values) - 1)
            times = [header[0] + increment * index for index in range(len(values))]
        return times, values

    def save_waveforms(self, path: Path, point_selection: str) -> None:
        time_1, channel_1 = self._read_waveform(1, point_selection)
        time_2, channel_2 = self._read_waveform(2, point_selection)
        count = min(len(time_1), len(time_2), len(channel_1), len(channel_2))
        with path.open("w", encoding="ascii", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["time_s", "c1_v", "c2_v"])
            for index in range(count):
                writer.writerow([time_1[index], channel_1[index], channel_2[index]])

    def emergency_stop(self) -> None:
        for command in ("WGEN:OUTP OFF", "WGEN:BURS OFF", "STOP"):
            try:
                self.scpi.write(command)
            except OSError:
                pass

    def finish(self) -> None:
        self.scpi.write("WGEN:OUTP OFF")
        self.scpi.write("WGEN:BURS OFF")
        self.scpi.write("STOP")
        self.scpi.query("*OPC?")
        self.check_errors()
