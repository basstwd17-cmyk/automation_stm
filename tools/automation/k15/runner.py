"""Fail-closed K15 segmented-history acquisition for the RTM3004."""

from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import math
import statistics
import sys
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

AUTOMATION_DIR = Path(__file__).resolve().parents[1]
if str(AUTOMATION_DIR) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_DIR))

from common.plan import (
    SweepStep,
    build_steps,
    firmware_plan_signature,
    load_config,
    validate_config,
    write_firmware_header,
    write_manifest,
)
from common.rtm3004 import HistoryWaveform, RTM3004, RunAborted, ScopeError
from common.statistics_runner import (
    FIRMWARE_HEADER_PATH,
    PROJECT_ROOT,
    AutomationRunner,
    RunOptions,
    _console_sink,
)


K15_DIR = Path(__file__).resolve().parent
DEFAULT_K15_CONFIG_PATH = K15_DIR / "config.json"
K15_SMOKE_MARKER = PROJECT_ROOT / "Debug" / "k15_smoke_verified.json"


@dataclass(frozen=True)
class EdgeMetrics:
    low_v: float
    high_v: float
    amplitude_v: float
    relative_50_time_s: float
    fixed_time_s: float
    rise_time_s: float
    overshoot_v: float


def _quantile(values: list[float], fraction: float) -> float:
    if not values:
        raise ValueError("quantile requires at least one value")
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("quantile fraction must be between zero and one")
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _rising_crossing(
    times_s: list[float],
    values_v: list[float],
    level_v: float,
    before_index: int,
) -> float:
    crossings: list[float] = []
    search_limit = min(before_index, len(values_v) - 1)
    for index in range(search_limit):
        value_0 = values_v[index]
        value_1 = values_v[index + 1]
        if value_1 <= value_0 or not (value_0 <= level_v <= value_1):
            continue
        fraction = (level_v - value_0) / (value_1 - value_0)
        crossing = times_s[index] + fraction * (
            times_s[index + 1] - times_s[index]
        )
        crossings.append(crossing)
    if not crossings:
        raise ScopeError(f"No rising crossing found at {level_v:.6g} V")
    return crossings[-1]


def _pulse_core(values_v: list[float], threshold_v: float) -> tuple[int, int]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, value_v in enumerate(values_v):
        if value_v >= threshold_v:
            if start is None:
                start = index
        elif start is not None:
            runs.append((start, index - 1))
            start = None
    if start is not None:
        runs.append((start, len(values_v) - 1))
    if not runs:
        raise ScopeError("No pulse samples were found above the 50% seed level")

    pulse_start, pulse_stop = max(
        runs,
        key=lambda run: (
            run[1] - run[0] + 1,
            max(values_v[run[0] : run[1] + 1]),
        ),
    )
    pulse_points = pulse_stop - pulse_start + 1
    if pulse_points < 4:
        raise ScopeError(
            f"History waveform has no contiguous pulse: {pulse_points} points"
        )
    return pulse_start, pulse_stop


def _edge_metrics(
    waveform: HistoryWaveform,
    fixed_threshold_v: float,
) -> EdgeMetrics:
    low_v = _quantile(waveform.values_v, 0.50)
    peak_v = max(waveform.values_v)
    seed_amplitude_v = peak_v - low_v
    if seed_amplitude_v <= 0.5:
        raise ScopeError(
            f"History waveform peak amplitude is too small: "
            f"{seed_amplitude_v:.6g} V"
        )

    pulse_start, pulse_stop = _pulse_core(
        waveform.values_v,
        low_v + 0.50 * seed_amplitude_v,
    )
    pulse_values = waveform.values_v[pulse_start : pulse_stop + 1]
    high_v = statistics.median(pulse_values)
    amplitude_v = high_v - low_v
    if amplitude_v <= 0.5:
        raise ScopeError(
            f"History waveform amplitude is too small: {amplitude_v:.6g} V"
        )
    peak_index = max(
        range(pulse_start, pulse_stop + 1),
        key=waveform.values_v.__getitem__,
    )

    level_10 = low_v + 0.10 * amplitude_v
    level_50 = low_v + 0.50 * amplitude_v
    level_90 = low_v + 0.90 * amplitude_v
    time_10 = _rising_crossing(
        waveform.times_s,
        waveform.values_v,
        level_10,
        peak_index,
    )
    time_50 = _rising_crossing(
        waveform.times_s,
        waveform.values_v,
        level_50,
        peak_index,
    )
    time_90 = _rising_crossing(
        waveform.times_s,
        waveform.values_v,
        level_90,
        peak_index,
    )
    fixed_time = _rising_crossing(
        waveform.times_s,
        waveform.values_v,
        fixed_threshold_v,
        peak_index,
    )
    rise_time = time_90 - time_10
    if rise_time <= 0:
        raise ScopeError(f"Invalid 10-90% rise time: {rise_time:.6g} s")
    return EdgeMetrics(
        low_v=low_v,
        high_v=high_v,
        amplitude_v=amplitude_v,
        relative_50_time_s=time_50,
        fixed_time_s=fixed_time,
        rise_time_s=rise_time,
        overshoot_v=peak_v - high_v,
    )


def _sample_stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {
            "mean_s": None,
            "stddev_s": None,
            "minimum_s": None,
            "maximum_s": None,
        }
    return {
        "mean_s": statistics.mean(values),
        "stddev_s": statistics.stdev(values) if len(values) >= 2 else None,
        "minimum_s": min(values),
        "maximum_s": max(values),
    }


class K15Runner(AutomationRunner):
    def __init__(
        self,
        options: RunOptions,
        event_sink: Any,
        config_path: Path,
        smoke: bool,
    ) -> None:
        super().__init__(options, event_sink)
        self.config_path = config_path
        self.smoke = smoke

    @staticmethod
    def _segment_header() -> list[str]:
        channel_fields = [
            "low_v",
            "high_v",
            "amplitude_v",
            "relative_50_time_s",
            "fixed_time_s",
            "rise_time_s",
            "overshoot_v",
        ]
        return [
            "segment_index",
            "relative_timestamp_s",
            "startup_segment",
            *[f"c1_{field}" for field in channel_fields],
            *[f"c2_{field}" for field in channel_fields],
            *[f"c3_{field}" for field in channel_fields],
            "delay_relative_50_s",
            "delay_fixed_threshold_s",
            "c1_minus_c3_relative_50_s",
            "c2_minus_c3_relative_50_s",
            "c1_minus_c3_fixed_threshold_s",
            "c2_minus_c3_fixed_threshold_s",
            "relative_50_closure_s",
            "fixed_threshold_closure_s",
            "analysis_status",
            "analysis_note",
        ]

    @staticmethod
    def _segment_row(
        segment_index: int,
        timestamp_s: float,
        startup_segment: bool,
        channel_1: EdgeMetrics | None,
        channel_2: EdgeMetrics | None,
        channel_3: EdgeMetrics | None,
        analysis_status: str,
        analysis_note: str,
    ) -> list[Any]:
        if channel_1 is None or channel_2 is None:
            channel_1_values: list[Any] = [""] * 7
            channel_2_values: list[Any] = [""] * 7
            delay_values: list[Any] = [""] * 8
        else:
            channel_1_values = list(asdict(channel_1).values())
            channel_2_values = list(asdict(channel_2).values())
            delay_values = [
                channel_2.relative_50_time_s - channel_1.relative_50_time_s,
                channel_2.fixed_time_s - channel_1.fixed_time_s,
            ]
            if channel_3 is None:
                delay_values.extend([""] * 6)
            else:
                c1_c3_relative = (
                    channel_1.relative_50_time_s
                    - channel_3.relative_50_time_s
                )
                c2_c3_relative = (
                    channel_2.relative_50_time_s
                    - channel_3.relative_50_time_s
                )
                c1_c3_fixed = channel_1.fixed_time_s - channel_3.fixed_time_s
                c2_c3_fixed = channel_2.fixed_time_s - channel_3.fixed_time_s
                delay_values.extend(
                    [
                        c1_c3_relative,
                        c2_c3_relative,
                        c1_c3_fixed,
                        c2_c3_fixed,
                        c2_c3_relative
                        - c1_c3_relative
                        - (channel_2.relative_50_time_s - channel_1.relative_50_time_s),
                        c2_c3_fixed
                        - c1_c3_fixed
                        - (channel_2.fixed_time_s - channel_1.fixed_time_s),
                    ]
                )
        channel_3_values = (
            [""] * 7 if channel_3 is None else list(asdict(channel_3).values())
        )
        return [
            segment_index,
            timestamp_s,
            int(startup_segment),
            *channel_1_values,
            *channel_2_values,
            *channel_3_values,
            *delay_values,
            analysis_status,
            analysis_note,
        ]

    @staticmethod
    def _write_waveform_to_zip(
        archive: zipfile.ZipFile,
        segment_index: int,
        channel_1: HistoryWaveform,
        channel_2: HistoryWaveform,
        channel_3: HistoryWaveform | None,
    ) -> None:
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(["time_s", "c1_v", "c2_v", "c3_v"])
        for index, (time_s, c1_v, c2_v) in enumerate(zip(
            channel_1.times_s,
            channel_1.values_v,
            channel_2.values_v,
        )):
            writer.writerow(
                [
                    time_s,
                    c1_v,
                    c2_v,
                    "" if channel_3 is None else channel_3.values_v[index],
                ]
            )
        archive.writestr(
            f"segment_{segment_index:04d}.csv",
            stream.getvalue(),
        )

    @staticmethod
    def _summary_header() -> list[str]:
        return [
            "index",
            "set_id",
            "requested_delay_ps",
            "signed_ticks",
            "actual_delay_ps",
            "planned_segments",
            "captured_segments",
            "startup_segments",
            "analysis_segments",
            "relative_50_mean_ps",
            "relative_50_stddev_ps",
            "relative_50_min_ps",
            "relative_50_max_ps",
            "fixed_threshold_mean_ps",
            "fixed_threshold_stddev_ps",
            "c1_minus_c3_relative_50_mean_ps",
            "c1_minus_c3_relative_50_stddev_ps",
            "c2_minus_c3_relative_50_mean_ps",
            "c2_minus_c3_relative_50_stddev_ps",
            "relative_50_closure_stddev_ps",
            "c1_rise_time_mean_ps",
            "c2_rise_time_mean_ps",
            "c3_rise_time_mean_ps",
            "quality_status",
            "quality_notes",
            "prefix",
        ]

    @staticmethod
    def _ps(value: float | None) -> float | str:
        return "" if value is None else value * 1e12

    def _export_history(
        self,
        scope: RTM3004,
        step: SweepStep,
        step_dir: Path,
        segment_count: int,
        startup_segments: int,
        fixed_threshold_v: float,
        point_selection: str,
        save_all_waveforms: bool,
        timestamp_tolerance_s: float,
        capture_channels: tuple[int, ...],
        input_reference: dict[str, Any],
        step_progress_base: float,
        step_progress_span: float,
    ) -> tuple[dict[str, float | None], dict[str, float | None], list[str]]:
        timestamps = {
            channel: scope.history_timestamps(channel)
            for channel in capture_channels
        }
        if any(len(values) != segment_count for values in timestamps.values()):
            raise ScopeError(
                f"{step.prefix}: expected {segment_count} timestamps, "
                "received "
                + ", ".join(
                    f"C{channel}={len(values)}"
                    for channel, values in timestamps.items()
                )
            )
        timestamp_delta = max(
            abs(value - timestamps[1][index])
            for channel, values in timestamps.items()
            if channel != 1
            for index, value in enumerate(values)
        )
        if timestamp_delta > timestamp_tolerance_s:
            raise ScopeError(
                f"{step.prefix}: C1/C2 history timestamp mismatch "
                f"{timestamp_delta:.6g} s"
            )

        segment_csv = step_dir / f"{step.prefix}_segments.csv"
        waveform_zip_path = step_dir / f"{step.prefix}_waveforms.zip"
        relative_delays: list[float] = []
        fixed_delays: list[float] = []
        c1_c3_relative_delays: list[float] = []
        c2_c3_relative_delays: list[float] = []
        relative_closures: list[float] = []
        c1_rise_times: list[float] = []
        c2_rise_times: list[float] = []
        c3_rise_times: list[float] = []
        quality_warnings: list[str] = []

        archive: zipfile.ZipFile | None = None
        if save_all_waveforms:
            archive = zipfile.ZipFile(
                waveform_zip_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            )
        try:
            with segment_csv.open(
                "w",
                encoding="utf-8-sig",
                newline="",
            ) as segment_stream:
                writer = csv.writer(segment_stream)
                writer.writerow(self._segment_header())
                for segment_index in range(1, segment_count + 1):
                    self._check_abort()
                    history_index = segment_index - segment_count
                    waveforms = scope.read_history_segment_channels(
                        history_index,
                        point_selection,
                        capture_channels,
                    )
                    channel_1 = waveforms[1]
                    channel_2 = waveforms[2]
                    channel_3 = waveforms.get(3)
                    # A diagnostic run must preserve the raw waveform even
                    # when edge extraction rejects it.  In particular, a
                    # flat C1/C2 capture is the evidence needed to separate
                    # an STM32-output issue from an analysis issue.
                    if archive is not None:
                        self._write_waveform_to_zip(
                            archive,
                            segment_index,
                            channel_1,
                            channel_2,
                            channel_3,
                        )
                    startup_segment = segment_index <= startup_segments
                    metrics_1: EdgeMetrics | None = None
                    metrics_2: EdgeMetrics | None = None
                    metrics_3: EdgeMetrics | None = None
                    analysis_status = "startup_skipped"
                    analysis_note = "excluded from edge statistics by configuration"
                    if not startup_segment:
                        metrics_1 = _edge_metrics(channel_1, fixed_threshold_v)
                        metrics_2 = _edge_metrics(channel_2, fixed_threshold_v)
                        if channel_3 is not None:
                            metrics_3 = _edge_metrics(
                                channel_3,
                                fixed_threshold_v,
                            )
                            if bool(input_reference.get("required", False)):
                                minimum_amplitude_v = float(
                                    input_reference.get("minimum_amplitude_v", 0.0)
                                )
                                maximum_rise_time_s = float(
                                    input_reference.get("maximum_rise_time_s", math.inf)
                                )
                                if metrics_3.amplitude_v < minimum_amplitude_v:
                                    raise ScopeError(
                                        f"{step.prefix} segment {segment_index}: "
                                        f"C3 amplitude {metrics_3.amplitude_v:.3f} V "
                                        f"is below {minimum_amplitude_v:.3f} V"
                                    )
                                if metrics_3.rise_time_s > maximum_rise_time_s:
                                    raise ScopeError(
                                        f"{step.prefix} segment {segment_index}: "
                                        f"C3 10-90% rise time "
                                        f"{metrics_3.rise_time_s * 1e9:.3f} ns exceeds "
                                        f"{maximum_rise_time_s * 1e9:.3f} ns"
                                    )
                        analysis_status = "ok"
                        analysis_note = ""
                    writer.writerow(
                        self._segment_row(
                            segment_index,
                            timestamps[1][segment_index - 1],
                            startup_segment,
                            metrics_1,
                            metrics_2,
                            metrics_3,
                            analysis_status,
                            analysis_note,
                        )
                    )
                    if metrics_1 is not None and metrics_2 is not None:
                        relative_delays.append(
                            metrics_2.relative_50_time_s
                            - metrics_1.relative_50_time_s
                        )
                        fixed_delays.append(
                            metrics_2.fixed_time_s - metrics_1.fixed_time_s
                        )
                        c1_rise_times.append(metrics_1.rise_time_s)
                        c2_rise_times.append(metrics_2.rise_time_s)
                        if metrics_3 is not None:
                            c1_c3_relative = (
                                metrics_1.relative_50_time_s
                                - metrics_3.relative_50_time_s
                            )
                            c2_c3_relative = (
                                metrics_2.relative_50_time_s
                                - metrics_3.relative_50_time_s
                            )
                            c1_c3_relative_delays.append(c1_c3_relative)
                            c2_c3_relative_delays.append(c2_c3_relative)
                            relative_closures.append(
                                c2_c3_relative
                                - c1_c3_relative
                                - (
                                    metrics_2.relative_50_time_s
                                    - metrics_1.relative_50_time_s
                                )
                            )
                            c3_rise_times.append(metrics_3.rise_time_s)

                    if segment_index == 1:
                        metadata = {
                            "point_selection": point_selection,
                            "point_count": channel_1.point_count,
                            "start_s": channel_1.start_s,
                            "stop_s": channel_1.stop_s,
                            "fixed_threshold_v": fixed_threshold_v,
                            "timestamp_tolerance_s": timestamp_tolerance_s,
                            "maximum_c1_c2_timestamp_delta_s": timestamp_delta,
                            "capture_channels": list(capture_channels),
                            "input_reference_quality": input_reference,
                        }
                        (
                            step_dir / f"{step.prefix}_waveform_metadata.json"
                        ).write_text(
                            json.dumps(metadata, indent=2),
                            encoding="ascii",
                        )
                    if segment_index % 25 == 0:
                        scope.check_errors()
                    self.emit(
                        "progress",
                        value=step_progress_base
                        + step_progress_span * segment_index / segment_count,
                    )
        finally:
            if archive is not None:
                archive.close()

        expected_analysis_count = max(0, segment_count - startup_segments)
        if len(relative_delays) != expected_analysis_count:
            quality_warnings.append(
                f"{step.prefix}: expected {expected_analysis_count} analysis "
                f"segments, calculated {len(relative_delays)}"
            )
        if not relative_delays:
            quality_warnings.append(
                f"{step.prefix}: no non-startup segments are available for statistics"
            )

        relative_stats = _sample_stats(relative_delays)
        fixed_stats = _sample_stats(fixed_delays)
        c1_c3_relative_stats = _sample_stats(c1_c3_relative_delays)
        c2_c3_relative_stats = _sample_stats(c2_c3_relative_delays)
        relative_closure_stats = _sample_stats(relative_closures)
        relative_stats["c1_rise_time_mean_s"] = (
            statistics.mean(c1_rise_times) if c1_rise_times else None
        )
        relative_stats["c2_rise_time_mean_s"] = (
            statistics.mean(c2_rise_times) if c2_rise_times else None
        )
        relative_stats["c3_rise_time_mean_s"] = (
            statistics.mean(c3_rise_times) if c3_rise_times else None
        )
        relative_stats["c1_minus_c3_relative_50_mean_s"] = (
            c1_c3_relative_stats["mean_s"]
        )
        relative_stats["c1_minus_c3_relative_50_stddev_s"] = (
            c1_c3_relative_stats["stddev_s"]
        )
        relative_stats["c2_minus_c3_relative_50_mean_s"] = (
            c2_c3_relative_stats["mean_s"]
        )
        relative_stats["c2_minus_c3_relative_50_stddev_s"] = (
            c2_c3_relative_stats["stddev_s"]
        )
        relative_stats["relative_50_closure_stddev_s"] = (
            relative_closure_stats["stddev_s"]
        )
        return relative_stats, fixed_stats, quality_warnings

    def run(self) -> str:
        scope: RTM3004 | None = None
        output_dir: Path | None = None
        status = "completed"
        error_text: str | None = None
        identity = ""
        options_text = ""
        signature = ""
        smoke_marker: dict[str, Any] | None = None
        try:
            config = copy.deepcopy(load_config(self.config_path))
            manual_generator = (
                str(config.get("generator", {}).get("control", "remote"))
                == "manual"
            )
            if config.get("measurement_mode") != "segmented_history":
                raise ValueError(
                    "K15 config measurement_mode must be segmented_history"
                )
            config["scope"]["host"] = self.options.scope_host
            configured_repeats = int(config["sweep"].get("repeats", 1))
            if configured_repeats != 1:
                raise ValueError("K15 segmented config must use one firmware repeat")
            steps = build_steps(config, configured_repeats)
            validate_config(config, steps)
            if config.get("autonomous", {}).get("transition_mode") == "rising_edge_count":
                raise ValueError(
                    "Continuous-input plans use prepare_plan.py and "
                    "k15/external_input_k15.py, not the burst K15 runner."
                )
            firmware_segments = int(config["acquisition"]["waveforms_per_step"])
            signature = firmware_plan_signature(steps, firmware_segments)
            config.setdefault("autonomous", {})["plan_signature"] = signature
            config["autonomous"]["compiled_step_count"] = len(steps)

            k15_config = config["k15"]
            capture_channels = tuple(
                int(channel)
                for channel in k15_config.get("capture_channels", (1, 2))
            )
            if 1 not in capture_channels or 2 not in capture_channels:
                raise ValueError("K15 capture_channels must include C1 and C2")
            if len(set(capture_channels)) != len(capture_channels):
                raise ValueError("K15 capture_channels must not contain duplicates")
            configured_channels = tuple(
                int(channel)
                for channel in config["acquisition"].get(
                    "enabled_channels",
                    (1, 2),
                )
            )
            if capture_channels != configured_channels:
                raise ValueError(
                    "k15.capture_channels must match acquisition.enabled_channels"
                )
            smoke_segments = int(k15_config["smoke_segments"])
            segment_count = smoke_segments if self.smoke else firmware_segments
            if not 1 <= smoke_segments <= firmware_segments:
                raise ValueError(
                    "k15.smoke_segments must be between 1 and waveforms_per_step"
                )
            startup_segments = int(k15_config.get("startup_segments", 2))
            if not 0 <= startup_segments < segment_count:
                raise ValueError(
                    "k15.startup_segments must be smaller than capture segments"
                )
            steps_to_run = steps[:1] if self.smoke else steps
            self.emit("plan", steps=steps_to_run)

            if self.options.dry_run:
                self._simulate(steps_to_run)
                return "completed"

            if self.smoke and K15_SMOKE_MARKER.exists():
                K15_SMOKE_MARKER.unlink()

            if self.options.build_firmware and not self.options.flash_firmware:
                raise RuntimeError(
                    "K15 Build changes the board plan, so DFU flash is also required."
                )
            if self.options.build_firmware:
                changed = write_firmware_header(
                    FIRMWARE_HEADER_PATH,
                    steps,
                    firmware_segments,
                )
                self.emit(
                    "log",
                    text=(
                        "Updated K15 firmware plan"
                        if changed
                        else "K15 firmware plan is unchanged"
                    ),
                )
                self._build()
                self._record_firmware_plan(
                    signature,
                    len(steps),
                    firmware_segments,
                )
            else:
                self._validate_firmware_plan(steps, firmware_segments)

            if not self.smoke:
                if not K15_SMOKE_MARKER.exists():
                    raise RuntimeError(
                        "No successful K15 smoke test was found. Run smoke first."
                    )
                smoke_marker = json.loads(
                    K15_SMOKE_MARKER.read_text(encoding="utf-8")
                )
                if smoke_marker.get("plan_signature") != signature:
                    raise RuntimeError(
                        "K15 smoke test belongs to a different firmware plan."
                    )
                if smoke_marker.get("capture_channels") != list(capture_channels):
                    raise RuntimeError(
                        "K15 smoke test uses different captured scope channels."
                    )

            self.emit("stage", text="Connecting to RTM3004 for K15")
            scope = RTM3004(
                host=config["scope"]["host"],
                port=int(config["scope"]["port"]),
                timeout_s=float(config["scope"]["timeout_s"]),
            )
            identity = scope.identity()
            options_text = scope.verify_k15_option()
            if (
                smoke_marker is not None
                and smoke_marker.get("scope_identity") != identity
            ):
                raise RuntimeError(
                    "RTM3004 identity differs from the scope used for K15 smoke."
                )
            self.emit("log", text=f"Connected: {identity}")
            self.emit("log", text=f"RTM3004 options: {options_text}")

            for command in ("*CLS", "WGEN:OUTP OFF", "WGEN:BURS OFF", "STOP"):
                scope.scpi.write(command)
            scope.scpi.query("*OPC?")
            scope.check_errors()

            if self.options.flash_firmware:
                self._flash()
            else:
                self.emit(
                    "log",
                    text="Using the verified K15 autonomous plan already flashed",
                )

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            suffix = "_smoke" if self.smoke else "_main"
            output_dir = self.options.output_root / f"{timestamp}{suffix}"
            output_dir.mkdir(parents=True, exist_ok=False)
            write_manifest(output_dir / "manifest.json", config, steps)
            (output_dir / "k15_run.json").write_text(
                json.dumps(
                    {
                        "run_kind": "smoke" if self.smoke else "main",
                        "capture_segments_per_step": segment_count,
                        "firmware_segments_per_step": firmware_segments,
                        "startup_segments": startup_segments,
                        "captured_step_prefixes": [
                            step.prefix for step in steps_to_run
                        ],
                        "scope_identity": identity,
                        "scope_options": options_text,
                        "capture_channels": list(capture_channels),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            runtime_config = copy.deepcopy(config)
            runtime_config["acquisition"]["waveforms_per_step"] = segment_count
            scope.configure(runtime_config, fast_segmentation=True)
            generator_state = scope.prepare_generator_before_reset()
            self.emit(
                "log",
                text=(
                    "K15 generator is OFF before RESET; enable Gen manually "
                    "after RESET before pressing Enter"
                    if manual_generator
                    else f"K15 generator verified before RESET: {generator_state}"
                ),
            )
            if manual_generator:
                # The RTM3004 keeps the front-panel Gen output under remote
                # control while this SCPI socket is open.  Release the socket
                # during the operator step so the physical Gen ON button can
                # take effect, then reconnect without reconfiguring the scope.
                scope.close()
                self.emit(
                    "log",
                    text="RTM3004 remote session released for manual Gen ON",
                )
            self._wait_for_board_reset()
            if manual_generator:
                scope = RTM3004(
                    host=config["scope"]["host"],
                    port=int(config["scope"]["port"]),
                    timeout_s=float(config["scope"]["timeout_s"]),
                )
                self.emit(
                    "log",
                    text="RTM3004 reconnected after manual Gen ON",
                )
            reset_settle_s = float(
                config.get("autonomous", {}).get("reset_settle_s", 0.5)
            )
            time.sleep(max(0.0, reset_settle_s))

            summary_path = output_dir / "summary.csv"
            with summary_path.open(
                "w",
                encoding="utf-8-sig",
                newline="",
            ) as summary_stream:
                summary_writer = csv.writer(summary_stream)
                summary_writer.writerow(self._summary_header())
                summary_stream.flush()

                generator_frequency_hz = float(config["generator"]["frequency_hz"])
                burst_duration_s = segment_count / generator_frequency_hz
                point_timeout_s = max(15.0, burst_duration_s * 1.5 + 10.0)
                transition_settle_s = float(
                    config.get("autonomous", {}).get(
                        "transition_settle_s",
                        0.5,
                    )
                )
                fixed_threshold_v = float(k15_config["fixed_threshold_v"])
                point_selection = str(
                    config["acquisition"].get("waveform_points", "MAX")
                )
                save_all_waveforms = bool(
                    k15_config.get("save_all_waveforms", True)
                )
                timestamp_tolerance_s = float(
                    k15_config.get("timestamp_tolerance_s", 1e-12)
                )
                input_reference = dict(k15_config.get("input_reference", {}))
                if bool(input_reference.get("required", False)) and 3 not in capture_channels:
                    raise ValueError(
                        "k15.input_reference.required requires C3 in capture_channels"
                    )

                for index, step in enumerate(steps_to_run):
                    self._check_abort()
                    self.emit("stage", text=f"K15 measuring {step.prefix}")
                    self.emit("step", index=index, step=step)
                    step_dir = output_dir / step.prefix
                    step_dir.mkdir()
                    (
                        step_dir / f"{step.prefix}_planned_step.json"
                    ).write_text(
                        json.dumps(
                            {
                                "mode": "segmented_history",
                                "firmware_step_index": step.index,
                                "planned_segments": segment_count,
                                "firmware_segments_per_step": firmware_segments,
                                "step": asdict(step),
                            },
                            indent=2,
                        ),
                        encoding="utf-8",
                    )

                    preflight = scope.arm_segmented(segment_count)
                    (
                        step_dir / f"{step.prefix}_preflight.json"
                    ).write_text(
                        json.dumps(preflight, indent=2),
                        encoding="utf-8",
                    )
                    generator_state = scope.verify_generator_state(
                        output_enabled=manual_generator
                    )
                    self.emit(
                        "log",
                        text=f"{step.prefix} generator ready: {generator_state}",
                    )

                    def acquisition_progress(
                        fraction: float,
                        current_index: int = index,
                    ) -> None:
                        self.emit(
                            "progress",
                            value=(
                                current_index + 0.5 * fraction
                            ) / len(steps_to_run),
                        )

                    acquisition = scope.acquire_segments(
                        segment_count=segment_count,
                        timeout_s=point_timeout_s,
                        abort_event=self.abort_event,
                        progress=acquisition_progress,
                        enable_output=not manual_generator,
                    )
                    if not manual_generator:
                        generator_state = scope.stop_continuous_generator()
                    time.sleep(max(0.0, transition_settle_s))
                    self.emit(
                        "log",
                        text=(
                            f"{step.prefix} K15 capture complete: "
                            f"{acquisition.waveform_count} segments, "
                            f"{acquisition.start_mode}; generator stopped: "
                            f"{generator_state}"
                        ),
                    )

                    relative_stats, fixed_stats, quality_warnings = (
                        self._export_history(
                            scope=scope,
                            step=step,
                            step_dir=step_dir,
                            segment_count=segment_count,
                            startup_segments=startup_segments,
                            fixed_threshold_v=fixed_threshold_v,
                            point_selection=point_selection,
                            save_all_waveforms=save_all_waveforms,
                            timestamp_tolerance_s=timestamp_tolerance_s,
                            capture_channels=capture_channels,
                            input_reference=input_reference,
                            step_progress_base=(index + 0.5) / len(steps_to_run),
                            step_progress_span=0.5 / len(steps_to_run),
                        )
                    )
                    quality_status = "warning" if quality_warnings else "ok"
                    quality_notes = " | ".join(quality_warnings)
                    summary_writer.writerow(
                        [
                            step.index,
                            step.set_id,
                            step.delay_ps,
                            step.signed_ticks,
                            step.actual_delay_ps,
                            segment_count,
                            acquisition.waveform_count,
                            startup_segments,
                            max(0, segment_count - startup_segments),
                            self._ps(relative_stats["mean_s"]),
                            self._ps(relative_stats["stddev_s"]),
                            self._ps(relative_stats["minimum_s"]),
                            self._ps(relative_stats["maximum_s"]),
                            self._ps(fixed_stats["mean_s"]),
                            self._ps(fixed_stats["stddev_s"]),
                            self._ps(
                                relative_stats[
                                    "c1_minus_c3_relative_50_mean_s"
                                ]
                            ),
                            self._ps(
                                relative_stats[
                                    "c1_minus_c3_relative_50_stddev_s"
                                ]
                            ),
                            self._ps(
                                relative_stats[
                                    "c2_minus_c3_relative_50_mean_s"
                                ]
                            ),
                            self._ps(
                                relative_stats[
                                    "c2_minus_c3_relative_50_stddev_s"
                                ]
                            ),
                            self._ps(
                                relative_stats["relative_50_closure_stddev_s"]
                            ),
                            self._ps(relative_stats["c1_rise_time_mean_s"]),
                            self._ps(relative_stats["c2_rise_time_mean_s"]),
                            self._ps(relative_stats["c3_rise_time_mean_s"]),
                            quality_status,
                            quality_notes,
                            step.prefix,
                        ]
                    )
                    summary_stream.flush()
                    self.emit(
                        "result",
                        index=index,
                        step=step,
                        stats=None,
                        quality_status=quality_status,
                        output_dir=step_dir,
                    )
                    self.emit(
                        "progress",
                        value=(index + 1) / len(steps_to_run),
                    )

            scope.finish_segmented()
            if self.smoke:
                K15_SMOKE_MARKER.write_text(
                    json.dumps(
                        {
                            "verified_at": datetime.now().isoformat(
                                timespec="seconds"
                            ),
                            "plan_signature": signature,
                            "scope_identity": identity,
                            "scope_options": options_text,
                            "smoke_segments": segment_count,
                            "capture_channels": list(capture_channels),
                            "output_dir": str(output_dir),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
        except RunAborted as error:
            status = "aborted"
            error_text = str(error)
            if scope is not None:
                scope.emergency_stop()
        except Exception as error:
            status = "failed"
            error_text = str(error)
            if scope is not None:
                scope.emergency_stop()
        finally:
            if scope is not None:
                if status != "completed":
                    try:
                        scope.finish_segmented()
                    except Exception:
                        pass
                scope.close()
            if output_dir is not None and output_dir.exists():
                (output_dir / "run_status.json").write_text(
                    json.dumps(
                        {
                            "status": status,
                            "error": error_text,
                            "finished_at": datetime.now().isoformat(
                                timespec="seconds"
                            ),
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            self.emit(
                "finished",
                status=status,
                error=error_text,
                output_dir=output_dir,
            )
        return status


def main() -> int:
    parser = argparse.ArgumentParser(
        description="RTM3004 K15 segmented-history automation"
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Override the scope host in the selected config file",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_K15_CONFIG_PATH)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--flash", action="store_true")
    args = parser.parse_args()
    if args.build and not args.flash and not args.dry_run:
        parser.error("--build requires --flash")

    config = load_config(args.config)
    scope_host = args.host or str(config["scope"]["host"])
    output_root = PROJECT_ROOT / str(config["paths"]["output_root"])
    runner_holder: dict[str, K15Runner] = {}

    def console_sink(event: str, payload: dict[str, Any]) -> None:
        _console_sink(event, payload)
        if event == "reset_required":
            manual_generator = (
                str(config.get("generator", {}).get("control", "remote"))
                == "manual"
            )
            input(
                "K15 configuration is complete. "
                + (
                    "Turn Gen output ON manually, press RESET on the board, "
                    "then press Enter: "
                    if manual_generator
                    else "Press RESET on the board, then press Enter: "
                )
            )
            runner_holder["runner"].confirm_board_reset()

    runner = K15Runner(
        RunOptions(
            scope_host=scope_host,
            repeats=1,
            output_root=output_root,
            build_firmware=args.build,
            flash_firmware=args.flash,
            dry_run=args.dry_run,
        ),
        console_sink,
        config_path=args.config,
        smoke=args.smoke,
    )
    runner_holder["runner"] = runner
    return 0 if runner.run() == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
