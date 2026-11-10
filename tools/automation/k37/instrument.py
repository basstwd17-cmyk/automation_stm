"""K37 Spectrum, Spectrogram, and Peak List control for the RTM3004."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from threading import Event
from typing import Any

from common.rtm3004 import RTM3004, RunAborted, ScopeError


TRACE_COMMANDS = {
    "average": "AVERAGE",
    "maximum": "MAXIMUM",
    "minimum": "MINIMUM",
    "spectrum": "SPECTRUM",
}


@dataclass(frozen=True)
class SpectrumTrace:
    kind: str
    point_count: int
    x_origin_hz: float
    x_increment_hz: float
    frequencies_hz: list[float]
    levels: list[float]


@dataclass(frozen=True)
class SpectrumPeak:
    rank: int
    frequency_hz: float
    level: float


class K37Spectrum:
    def __init__(self, scope: RTM3004) -> None:
        self.scope = scope
        self.scpi = scope.scpi
        self._enabled = False

    def verify_option(self) -> str:
        options = self.scope.options()
        if "K37" not in options.upper():
            raise ScopeError(
                "RTM3004 K37 option was not reported by *OPT?: "
                f"{options or '<empty>'}"
            )
        return options

    def configure(
        self,
        config: dict[str, Any],
        acquisition_count: int,
    ) -> dict[str, Any]:
        if acquisition_count <= 0:
            raise ValueError("K37 acquisition_count must be positive")
        spectrum = config["spectrum"]
        window = str(spectrum["window"]).upper()
        magnitude_scale = str(spectrum["magnitude_scale"]).upper()
        if window not in {
            "RECTANGULAR",
            "HAMMING",
            "HANNING",
            "BLACKMANHARRIS",
            "FLATTOP",
        }:
            raise ValueError(f"Unsupported K37 window: {window}")
        if magnitude_scale not in {"LINEAR", "DBM", "DBV", "DBUV"}:
            raise ValueError(
                f"Unsupported K37 magnitude scale: {magnitude_scale}"
            )

        for command in (
            "WGEN:OUTP OFF",
            "WGEN:BURS OFF",
            "STOP",
            "SPECTRUM:MARKER:RTABLE:ENABLE OFF",
            "SPECTRUM:MARKER:ENABLE OFF",
            "SPECTRUM:DIAGRAM:SPECTROGRAM:ENABLE OFF",
            "SPECTRUM:STATE OFF",
            "ACQUIRE:SEGMENTED:STATE OFF",
            "ACQUIRE:MEMORY:MODE AUTOMATIC",
        ):
            self.scpi.write(command)
        self.scpi.query("*OPC?")
        self.scope.check_errors()

        self.scpi.write(
            f"SPECTRUM:FREQUENCY:WINDOW:TYPE {window}"
        )
        self.scpi.write(
            f"SPECTRUM:FREQUENCY:MAGNITUDE:SCALE {magnitude_scale}"
        )
        self.scpi.write(
            "SPECTRUM:FREQUENCY:BANDWIDTH:RESOLUTION:AUTO "
            + ("ON" if spectrum.get("rbw_auto", True) else "OFF")
        )
        if not spectrum.get("rbw_auto", True):
            self.scpi.write(
                "SPECTRUM:FREQUENCY:BANDWIDTH:RESOLUTION:VALUE "
                f"{float(spectrum['resolution_bandwidth_hz']):.12g}"
            )

        # The RTM3004 firmware exposes these four traces through their DATA
        # queries without a separate enable step.  Its
        # SPECTRUM:WAVEFORM:<trace>:ENABLE command instead raises -221.
        # Leave the display trace selection untouched and collect the raw
        # Average/Maximum/Minimum/Spectrum data after acquisition.
        self.scpi.write(
            f"SPECTRUM:FREQUENCY:AVERAGE:COUNT {acquisition_count}"
        )
        self.scpi.write("SPECTRUM:STATE ON")
        time.sleep(0.5)

        if spectrum.get("full_span", True):
            self.scpi.write("SPECTRUM:FREQUENCY:FULLSPAN")
        else:
            self.scpi.write(
                f"SPECTRUM:FREQUENCY:START "
                f"{float(spectrum['start_frequency_hz']):.12g}"
            )
            self.scpi.write(
                f"SPECTRUM:FREQUENCY:STOP "
                f"{float(spectrum['stop_frequency_hz']):.12g}"
            )

        spectrogram = spectrum["spectrogram"]
        self.scpi.write(
            "SPECTRUM:DIAGRAM:SPECTROGRAM:ENABLE "
            + ("ON" if spectrogram.get("enabled", True) else "OFF")
        )
        self.scpi.write(
            f"SPECTRUM:SPECTROGRAM:SCALE "
            f"{int(spectrogram.get('lines_per_acquisition', 1))}"
        )
        if spectrogram.get("enabled", True):
            self.scpi.write("SPECTRUM:SPECTROGRAM:RESET")

        peaks = spectrum["peaks"]
        peak_enabled = bool(peaks.get("enabled", True))
        self.scpi.write(
            "SPECTRUM:MARKER:ENABLE " + ("ON" if peak_enabled else "OFF")
        )
        self.scpi.write(
            "SPECTRUM:MARKER:RTABLE:ENABLE "
            + ("ON" if peak_enabled else "OFF")
        )
        if peak_enabled:
            self.scpi.write("SPECTRUM:MARKER:SOURCE AVERAGE")
            self.scpi.write("SPECTRUM:MARKER:SETUP:MMODE LONLY")
            self.scpi.write(
                f"SPECTRUM:MARKER:SETUP:MLEVEL "
                f"{float(peaks['minimum_level']):.12g}"
            )
            self.scpi.write("SPECTRUM:MARKER:RMODE ABSOLUTE")

        time.sleep(0.5)
        self.scope.check_errors()
        self._enabled = True
        return self.readback()

    def readback(self) -> dict[str, Any]:
        readback = {
            "spectrum_enabled": bool(
                int(float(self.scpi.query("SPECTRUM:STATE?")))
            ),
            "mode": self.scpi.query("SPECTRUM:MODE?").upper(),
            "memory_mode": self.scpi.query("ACQUIRE:MEMORY:MODE?").upper(),
            "segmented_memory": bool(
                int(float(self.scpi.query("ACQUIRE:SEGMENTED:STATE?")))
            ),
            "source": self.scpi.query("SPECTRUM:SOURCE?").upper(),
            "window": self.scpi.query(
                "SPECTRUM:FREQUENCY:WINDOW:TYPE?"
            ).upper(),
            "magnitude_scale": self.scpi.query(
                "SPECTRUM:FREQUENCY:MAGNITUDE:SCALE?"
            ).upper(),
            "start_frequency_hz": self.scpi.query_float(
                "SPECTRUM:FREQUENCY:START?"
            ),
            "stop_frequency_hz": self.scpi.query_float(
                "SPECTRUM:FREQUENCY:STOP?"
            ),
            "resolution_bandwidth_hz": self.scpi.query_float(
                "SPECTRUM:FREQUENCY:BANDWIDTH:RESOLUTION:VALUE?"
            ),
            "average_count": int(
                float(
                    self.scpi.query(
                        "SPECTRUM:FREQUENCY:AVERAGE:COUNT?"
                    )
                )
            ),
            "spectrogram_enabled": bool(
                int(
                    float(
                        self.scpi.query(
                            "SPECTRUM:DIAGRAM:SPECTROGRAM:ENABLE?"
                        )
                    )
                )
            ),
            "peak_list_enabled": bool(
                int(
                    float(
                        self.scpi.query(
                            "SPECTRUM:MARKER:RTABLE:ENABLE?"
                        )
                    )
                )
            ),
        }
        self.scope.check_errors()
        if not readback["spectrum_enabled"]:
            raise ScopeError("RTM3004 K37 Spectrum did not remain enabled")
        if readback["segmented_memory"]:
            raise ScopeError("K37 requires Fast Segmentation to be OFF")
        # The scope is set to AUTOMATIC before K37 starts.  Once Spectrum is
        # active, firmware 01.700 reports the internally selected Spectrum
        # memory mode as "SPEC"; this is the expected K37 state.
        if not readback["memory_mode"].startswith(("AUT", "SPEC")):
            raise ScopeError(
                "K37 memory mode must be AUTOMATIC before Spectrum or SPEC "
                "while Spectrum is active, read "
                f"{readback['memory_mode']}"
            )
        return readback

    def prepare_source(
        self,
        source: str,
        acquisition_count: int,
    ) -> dict[str, Any]:
        source = source.upper()
        if source not in {"CH1", "CH2"}:
            raise ValueError(f"K37 source must be CH1 or CH2, received {source}")
        if acquisition_count <= 0:
            raise ValueError("K37 acquisition_count must be positive")

        self.scpi.write(f"SPECTRUM:SOURCE {source}")
        self.scpi.write(
            f"SPECTRUM:FREQUENCY:AVERAGE:COUNT {acquisition_count}"
        )
        self.scpi.write("SPECTRUM:FREQUENCY:RESET")
        self.scpi.write("SPECTRUM:SPECTROGRAM:RESET")
        time.sleep(0.25)

        selected_source = self.scpi.query("SPECTRUM:SOURCE?").upper()
        selected_count = int(
            float(
                self.scpi.query(
                    "SPECTRUM:FREQUENCY:AVERAGE:COUNT?"
                )
            )
        )
        self.scope.check_errors()
        if selected_source != source:
            raise ScopeError(
                f"K37 source mismatch: expected {source}, read {selected_source}"
            )
        if selected_count != acquisition_count:
            raise ScopeError(
                "K37 average count mismatch: expected "
                f"{acquisition_count}, read {selected_count}"
            )
        readback = self.readback()
        readback["requested_source"] = source
        readback["requested_acquisition_count"] = acquisition_count
        return readback

    def wait_average_complete(
        self,
        timeout_s: float,
        abort_event: Event,
    ) -> bool:
        """Wait briefly for K37 averaging and report, rather than gate data.

        RTM3004 firmware 01.700 can keep COMPLETE at zero after a finite
        normal-trigger burst, even though its raw Spectrum waveform is ready
        to read. The caller records this as a quality warning and still saves
        the available spectrum, peaks, time waveform, and screenshot.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if abort_event.is_set():
                raise RunAborted("K37 measurement aborted")
            complete = int(
                float(
                    self.scpi.query(
                        "SPECTRUM:FREQUENCY:AVERAGE:COMPLETE?"
                    )
                )
            )
            if complete == 1:
                self.scope.check_errors()
                return True
            time.sleep(0.1)
        self.scope.check_errors()
        return False

    def read_trace(self, kind: str) -> SpectrumTrace:
        kind_key = kind.lower()
        if kind_key not in TRACE_COMMANDS:
            raise ValueError(f"Unsupported K37 trace: {kind}")
        token = TRACE_COMMANDS[kind_key]
        prefix = f"SPECTRUM:WAVEFORM:{token}:DATA"

        point_count = int(float(self.scpi.query(f"{prefix}:POINTS?")))
        x_origin_hz = self.scpi.query_float(f"{prefix}:XORIGIN?")
        x_increment_hz = self.scpi.query_float(f"{prefix}:XINCREMENT?")
        self.scpi.write("FORMAT ASCII")
        try:
            raw = self.scpi.query(f"{prefix}?")
        finally:
            self.scpi.write("FORMAT REAL,32")
            self.scpi.write("FORMAT:BORDER LSBF")
        levels = [
            float(value)
            for value in raw.split(",")
            if value.strip()
        ]
        if len(levels) != point_count:
            raise ScopeError(
                f"K37 {kind_key} length mismatch: "
                f"{len(levels)} != {point_count}"
            )
        if (
            not math.isfinite(x_origin_hz)
            or not math.isfinite(x_increment_hz)
            or x_increment_hz <= 0.0
            or any(not math.isfinite(value) for value in levels)
        ):
            raise ScopeError(f"K37 {kind_key} returned invalid waveform data")
        frequencies_hz = [
            x_origin_hz + x_increment_hz * index
            for index in range(point_count)
        ]
        self.scope.check_errors()
        return SpectrumTrace(
            kind=kind_key,
            point_count=point_count,
            x_origin_hz=x_origin_hz,
            x_increment_hz=x_increment_hz,
            frequencies_hz=frequencies_hz,
            levels=levels,
        )

    def read_peaks(
        self,
        maximum_results: int,
    ) -> tuple[int, list[SpectrumPeak]]:
        if maximum_results <= 0:
            raise ValueError("maximum_results must be positive")
        detected_count = int(
            float(self.scpi.query("SPECTRUM:MARKER:RCOUNT?"))
        )
        if detected_count <= 0:
            self.scope.check_errors()
            return 0, []

        raw = self.scpi.query("SPECTRUM:MARKER:RESULT1:ALL?")
        values = [
            float(value)
            for value in raw.split(",")
            if value.strip()
        ]
        if len(values) % 2:
            raise ScopeError("K37 peak result did not contain frequency/level pairs")
        all_peaks = [
            SpectrumPeak(
                rank=index // 2 + 1,
                frequency_hz=values[index],
                level=values[index + 1],
            )
            for index in range(0, len(values), 2)
        ]
        if any(
            not math.isfinite(peak.frequency_hz)
            or not math.isfinite(peak.level)
            for peak in all_peaks
        ):
            raise ScopeError("K37 peak list contains a non-finite value")
        self.scope.check_errors()
        return detected_count, all_peaks[:maximum_results]

    def emergency_finish(self) -> None:
        for command in (
            "SPECTRUM:MARKER:RTABLE:ENABLE OFF",
            "SPECTRUM:MARKER:ENABLE OFF",
            "SPECTRUM:DIAGRAM:SPECTROGRAM:ENABLE OFF",
            "SPECTRUM:STATE OFF",
        ):
            try:
                self.scpi.write(command)
            except OSError:
                pass

    def finish(self) -> None:
        self.scpi.write("SPECTRUM:MARKER:RTABLE:ENABLE OFF")
        self.scpi.write("SPECTRUM:MARKER:ENABLE OFF")
        self.scpi.write("SPECTRUM:DIAGRAM:SPECTROGRAM:ENABLE OFF")
        self.scpi.write("SPECTRUM:STATE OFF")
        self.scpi.write("FORMAT REAL,32")
        self.scpi.write("FORMAT:BORDER LSBF")
        self.scpi.query("*OPC?")
        self.scope.check_errors()
        self._enabled = False
