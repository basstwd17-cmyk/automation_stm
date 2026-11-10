"""Fail-closed K37 spectrum-analysis automation for the RTM3004."""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import sys
import time
from dataclasses import asdict
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
from common.rtm3004 import RTM3004, RunAborted, ScopeError
from common.statistics_runner import (
    FIRMWARE_HEADER_PATH,
    PROJECT_ROOT,
    AutomationRunner,
    RunOptions,
    _console_sink,
)
from k37.instrument import K37Spectrum, SpectrumPeak, SpectrumTrace


K37_DIR = Path(__file__).resolve().parent
DEFAULT_K37_CONFIG_PATH = K37_DIR / "config.json"
K37_SMOKE_MARKER = PROJECT_ROOT / "Debug" / "k37_smoke_verified.json"


class K37Runner(AutomationRunner):
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
    def _summary_header() -> list[str]:
        return [
            "index",
            "set_id",
            "source",
            "requested_delay_ps",
            "signed_ticks",
            "actual_delay_ps",
            "planned_acquisitions",
            "captured_acquisitions",
            "completion_reason",
            "spectrum_points",
            "start_frequency_hz",
            "stop_frequency_hz",
            "resolution_bandwidth_hz",
            "detected_peak_count",
            "saved_peak_count",
            "strongest_peak_frequency_hz",
            "strongest_peak_level",
            "quality_status",
            "quality_notes",
            "prefix",
        ]

    @staticmethod
    def _validate_sources(
        config: dict[str, Any],
        steps: list[SweepStep],
    ) -> dict[str, str]:
        raw_steps = config["sweep"]["steps"]
        if len(raw_steps) != len(steps):
            raise ValueError("K37 config must use exactly one firmware repeat")
        source_by_id: dict[str, str] = {}
        for raw in raw_steps:
            set_id = str(raw["id"])
            source = str(raw["source"]).upper()
            if source not in {"CH1", "CH2"}:
                raise ValueError(
                    f"{set_id}: K37 source must be CH1 or CH2"
                )
            if set_id in source_by_id:
                raise ValueError(f"Duplicate K37 set id: {set_id}")
            source_by_id[set_id] = source

        if len(raw_steps) % 2:
            raise ValueError("K37 steps must be CH1/CH2 pairs")
        for index in range(0, len(raw_steps), 2):
            first = raw_steps[index]
            second = raw_steps[index + 1]
            if (
                int(first["delay_ps"]) != int(second["delay_ps"])
                or str(first["source"]).upper() != "CH1"
                or str(second["source"]).upper() != "CH2"
            ):
                raise ValueError(
                    "Each K37 delay condition must contain consecutive "
                    "CH1 then CH2 steps with the same delay"
                )
        return source_by_id

    @staticmethod
    def _write_spectrum_csv(
        path: Path,
        traces: list[SpectrumTrace],
    ) -> None:
        if not traces:
            raise ScopeError("K37 returned no spectrum traces")
        reference = traces[0]
        for trace in traces[1:]:
            if (
                trace.point_count != reference.point_count
                or not math.isclose(
                    trace.x_origin_hz,
                    reference.x_origin_hz,
                    abs_tol=1e-9,
                )
                or not math.isclose(
                    trace.x_increment_hz,
                    reference.x_increment_hz,
                    abs_tol=1e-9,
                )
            ):
                raise ScopeError("K37 trace frequency axes do not match")

        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(
                ["frequency_hz", *[f"{trace.kind}_level" for trace in traces]]
            )
            for index, frequency_hz in enumerate(
                reference.frequencies_hz
            ):
                writer.writerow(
                    [
                        frequency_hz,
                        *[trace.levels[index] for trace in traces],
                    ]
                )

    @staticmethod
    def _write_peaks_csv(
        path: Path,
        peaks: list[SpectrumPeak],
    ) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["rank", "frequency_hz", "level"])
            for peak in peaks:
                writer.writerow(
                    [peak.rank, peak.frequency_hz, peak.level]
                )

    def run(self) -> None:
        scope: RTM3004 | None = None
        k37: K37Spectrum | None = None
        output_dir: Path | None = None
        status = "completed"
        error_text: str | None = None
        identity = ""
        options_text = ""
        signature = ""
        smoke_marker: dict[str, Any] | None = None

        try:
            config = copy.deepcopy(load_config(self.config_path))
            if config.get("measurement_mode") != "k37_spectrum":
                raise ValueError(
                    "K37 config measurement_mode must be k37_spectrum"
                )
            config["scope"]["host"] = self.options.scope_host
            configured_repeats = int(config["sweep"].get("repeats", 1))
            if configured_repeats != 1:
                raise ValueError("K37 config must use one firmware repeat")
            steps = build_steps(config, configured_repeats)
            validate_config(config, steps)
            source_by_id = self._validate_sources(config, steps)

            firmware_acquisitions = int(
                config["acquisition"]["waveforms_per_step"]
            )
            spectrum_config = config["spectrum"]
            smoke_acquisitions = int(
                spectrum_config["smoke_acquisitions"]
            )
            if not 1 <= smoke_acquisitions <= firmware_acquisitions:
                raise ValueError(
                    "spectrum.smoke_acquisitions must be between 1 and "
                    "waveforms_per_step"
                )
            capture_count = (
                smoke_acquisitions
                if self.smoke
                else firmware_acquisitions
            )
            steps_to_run = steps[:1] if self.smoke else steps

            trace_names = [
                str(name).lower()
                for name in spectrum_config["traces"]
            ]
            allowed_traces = {
                "average",
                "maximum",
                "minimum",
                "spectrum",
            }
            if not trace_names or any(
                name not in allowed_traces for name in trace_names
            ):
                raise ValueError("K37 spectrum.traces contains an invalid trace")
            if len(set(trace_names)) != len(trace_names):
                raise ValueError("K37 spectrum.traces contains duplicates")

            signature = firmware_plan_signature(
                steps,
                firmware_acquisitions,
            )
            config.setdefault("autonomous", {})["plan_signature"] = signature
            config["autonomous"]["compiled_step_count"] = len(steps)
            self.emit("plan", steps=steps_to_run)

            if self.options.dry_run:
                self._simulate(steps_to_run)
                return

            if self.smoke and K37_SMOKE_MARKER.exists():
                K37_SMOKE_MARKER.unlink()

            if self.options.build_firmware and not self.options.flash_firmware:
                raise RuntimeError(
                    "K37 Build changes the board plan, so DFU flash is required."
                )
            if self.options.build_firmware:
                changed = write_firmware_header(
                    FIRMWARE_HEADER_PATH,
                    steps,
                    firmware_acquisitions,
                )
                self.emit(
                    "log",
                    text=(
                        "Updated K37 firmware plan"
                        if changed
                        else "K37 firmware plan is unchanged"
                    ),
                )
                self._build()
                self._record_firmware_plan(
                    signature,
                    len(steps),
                    firmware_acquisitions,
                )
            else:
                self._validate_firmware_plan(
                    steps,
                    firmware_acquisitions,
                )

            if not self.smoke:
                if not K37_SMOKE_MARKER.exists():
                    raise RuntimeError(
                        "No successful K37 smoke test was found. Run smoke first."
                    )
                smoke_marker = json.loads(
                    K37_SMOKE_MARKER.read_text(encoding="utf-8")
                )
                if smoke_marker.get("plan_signature") != signature:
                    raise RuntimeError(
                        "K37 smoke test belongs to a different firmware plan."
                    )

            self.emit("stage", text="Connecting to RTM3004 for K37")
            scope = RTM3004(
                host=config["scope"]["host"],
                port=int(config["scope"]["port"]),
                timeout_s=float(config["scope"]["timeout_s"]),
            )
            identity = scope.identity()
            k37 = K37Spectrum(scope)
            options_text = k37.verify_option()
            if (
                smoke_marker is not None
                and smoke_marker.get("scope_identity") != identity
            ):
                raise RuntimeError(
                    "RTM3004 identity differs from the K37 smoke test."
                )
            self.emit("log", text=f"Connected: {identity}")
            self.emit("log", text=f"RTM3004 options: {options_text}")

            for command in (
                "*CLS",
                "WGEN:OUTP OFF",
                "WGEN:BURS OFF",
                "STOP",
            ):
                scope.scpi.write(command)
            scope.scpi.query("*OPC?")
            scope.check_errors()

            # A previously interrupted K37 run can leave Spectrum active.
            # Return to the normal time-domain state before common C1/C2
            # configuration changes timebase, acquisition memory, and WGEN.
            k37.emergency_finish()
            scope.scpi.query("*OPC?")
            scope.check_errors()

            if self.options.flash_firmware:
                self._flash()
            else:
                self.emit(
                    "log",
                    text="Using the verified K37 autonomous plan already flashed",
                )

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            suffix = "_smoke" if self.smoke else "_main"
            output_dir = self.options.output_root / f"{timestamp}{suffix}"
            output_dir.mkdir(parents=True, exist_ok=False)
            write_manifest(output_dir / "manifest.json", config, steps)
            (output_dir / "k37_run.json").write_text(
                json.dumps(
                    {
                        "run_kind": "smoke" if self.smoke else "main",
                        "capture_acquisitions_per_step": capture_count,
                        "firmware_acquisitions_per_step": firmware_acquisitions,
                        "captured_step_prefixes": [
                            step.prefix for step in steps_to_run
                        ],
                        "scope_identity": identity,
                        "scope_options": options_text,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            runtime_config = copy.deepcopy(config)
            runtime_config["acquisition"]["waveforms_per_step"] = (
                capture_count
            )
            scope.configure(runtime_config, fast_segmentation=False)
            base_readback = k37.configure(runtime_config, capture_count)
            (
                output_dir / "k37_configuration_readback.json"
            ).write_text(
                json.dumps(base_readback, indent=2),
                encoding="utf-8",
            )

            generator_state = scope.prepare_generator_before_reset()
            self.emit(
                "log",
                text=f"K37 generator verified before RESET: {generator_state}",
            )
            self._wait_for_board_reset()
            time.sleep(
                max(
                    0.0,
                    float(
                        config.get("autonomous", {}).get(
                            "reset_settle_s",
                            0.5,
                        )
                    ),
                )
            )

            summary_path = output_dir / "summary.csv"
            with summary_path.open(
                "w",
                encoding="utf-8-sig",
                newline="",
            ) as summary_stream:
                summary_writer = csv.writer(summary_stream)
                summary_writer.writerow(self._summary_header())
                summary_stream.flush()

                generator_frequency_hz = float(
                    config["generator"]["frequency_hz"]
                )
                burst_duration_s = capture_count / generator_frequency_hz
                point_timeout_s = max(
                    20.0,
                    burst_duration_s * 1.5 + 10.0,
                )
                transition_settle_s = float(
                    config.get("autonomous", {}).get(
                        "transition_settle_s",
                        0.5,
                    )
                )
                maximum_peak_results = int(
                    spectrum_config["peaks"]["maximum_results"]
                )
                save_time_waveforms = bool(
                    spectrum_config.get("save_time_waveforms", True)
                )

                for index, step in enumerate(steps_to_run):
                    self._check_abort()
                    source = source_by_id[step.set_id]
                    self.emit(
                        "stage",
                        text=f"K37 measuring {step.prefix} from {source}",
                    )
                    self.emit("step", index=index, step=step)

                    step_dir = output_dir / step.prefix
                    step_dir.mkdir()
                    (
                        step_dir / f"{step.prefix}_planned_step.json"
                    ).write_text(
                        json.dumps(
                            {
                                "mode": "k37_spectrum",
                                "firmware_step_index": step.index,
                                "source": source,
                                "planned_acquisitions": capture_count,
                                "firmware_acquisitions_per_step": (
                                    firmware_acquisitions
                                ),
                                "step": asdict(step),
                            },
                            indent=2,
                        ),
                        encoding="utf-8",
                    )

                    preflight = k37.prepare_source(
                        source,
                        capture_count,
                    )
                    (
                        step_dir / f"{step.prefix}_preflight.json"
                    ).write_text(
                        json.dumps(preflight, indent=2),
                        encoding="utf-8",
                    )

                    # Spectrum averaging needs the scope to remain in RUN
                    # after the finite WGEN burst.  The burst itself limits
                    # the number of input edges; MEAS1 WFMCount verifies it.
                    scope.arm_continuous_step()
                    first_step = index == 0
                    generator_state = scope.verify_generator_state(
                        output_enabled=not first_step
                    )
                    self.emit(
                        "log",
                        text=(
                            f"{step.prefix} {source} generator ready: "
                            f"{generator_state}"
                        ),
                    )

                    def acquisition_progress(
                        fraction: float,
                        current_index: int = index,
                    ) -> None:
                        self.emit(
                            "progress",
                            value=(
                                current_index + 0.75 * fraction
                            ) / len(steps_to_run),
                        )

                    acquisition = scope.acquire_waveforms(
                        waveform_count=capture_count,
                        timeout_s=point_timeout_s,
                        abort_event=self.abort_event,
                        progress=acquisition_progress,
                        enable_output=first_step,
                        return_when_count_reached=True,
                    )

                    # Preserve evidence even if K37 post-processing times
                    # out: this PNG is the scope state immediately after the
                    # requested trigger burst, before waiting on FFT average.
                    scope.save_screenshot(
                        step_dir / f"{step.prefix}_capture.png"
                    )
                    if save_time_waveforms:
                        scope.save_waveforms(
                            step_dir
                            / f"{step.prefix}_time_waveforms.csv",
                            str(
                                config["acquisition"].get(
                                    "waveform_points",
                                    "DMAX",
                                )
                            ),
                        )
                    average_complete = k37.wait_average_complete(
                        # The RTM3004 may retain COMPLETE=0 after a finite
                        # burst despite exposing valid raw trace data. Do not
                        # spend tens of seconds on that advisory flag.
                        timeout_s=5.0,
                        abort_event=self.abort_event,
                    )

                    traces = [
                        k37.read_trace(name)
                        for name in trace_names
                    ]
                    self._write_spectrum_csv(
                        step_dir / f"{step.prefix}_spectrum.csv",
                        traces,
                    )
                    detected_peak_count, peaks = k37.read_peaks(
                        maximum_peak_results
                    )
                    self._write_peaks_csv(
                        step_dir / f"{step.prefix}_peaks.csv",
                        peaks,
                    )

                    scope.save_screenshot(
                        step_dir / f"{step.prefix}_spectrogram.png"
                    )

                    strongest_peak = (
                        max(peaks, key=lambda peak: peak.level)
                        if peaks
                        else None
                    )
                    readback = k37.readback()
                    metadata = {
                        "source": source,
                        "magnitude_scale": readback["magnitude_scale"],
                        "trace_names": trace_names,
                        "point_count": traces[0].point_count,
                        "x_origin_hz": traces[0].x_origin_hz,
                        "x_increment_hz": traces[0].x_increment_hz,
                        "start_frequency_hz": readback[
                            "start_frequency_hz"
                        ],
                        "stop_frequency_hz": readback[
                            "stop_frequency_hz"
                        ],
                        "resolution_bandwidth_hz": readback[
                            "resolution_bandwidth_hz"
                        ],
                        "detected_peak_count": detected_peak_count,
                        "saved_peak_count": len(peaks),
                    }
                    (
                        step_dir / f"{step.prefix}_spectrum_metadata.json"
                    ).write_text(
                        json.dumps(metadata, indent=2),
                        encoding="utf-8",
                    )

                    quality_notes_list = [
                        note
                        for note in (
                            acquisition.warning,
                            (
                                "K37 averaging COMPLETE flag remained 0; "
                                "available raw Spectrum data was saved"
                                if not average_complete
                                else None
                            ),
                        )
                        if note
                    ]
                    quality_notes = "; ".join(quality_notes_list)
                    quality_status = (
                        "warning" if quality_notes else "ok"
                    )
                    summary_writer.writerow(
                        [
                            step.index,
                            step.set_id,
                            source,
                            step.delay_ps,
                            step.signed_ticks,
                            step.actual_delay_ps,
                            capture_count,
                            acquisition.waveform_count,
                            acquisition.completion_reason,
                            traces[0].point_count,
                            readback["start_frequency_hz"],
                            readback["stop_frequency_hz"],
                            readback["resolution_bandwidth_hz"],
                            detected_peak_count,
                            len(peaks),
                            (
                                strongest_peak.frequency_hz
                                if strongest_peak
                                else ""
                            ),
                            (
                                strongest_peak.level
                                if strongest_peak
                                else ""
                            ),
                            quality_status,
                            quality_notes,
                            step.prefix,
                        ]
                    )
                    summary_stream.flush()
                    self.emit(
                        "log",
                        text=(
                            f"{step.prefix} K37 capture complete: "
                            f"{acquisition.waveform_count} acquisitions, "
                            f"{traces[0].point_count} spectrum points, "
                            f"{detected_peak_count} peaks"
                        ),
                    )
                    self.emit(
                        "result",
                        index=index,
                        step=step,
                        stats=None,
                        output_dir=step_dir,
                    )
                    self.emit(
                        "progress",
                        value=(index + 1) / len(steps_to_run),
                    )
                    time.sleep(max(0.0, transition_settle_s))

            k37.finish()
            scope.finish()
            if self.smoke:
                K37_SMOKE_MARKER.write_text(
                    json.dumps(
                        {
                            "verified_at": datetime.now().isoformat(
                                timespec="seconds"
                            ),
                            "plan_signature": signature,
                            "scope_identity": identity,
                            "scope_options": options_text,
                            "smoke_acquisitions": capture_count,
                            "output_dir": str(output_dir),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
        except RunAborted as error:
            status = "aborted"
            error_text = str(error)
            if k37 is not None:
                k37.emergency_finish()
            if scope is not None:
                scope.emergency_stop()
        except Exception as error:
            status = "failed"
            error_text = str(error)
            if k37 is not None:
                k37.emergency_finish()
            if scope is not None:
                scope.emergency_stop()
        finally:
            if scope is not None:
                if status != "completed":
                    if k37 is not None:
                        try:
                            k37.finish()
                        except Exception:
                            pass
                    try:
                        scope.finish()
                    except Exception:
                        pass
                scope.close()
            if output_dir is not None:
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="RTM3004 K37 spectrum-analysis automation"
    )
    parser.add_argument("--host", default="192.168.50.2")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_K37_CONFIG_PATH,
    )
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--flash", action="store_true")
    args = parser.parse_args()
    if args.build and not args.flash and not args.dry_run:
        parser.error("--build requires --flash")

    config = load_config(args.config)
    output_root = PROJECT_ROOT / str(config["paths"]["output_root"])
    runner_holder: dict[str, K37Runner] = {}

    def console_sink(event: str, payload: dict[str, Any]) -> None:
        _console_sink(event, payload)
        if event == "reset_required":
            input(
                "K37 configuration is complete. Press RESET on the board, "
                "then press Enter: "
            )
            runner_holder["runner"].confirm_board_reset()

    runner = K37Runner(
        RunOptions(
            scope_host=args.host,
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
    runner.run()


if __name__ == "__main__":
    main()
