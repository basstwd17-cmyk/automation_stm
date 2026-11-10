"""RTM3004 SCPI driver shared by statistics, K15, and K37 measurements."""

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


@dataclass(frozen=True)
class HistoryWaveform:
    start_s: float
    stop_s: float
    point_count: int
    times_s: list[float]
    values_v: list[float]


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
        self._fast_segmentation = False

    def close(self) -> None:
        self.scpi.close()

    def identity(self) -> str:
        return self.scpi.query("*IDN?")

    def options(self) -> str:
        return self.scpi.query("*OPT?")

    def verify_k15_option(self) -> str:
        options = self.options()
        if "K15" not in options.upper():
            raise ScopeError(
                "RTM3004 K15 option was not reported by *OPT?: "
                f"{options or '<empty>'}"
            )
        return options

    def check_errors(self) -> None:
        error = self.scpi.query("SYST:ERR:ALL?")
        if not error.startswith("0,"):
            raise ScopeError(f"RTM3004 SCPI error: {error}")

    def configure(
        self,
        config: dict[str, Any],
        fast_segmentation: bool = False,
    ) -> None:
        generator = config["generator"]
        acquisition = config["acquisition"]
        self._arm_settle_s = max(
            0.0, float(acquisition.get("arm_settle_s", 0.5))
        )
        generator_frequency_hz = float(generator["frequency_hz"])
        self._generator_period_s = 1.0 / generator_frequency_hz
        self._waveforms_per_step = int(acquisition["waveforms_per_step"])
        self._fast_segmentation = fast_segmentation
        enabled_channels = tuple(
            int(channel)
            for channel in acquisition.get("enabled_channels", (1, 2))
        )
        if not enabled_channels or any(channel not in (1, 2, 3, 4) for channel in enabled_channels):
            raise ValueError("acquisition.enabled_channels must contain scope channels 1-4")
        trigger_channel = int(acquisition.get("trigger_channel", 1))
        if trigger_channel not in enabled_channels:
            raise ValueError("acquisition.trigger_channel must be enabled")
        configured_channels = tuple(
            int(channel)
            for channel in acquisition.get("configured_channels", enabled_channels)
        )
        if (
            not configured_channels
            or any(channel not in enabled_channels for channel in configured_channels)
        ):
            raise ValueError(
                "acquisition.configured_channels must be enabled scope channels"
            )
        vertical_channels = tuple(
            int(channel)
            for channel in acquisition.get("vertical_channels", configured_channels)
        )
        if (
            not vertical_channels
            or any(channel not in enabled_channels for channel in vertical_channels)
        ):
            raise ValueError(
                "acquisition.vertical_channels must be enabled scope channels"
            )

        # An input-reference measurement is only meaningful when the input
        # channel is configured explicitly, rather than inheriting whatever
        # scale/offset happened to be left on the oscilloscope.
        channel_settings = acquisition.get("channel_settings", {})
        if not isinstance(channel_settings, dict):
            raise ValueError("acquisition.channel_settings must be an object")

        def channel_setting(channel: int, name: str, fallback: float) -> float:
            values = channel_settings.get(str(channel), channel_settings.get(channel, {}))
            if not isinstance(values, dict):
                raise ValueError(
                    f"acquisition.channel_settings[{channel}] must be an object"
                )
            return float(values.get(name, fallback))

        # Start from a deterministic horizontal/memory state.  In particular,
        # this clears the RTM3004 condition in which a prior 10 ms/div manual
        # view rejects the required 10 ns/div K15 view with SCPI -222.
        self.scpi.write("*RST")
        # A prior timed-out query can make this firmware answer the immediate
        # post-reset *OPC? with -410 (Query INTERRUPTED).  The reset itself is
        # asynchronous, so wait briefly and clear the queue in the normal
        # setup sequence below instead of issuing another query here.
        time.sleep(2.0)

        # Keep the input quiet while the post-reset setup is applied.
        for command in ("*CLS", "WGEN:OUTP OFF", "WGEN:BURS OFF", "STOP"):
            self.scpi.write(command)
        self.scpi.query("*OPC?")
        self.check_errors()

        acquisition_commands = [
            f"ACQ:POIN {int(acquisition['record_length'])}",
            "ACQ:SEGM:STAT OFF",
        ]
        if fast_segmentation:
            acquisition_commands = [
                "ACQ:SEGM:STAT OFF",
                "ACQ:MEM:MODE DMEM",
                "ACQ:POIN:AUT OFF",
                f"ACQ:POIN {int(acquisition['record_length'])}",
            ]

        commands = [
            *[
                f"CHAN{channel}:STAT {'ON' if channel in enabled_channels else 'OFF'}"
                for channel in (1, 2, 3, 4)
            ],
            *[
                command
                for channel in configured_channels
                for command in (
                    f"CHAN{channel}:COUP DC",
                    f"CHAN{channel}:SCAL {channel_setting(channel, 'scale_v_per_div', float(acquisition['channel_scale_v_per_div'])):.12g}",
                    f"CHAN{channel}:OFFS {channel_setting(channel, 'offset_v', 0.0):.12g}",
                    f"CHAN{channel}:POS {channel_setting(channel, 'position_div', float(acquisition['channel_position_div'])):.12g}",
                    f"CHAN{channel}:BAND FULL",
                    f"CHAN{channel}:SKEW 0",
                    f"CHAN{channel}:TYPE SAMP",
                )
            ],
            # Fast Segmentation's DMEM record configuration must precede a
            # nanosecond-scale timebase request.  In the scope's normal
            # memory mode the same request is out of range.
            *acquisition_commands,
            # The RTM3004 rejects a smaller scale when the currently retained
            # horizontal position lies outside its new record window (for
            # example after the 45 ns diagnostic view).  Re-center first,
            # then set scale and the requested final position.
            "TIM:POS 0",
            f"TIM:SCAL {float(acquisition['time_scale_s_per_div']):.12g}",
            f"TIM:POS {float(acquisition['time_position_s']):.12g}",
            "TRIG:A:TYPE EDGE",
            f"TRIG:A:SOUR CH{trigger_channel}",
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
            # K15 derives all timing values from history waveforms.  Leave
            # the scope's automatic timing/normalization disabled so it
            # cannot alter the operator's stable display setup.
            "MEAS1:TIM:AUTO OFF",
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
        # C3 is the physical input reference.  Some probe types reject
        # coupling/bandwidth writes, so retain those probe-specific settings
        # but always restore the known-good display range used for triggering.
        if trigger_channel not in configured_channels:
            commands[4:4] = [
                f"CHAN{trigger_channel}:SCAL {channel_setting(trigger_channel, 'scale_v_per_div', float(acquisition['channel_scale_v_per_div'])):.12g}",
                f"CHAN{trigger_channel}:POS {channel_setting(trigger_channel, 'position_div', float(acquisition['channel_position_div'])):.12g}",
            ]
        for command in commands:
            self.scpi.write(command)
            self.scpi.query("*OPC?")
            try:
                self.check_errors()
            except ScopeError as error:
                raise ScopeError(
                    f"RTM3004 rejected setup command {command!r}: {error}"
                ) from error
        actual_time_scale_s = self.scpi.query_float("TIM:SCAL?")
        actual_time_position_s = self.scpi.query_float("TIM:POS?")
        expected_time_scale_s = float(acquisition["time_scale_s_per_div"])
        expected_time_position_s = float(acquisition["time_position_s"])
        if not math.isclose(
            actual_time_scale_s,
            expected_time_scale_s,
            rel_tol=0.01,
            abs_tol=1e-15,
        ):
            raise ScopeError(
                "RTM3004 time scale readback mismatch: expected "
                f"{expected_time_scale_s:.6g} s/div, read "
                f"{actual_time_scale_s:.6g} s/div"
            )
        if not math.isclose(
            actual_time_position_s,
            expected_time_position_s,
            rel_tol=0.01,
            abs_tol=1e-15,
        ):
            raise ScopeError(
                "RTM3004 time position readback mismatch: expected "
                f"{expected_time_position_s:.6g} s, read "
                f"{actual_time_position_s:.6g} s"
            )
        if fast_segmentation:
            # Finish all channel, timebase, memory, measurement and generator
            # changes before enabling Fast Segmentation. Reconfiguring those
            # settings while History is active can produce transient
            # -221/-222 errors immediately after a completed smoke run.
            self.scpi.write("ACQ:SEGM:STAT ON")
            self.scpi.query("*OPC?")
            self.check_errors()

    def verify_generator_state(self, output_enabled: bool) -> str:
        burst_enabled = int(float(self.scpi.query("WGEN:BURS?")))
        output_state = int(float(self.scpi.query("WGEN:OUTP?")))
        self.check_errors()

        expected_output = 1 if output_enabled else 0
        if burst_enabled != 0 or output_state != expected_output:
            raise ScopeError(
                "RTM3004 generator state mismatch: expected "
                f"BURS=0 (continuous), OUTP={expected_output}; read "
                f"BURS={burst_enabled}, OUTP={output_state}"
            )

        return f"BURS={burst_enabled} (continuous), OUTP={output_state}"

    def prepare_generator_before_reset(self) -> str:
        """Configure and verify an idle continuous-pulse state with output off."""
        self.scpi.write("WGEN:OUTP OFF")
        self.scpi.write("WGEN:BURS OFF")
        self.scpi.query("*OPC?")
        return self.verify_generator_state(output_enabled=False)

    def stop_continuous_generator(self) -> str:
        """End one measurement interval and create the firmware idle gap."""
        self.scpi.write("WGEN:OUTP OFF")
        self.scpi.write("WGEN:BURS OFF")
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

    def arm_continuous_step(self) -> None:
        """Arm normal continuous acquisition for Spectrum applications.

        K37 updates its FFT averaging while the time-domain acquisition stays
        in RUN state.  A finite WGEN burst still limits the number of real
        input edges; the caller uses the measurement waveform count to detect
        the requested number of captured edges.
        """
        if self._fast_segmentation:
            raise ScopeError("K37 continuous acquisition requires Fast Segmentation OFF")

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

        self.scpi.write("RUN")
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
                "RTM3004 did not arm continuous acquisition "
                f"(state={last_state!r}, WTRIG={int(waiting_for_trigger)})"
            )
        self.check_errors()

        if self._waveform_count() != 0:
            self._stop_acquisition()
            raise ScopeError("RTM3004 continuous acquisition was not armed at zero")

    def available_segments(self) -> int:
        count_raw = float(self.scpi.query("ACQ:AVA?"))
        if not math.isfinite(count_raw) or count_raw > 1e36:
            return 0
        return int(count_raw)

    def segmented_preflight(self, expected_segments: int) -> dict[str, Any]:
        if expected_segments <= 0:
            raise ValueError("expected_segments must be positive")

        memory_mode = self.scpi.query("ACQ:MEM:MODE?").upper()
        record_length = int(float(self.scpi.query("ACQ:POIN?")))
        configured_count = int(float(self.scpi.query("ACQ:NSIN:COUN?")))
        configured_history_count = int(float(self.scpi.query("ACQ:COUN?")))
        fast_segmentation = int(float(self.scpi.query("ACQ:SEGM:STAT?")))
        self.check_errors()

        if not memory_mode.startswith("DMEM"):
            raise ScopeError(
                "RTM3004 segmented memory mode mismatch: "
                f"expected DMEM, read {memory_mode}"
            )
        if fast_segmentation != 1:
            raise ScopeError("RTM3004 Fast Segmentation did not remain enabled")
        if configured_count != expected_segments:
            raise ScopeError(
                "RTM3004 segmented Nx Single count mismatch: expected "
                f"{expected_segments}, read {configured_count}"
            )
        if configured_history_count != expected_segments:
            raise ScopeError(
                "RTM3004 history segment count mismatch: expected "
                f"{expected_segments}, read {configured_history_count}"
            )

        return {
            "memory_mode": memory_mode,
            "record_length": record_length,
            "configured_segments": configured_count,
            "configured_history_segments": configured_history_count,
            "fast_segmentation": bool(fast_segmentation),
        }

    def arm_segmented(self, segment_count: int) -> dict[str, Any]:
        """Arm K15 Fast Segmentation without using live measurement statistics."""
        if segment_count <= 0:
            raise ValueError("segment_count must be positive")
        if not self._fast_segmentation:
            raise ScopeError("RTM3004 was not configured for Fast Segmentation")

        self.scpi.write("STOP")
        self.scpi.query("*OPC?")
        self.scpi.write("ACQ:SEGM:STAT ON")
        self.scpi.write(f"ACQ:COUN {segment_count}")
        self.scpi.write(f"ACQ:NSIN:COUN {segment_count}")
        self.scpi.query("*OPC?")

        preflight = self.segmented_preflight(segment_count)
        preflight["available_before_arm"] = self.available_segments()

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
                "RTM3004 did not arm Fast Segmentation "
                f"(state={last_state!r}, WTRIG={int(waiting_for_trigger)})"
            )
        self.check_errors()
        return preflight

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
        return_when_count_reached: bool = False,
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
                if return_when_count_reached and count == waveform_count:
                    completion_reason = "target_reached_continuous"
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

    def acquire_segments(
        self,
        segment_count: int,
        timeout_s: float,
        abort_event: Event,
        progress: Callable[[float], None] | None = None,
        enable_output: bool = False,
    ) -> AcquisitionResult:
        """Acquire a complete K15 history buffer and fail closed on mismatch."""
        if segment_count <= 0:
            raise ValueError("segment_count must be positive")
        if not self._fast_segmentation:
            raise ScopeError("RTM3004 was not configured for Fast Segmentation")

        if abort_event.is_set():
            self.emergency_stop()
            raise RunAborted("Measurement aborted")
        initial_state, waiting_for_trigger = self._acquisition_status()
        if not (initial_state.startswith("RUN") or waiting_for_trigger):
            raise ScopeError("RTM3004 stopped before the segmented burst")

        available_before = self.available_segments()
        if enable_output:
            self.verify_generator_state(output_enabled=False)
            self.scpi.write("WGEN:BURS OFF")
            self.scpi.write("WGEN:OUTP ON")
            # Do not use *OPC? after enabling a continuous generator.  On
            # this RTM3004 firmware it waits for the never-ending output
            # operation and causes a socket timeout before any segment can
            # be acquired.
            start_mode = "continuous_output_enable"

            observed_start = False
            observe_deadline = time.monotonic() + max(
                1.0,
                5.0 * self._generator_period_s,
            )
            while time.monotonic() < observe_deadline:
                if abort_event.is_set():
                    self.emergency_stop()
                    raise RunAborted("Measurement aborted")
                state, _ = self._acquisition_status()
                available = self.available_segments()
                if state.startswith("COMP") or available != available_before:
                    observed_start = True
                    break
                time.sleep(0.1)
            # With Burst disabled the manual, verified setup begins emitting
            # immediately after OUTP ON.  Do not switch the generator into
            # Single-Burst mode here: this scope does not produce a usable
            # input pulse after that remote-mode transition.
        else:
            self.verify_generator_state(output_enabled=True)
            start_mode = "manual_continuous_input"

        started_at = time.monotonic()
        deadline = started_at + timeout_s
        last_state = "RUN"
        last_available = -1
        while time.monotonic() < deadline:
            if abort_event.is_set():
                self.emergency_stop()
                raise RunAborted("Measurement aborted")

            last_state, waiting_for_trigger = self._acquisition_status()
            available = self.available_segments()
            if available != last_available:
                if progress is not None and 0 <= available <= segment_count:
                    progress(available / segment_count)
                last_available = available

            if last_state.startswith("COMP"):
                break
            if last_state.startswith("BRE"):
                self.emergency_stop()
                raise ScopeError(
                    "RTM3004 interrupted Fast Segmentation after "
                    f"{available}/{segment_count} segments"
                )
            if last_state.startswith("STOP") and not waiting_for_trigger:
                self.emergency_stop()
                raise ScopeError(
                    "RTM3004 stopped Fast Segmentation after "
                    f"{available}/{segment_count} segments"
                )

            if progress is not None and available <= 0:
                elapsed_fraction = (time.monotonic() - started_at) / max(
                    timeout_s,
                    1e-9,
                )
                progress(min(0.95, max(0.0, elapsed_fraction)))
            time.sleep(0.2)
        else:
            available = self.available_segments()
            self.emergency_stop()
            raise ScopeError(
                "Timed out waiting for RTM3004 Fast Segmentation: "
                f"{available}/{segment_count} segments, state={last_state!r}"
            )

        self.scpi.query("*OPC?")
        finalize_deadline = time.monotonic() + max(
            10.0,
            min(60.0, segment_count * 0.05),
        )
        final_count = self.available_segments()
        while final_count != segment_count and time.monotonic() < finalize_deadline:
            if abort_event.is_set():
                self.emergency_stop()
                raise RunAborted("Measurement aborted")
            time.sleep(0.1)
            final_count = self.available_segments()

        self.check_errors()
        if final_count != segment_count:
            self.emergency_stop()
            raise ScopeError(
                "RTM3004 completed Fast Segmentation with "
                f"{final_count}/{segment_count} history segments"
            )
        if progress is not None:
            progress(1.0)
        return AcquisitionResult(
            waveform_count=final_count,
            completion_reason="complete",
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

    def _read_waveform_data(
        self,
        channel: int,
        point_selection: str,
    ) -> tuple[list[float], list[float], list[float]]:
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
        return header, times, values

    def _read_waveform(
        self,
        channel: int,
        point_selection: str,
    ) -> tuple[list[float], list[float]]:
        _, times, values = self._read_waveform_data(channel, point_selection)
        return times, values

    def history_timestamps(self, channel: int) -> list[float]:
        raw = self.scpi.query(f"CHAN{channel}:HIST:TSR:ALL?")
        timestamps = [float(value) for value in raw.split(",") if value.strip()]
        if any(not math.isfinite(value) for value in timestamps):
            raise ScopeError(f"CHAN{channel} returned an invalid history timestamp")
        return timestamps

    def read_history_segment_channels(
        self,
        history_index: int,
        point_selection: str,
        channels: tuple[int, ...],
        *,
        verify_selection: bool = True,
        wait_for_selection: bool = True,
        check_errors: bool = True,
    ) -> dict[int, HistoryWaveform]:
        if history_index > 0:
            raise ValueError("RTM3004 history index must be zero or negative")
        if not channels:
            raise ValueError("at least one history channel is required")
        if any(channel not in (1, 2, 3, 4) for channel in channels):
            raise ValueError("history channels must be in the range 1-4")

        for channel in channels:
            self.scpi.write(f"CHAN{channel}:HIST:CURR {history_index}")
        if verify_selection:
            if wait_for_selection:
                self.scpi.query("*OPC?")
                selected = {
                    channel: int(float(self.scpi.query(f"CHAN{channel}:HIST:CURR?")))
                    for channel in channels
                }
            else:
                # Some RTM3004 states leave *OPC? pending after an earlier
                # interrupted remote operation. Poll the selected history
                # cursor instead; the query itself is a safe ordering point.
                deadline = time.monotonic() + 2.0
                selected = {}
                while time.monotonic() < deadline:
                    selected = {
                        channel: int(float(self.scpi.query(f"CHAN{channel}:HIST:CURR?")))
                        for channel in channels
                    }
                    if all(value == history_index for value in selected.values()):
                        break
                    time.sleep(0.005)
            if any(value != history_index for value in selected.values()):
                raise ScopeError(
                    "RTM3004 history selection mismatch: expected "
                    f"{history_index}, read "
                    + ", ".join(
                        f"C{channel}={value}"
                        for channel, value in selected.items()
                    )
                )

        waveforms = {
            channel: self._read_waveform_data(channel, point_selection)
            for channel in channels
        }
        if any(int(header[3]) != 1 for header, _, _ in waveforms.values()):
            raise ScopeError(
                "RTM3004 history segment must contain one acquisition: "
                + ", ".join(
                    f"C{channel}={int(header[3])}"
                    for channel, (header, _, _) in waveforms.items()
                )
            )
        reference_channel = channels[0]
        reference_header, _, reference_values = waveforms[reference_channel]
        for channel, (header, _, values) in waveforms.items():
            if (
                int(header[2]) != int(reference_header[2])
                or len(values) != len(reference_values)
                or not math.isclose(header[0], reference_header[0], abs_tol=1e-15)
                or not math.isclose(header[1], reference_header[1], abs_tol=1e-15)
            ):
                raise ScopeError(
                    "RTM3004 history axes differ at index "
                    f"{history_index}: C{reference_channel} vs C{channel}"
                )

        if check_errors:
            self.check_errors()
        return {
            channel: HistoryWaveform(
                start_s=header[0],
                stop_s=header[1],
                point_count=len(values),
                times_s=times,
                values_v=values,
            )
            for channel, (header, times, values) in waveforms.items()
        }

    def read_history_segment(
        self,
        history_index: int,
        point_selection: str,
    ) -> tuple[HistoryWaveform, HistoryWaveform]:
        """Compatibility wrapper for the original C1/C2 K15 mode."""
        waveforms = self.read_history_segment_channels(
            history_index,
            point_selection,
            (1, 2),
        )
        return waveforms[1], waveforms[2]

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

    def finish_segmented(self) -> None:
        self.scpi.write("WGEN:OUTP OFF")
        self.scpi.write("WGEN:BURS OFF")
        self.scpi.write("STOP")
        self.scpi.write("ACQ:SEGM:STAT OFF")
        self.scpi.query("*OPC?")
        self.check_errors()
