"""Proven RTM3004 statistics runner and shared firmware orchestration."""

from __future__ import annotations

import argparse
import copy
import csv
import json
import locale
import os
import statistics
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .plan import (
    SweepStep,
    build_steps,
    firmware_header_matches,
    firmware_plan_signature,
    load_config,
    validate_config,
    write_firmware_header,
    write_manifest,
)
from .rtm3004 import MeasurementStats, RTM3004, RunAborted


COMMON_DIR = Path(__file__).resolve().parent
AUTOMATION_DIR = COMMON_DIR.parent
PROJECT_ROOT = AUTOMATION_DIR.parents[1]
DEFAULT_CONFIG_PATH = AUTOMATION_DIR / "ab_compare" / "config.json"
DEBUG_DIR = PROJECT_ROOT / "Debug"
ELF_PATH = DEBUG_DIR / "delay_generator_g474ceu6.elf"
FIRMWARE_HEADER_PATH = PROJECT_ROOT / "Core" / "Inc" / "autonomous_sweep.h"
# CubeIDE owns the Debug directory on some workstations, so keep automation's
# mutable verification state in the writable output tree rather than beside
# build artifacts.
FIRMWARE_PLAN_STAMP = (
    PROJECT_ROOT / "output" / "_automation_state" / "firmware_plan.json"
)
CUBE_PROGRAMMER = Path(os.environ.get("STM32_PROGRAMMER_CLI",
    r"C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin"
    r"\STM32_Programmer_CLI.exe"
))


EventSink = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class RunOptions:
    scope_host: str
    repeats: int
    output_root: Path
    build_firmware: bool = False
    flash_firmware: bool = False
    dry_run: bool = False
    dfu_wait_timeout_s: float = 180.0


class AutomationRunner:
    def __init__(self, options: RunOptions, event_sink: EventSink) -> None:
        self.options = options
        self.event_sink = event_sink
        self.abort_event = threading.Event()
        self.pause_event = threading.Event()
        self.stop_after_step_event = threading.Event()
        self.board_reset_event = threading.Event()

    def abort(self) -> None:
        self.abort_event.set()
        self.board_reset_event.set()

    def confirm_board_reset(self) -> None:
        self.board_reset_event.set()

    def request_pause(self, paused: bool) -> None:
        if paused:
            self.pause_event.set()
        else:
            self.pause_event.clear()

    def request_stop_after_step(self) -> None:
        self.stop_after_step_event.set()

    def emit(self, event: str, **payload: Any) -> None:
        self.event_sink(event, payload)

    def _check_abort(self) -> None:
        if self.abort_event.is_set():
            raise RunAborted("Run aborted by user")

    def _wait_if_paused(self) -> None:
        announced = False
        while self.pause_event.is_set():
            self._check_abort()
            if not announced:
                self.emit("stage", text="Paused between measurement points")
                announced = True
            time.sleep(0.1)

    def _wait_for_board_reset(self) -> None:
        self.board_reset_event.clear()
        self.emit("stage", text="Waiting for board RESET")
        self.emit("reset_required")
        while not self.board_reset_event.wait(0.1):
            self._check_abort()
        self._check_abort()
        self.emit(
            "log",
            text="Board RESET acknowledged by operator",
        )
        time.sleep(0.1)

    @staticmethod
    def _find_plateau_change(values_s: list[float]) -> dict[str, float | int] | None:
        if len(values_s) < 20:
            return None

        values_ps = [value * 1e12 for value in values_s]
        minimum_segment = 10
        best: dict[str, float | int] | None = None
        best_cost = float("inf")
        for split in range(
            minimum_segment,
            len(values_ps) - minimum_segment + 1,
        ):
            left = values_ps[:split]
            right = values_ps[split:]
            left_median = statistics.median(left)
            right_median = statistics.median(right)
            left_mad = statistics.median(
                abs(value - left_median) for value in left
            )
            right_mad = statistics.median(
                abs(value - right_median) for value in right
            )
            cost = sum(abs(value - left_median) for value in left)
            cost += sum(abs(value - right_median) for value in right)
            separation_ps = abs(right_median - left_median)
            threshold_ps = max(100.0, 3.0 * max(left_mad, right_mad))
            if cost < best_cost:
                best_cost = cost
                best = {
                    "split": split,
                    "left_median_ps": left_median,
                    "right_median_ps": right_median,
                    "separation_ps": separation_ps,
                    "threshold_ps": threshold_ps,
                }
        if (
            best is None or
            float(best["separation_ps"]) <= float(best["threshold_ps"])
        ):
            return None
        return best

    @staticmethod
    def _find_build_tools() -> tuple[Path, Path]:
        roots = []
        if os.environ.get("STM32CUBEIDE_ROOT"):
            roots.append(Path(os.environ["STM32CUBEIDE_ROOT"]))
        roots.extend((PROJECT_ROOT.parent, PROJECT_ROOT.parent / "STM32Cube", Path("C:/ST")))
        for root in roots:
            for prefix in ("plugins/", "STM32CubeIDE/plugins/", "STM32CubeIDE_*/STM32CubeIDE/plugins/"):
                make_paths = sorted(root.glob(prefix + "com.st.stm32cube.ide.mcu.externaltools.make.win32_*/tools/bin/make.exe"))
                gcc_bins = sorted(root.glob(prefix + "com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.*.win32_*/tools/bin"))
                if make_paths and gcc_bins:
                    return make_paths[-1], gcc_bins[-1]
        raise FileNotFoundError(
            "STM32CubeIDE make/GCC tools were not found. Set STM32CUBEIDE_ROOT "
            "to the installation directory containing plugins/."
        )

    def _run_process(
        self,
        command: list[str],
        cwd: Path,
        environment: dict[str, str] | None = None,
    ) -> str:
        self.emit("log", text="> " + subprocess.list2cmdline(command))
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
        )
        output_lines: list[str] = []
        assert process.stdout is not None
        while True:
            line = process.stdout.readline()
            if line:
                stripped = line.rstrip()
                output_lines.append(stripped)
                self.emit("log", text=stripped)
            if process.poll() is not None:
                break
            if self.abort_event.is_set():
                process.terminate()
                process.wait(timeout=5)
                raise RunAborted("Process stopped by user")
        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError(
                f"Command failed with exit code {return_code}: {command[0]}"
            )
        return "\n".join(output_lines)

    def _build(self) -> None:
        if not (DEBUG_DIR / "makefile").is_file():
            raise FileNotFoundError(
                "Debug/makefile is missing. Import this project into STM32CubeIDE "
                "and build Debug once before using automation Build."
            )
        make_path, gcc_bin = self._find_build_tools()
        environment = os.environ.copy()
        environment["PATH"] = str(gcc_bin) + os.pathsep + environment.get("PATH", "")
        self.emit("stage", text="Building STM32 firmware")
        try:
            self._run_process(
                [str(make_path), "-j8", "all"],
                cwd=DEBUG_DIR,
                environment=environment,
            )
        except RuntimeError as error:
            raise RuntimeError(
                f"Firmware build failed. Close CubeIDE if main.su is locked. {error}"
            ) from error
        if not ELF_PATH.exists():
            raise FileNotFoundError(f"Build completed without ELF output: {ELF_PATH}")

    @staticmethod
    def _record_firmware_plan(
        signature: str,
        step_count: int,
        pulses_per_step: int,
    ) -> None:
        elf_stat = ELF_PATH.stat()
        payload = {
            "signature": signature,
            "step_count": step_count,
            "pulses_per_step": pulses_per_step,
            "elf_size": elf_stat.st_size,
            "elf_modified_ns": elf_stat.st_mtime_ns,
            "built_at": datetime.now().isoformat(timespec="seconds"),
        }
        FIRMWARE_PLAN_STAMP.parent.mkdir(parents=True, exist_ok=True)
        FIRMWARE_PLAN_STAMP.write_text(
            json.dumps(payload, indent=2),
            encoding="ascii",
        )

    @staticmethod
    def _validate_firmware_plan(
        steps: list[SweepStep],
        pulses_per_step: int,
    ) -> str:
        signature = firmware_plan_signature(steps, pulses_per_step)
        if not firmware_header_matches(FIRMWARE_HEADER_PATH, steps, pulses_per_step):
            raise RuntimeError(
                "Measurement plan differs from the firmware header. "
                "Enable Build once and DFU flash once."
            )
        if not ELF_PATH.exists() or not FIRMWARE_PLAN_STAMP.exists():
            raise RuntimeError(
                "No verified autonomous firmware build was found. "
                "Enable Build once and DFU flash once."
            )
        stamp = json.loads(FIRMWARE_PLAN_STAMP.read_text(encoding="ascii"))
        if stamp.get("signature") != signature:
            raise RuntimeError(
                "The ELF was built for a different autonomous plan. "
                "Enable Build once and DFU flash once."
            )
        return signature

    def _dfu_available(self) -> bool:
        if not CUBE_PROGRAMMER.exists():
            raise FileNotFoundError(f"STM32CubeProgrammer CLI not found: {CUBE_PROGRAMMER}")
        result = subprocess.run(
            [str(CUBE_PROGRAMMER), "-l", "usb"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
            check=False,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0 and "No STM32 device in DFU mode" not in output

    def _wait_for_dfu(self) -> None:
        self.emit("stage", text="Waiting for STM32 USB DFU mode")
        self.emit("log", text="Enter DFU mode with BOOT and RESET; the run continues automatically.")
        deadline = time.monotonic() + self.options.dfu_wait_timeout_s
        next_log = 0.0
        while time.monotonic() < deadline:
            self._check_abort()
            if self._dfu_available():
                self.emit("log", text="STM32 DFU device detected")
                return
            if time.monotonic() >= next_log:
                self.emit("log", text="Still waiting for DFU device...")
                next_log = time.monotonic() + 5.0
            time.sleep(1.0)
        raise TimeoutError("STM32 did not enter DFU mode before the timeout")

    def _flash(self) -> None:
        self._wait_for_dfu()
        self.emit("stage", text="Programming and starting STM32")
        self._run_process(
            [
                str(CUBE_PROGRAMMER),
                "-c",
                "port=USB1",
                "-d",
                str(ELF_PATH),
                "-v",
                "-s",
            ],
            cwd=PROJECT_ROOT,
        )
        time.sleep(2.0)

    @staticmethod
    def _summary_header() -> list[str]:
        return [
            "index",
            "set_id",
            "repeat",
            "requested_delay_ps",
            "signed_ticks",
            "actual_delay_ps",
            "planned_pulse_count",
            "measured_actual_ps",
            "measured_mean_ps",
            "measured_stddev_ps",
            "measured_min_ps",
            "measured_max_ps",
            "waveform_count",
            "sample_count",
            "quality_status",
            "quality_notes",
            "error_from_actual_ps",
            "prefix",
        ]

    @staticmethod
    def _summary_row(
        step: SweepStep,
        planned_pulse_count: int,
        stats: MeasurementStats,
        sample_count: int,
        quality_status: str,
        quality_notes: str,
    ) -> list[Any]:
        measured_mean_ps = stats.mean_s * 1e12
        return [
            step.index,
            step.set_id,
            step.repeat,
            step.delay_ps,
            step.signed_ticks,
            step.actual_delay_ps,
            planned_pulse_count,
            stats.actual_s * 1e12,
            measured_mean_ps,
            stats.stddev_s * 1e12,
            stats.minimum_s * 1e12,
            stats.maximum_s * 1e12,
            stats.waveform_count,
            sample_count,
            quality_status,
            quality_notes,
            measured_mean_ps - step.actual_delay_ps,
            step.prefix,
        ]

    def _simulate(self, steps: list[SweepStep]) -> None:
        self.emit("stage", text="Dry-run simulation")
        for index, step in enumerate(steps):
            self._wait_if_paused()
            self._check_abort()
            self.emit("step", index=index, step=step)
            time.sleep(0.03)
            self.emit(
                "result",
                index=index,
                step=step,
                stats=None,
                output_dir=None,
            )
            self.emit("progress", value=(index + 1) / len(steps))
            if self.stop_after_step_event.is_set():
                break

    def run(self) -> None:
        scope: RTM3004 | None = None
        output_dir: Path | None = None
        status = "completed"
        error_text: str | None = None
        try:
            config = copy.deepcopy(load_config(DEFAULT_CONFIG_PATH))
            config["scope"]["host"] = self.options.scope_host
            config["sweep"]["repeats"] = self.options.repeats
            steps = build_steps(config, self.options.repeats)
            validate_config(config, steps)
            pulses = int(config["acquisition"]["waveforms_per_step"])
            signature = firmware_plan_signature(steps, pulses)
            autonomous = config.setdefault("autonomous", {})
            autonomous["plan_signature"] = signature
            autonomous["compiled_step_count"] = len(steps)
            self.emit("plan", steps=steps)

            if self.options.dry_run:
                self._simulate(steps)
                if self.stop_after_step_event.is_set():
                    status = "stopped"
                return

            if self.options.build_firmware and not self.options.flash_firmware:
                raise RuntimeError(
                    "Build once changes the board plan, so DFU flash once must also be enabled."
                )

            if self.options.build_firmware:
                changed = write_firmware_header(FIRMWARE_HEADER_PATH, steps, pulses)
                self.emit(
                    "log",
                    text=(
                        "Updated autonomous firmware plan"
                        if changed
                        else "Autonomous firmware plan is unchanged"
                    ),
                )
                self._build()
                self._record_firmware_plan(signature, len(steps), pulses)
            else:
                self._validate_firmware_plan(steps, pulses)

            self.emit("stage", text="Connecting to RTM3004")
            scope = RTM3004(
                host=config["scope"]["host"],
                port=int(config["scope"]["port"]),
                timeout_s=float(config["scope"]["timeout_s"]),
            )
            identity = scope.identity()
            self.emit("log", text=f"Connected: {identity}")
            scope.scpi.write("*CLS")
            scope.scpi.write("WGEN:OUTP OFF")
            scope.scpi.write("WGEN:BURS OFF")
            scope.scpi.write("STOP")
            scope.scpi.query("*OPC?")
            scope.check_errors()

            if self.options.flash_firmware:
                self._flash()
            else:
                self.emit("log", text="Using the autonomous plan already flashed on the board")

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_dir = self.options.output_root / timestamp
            output_dir.mkdir(parents=True, exist_ok=False)
            write_manifest(output_dir / "manifest.json", config, steps)

            self.emit("stage", text="Configuring RTM3004 and generator")
            scope.configure(config)
            generator_state = scope.prepare_generator_before_reset()
            self.emit(
                "log",
                text=f"Generator verified before board RESET: {generator_state}",
            )
            self._wait_for_board_reset()
            reset_settle_s = float(
                config.get("autonomous", {}).get("reset_settle_s", 0.5)
            )
            self.emit(
                "log",
                text=(
                    "Generator output remains OFF; waiting "
                    f"{reset_settle_s:.3f} s for MCU startup"
                ),
            )
            time.sleep(max(0.0, reset_settle_s))

            summary_path = output_dir / "summary.csv"
            with summary_path.open("w", encoding="utf-8-sig", newline="") as summary_stream:
                summary_writer = csv.writer(summary_stream)
                summary_writer.writerow(self._summary_header())
                summary_stream.flush()

                generator_frequency_hz = float(config["generator"]["frequency_hz"])
                burst_duration_s = pulses / generator_frequency_hz
                point_timeout = max(
                    10.0,
                    burst_duration_s * 2.0 + 5.0,
                )
                self.emit(
                    "log",
                    text=(
                        f"Burst acquisition: {pulses} cycles at "
                        f"{generator_frequency_hz:g} Hz "
                        f"({burst_duration_s:.3f} s per point, "
                        f"{point_timeout:.1f} s timeout)"
                    ),
                )
                point_selection = str(config["acquisition"]["waveform_points"])
                transition_settle_s = float(
                    config.get("autonomous", {}).get("transition_settle_s", 0.5)
                )

                for index, step in enumerate(steps):
                    self._wait_if_paused()
                    self._check_abort()
                    self.emit("stage", text=f"Measuring {step.prefix}")
                    self.emit("step", index=index, step=step)

                    step_dir = output_dir / step.prefix
                    step_dir.mkdir()
                    (step_dir / f"{step.prefix}_planned_step.json").write_text(
                        json.dumps(
                            {
                                "mode": "autonomous",
                                "firmware_step_index": index,
                                "planned_pulse_count": pulses,
                                "step": asdict(step),
                            },
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    screenshot_path = step_dir / f"{step.prefix}.png"
                    acquisition_started = False
                    evidence_saved = False
                    quality_warnings: list[str] = []
                    try:
                        scope.arm_step(pulses)
                        first_step = index == 0
                        if first_step:
                            generator_state = scope.verify_generator_state(
                                output_enabled=False
                            )
                        else:
                            generator_state = scope.verify_idle_generator_output()
                        self.emit(
                            "log",
                            text=(
                                f"{step.prefix} generator ready: "
                                f"{generator_state}"
                            ),
                        )

                        def report_progress(
                            fraction: float,
                            i: int = index,
                        ) -> None:
                            self.emit(
                                "progress",
                                value=(i + fraction) / len(steps),
                            )

                        acquisition_started = True
                        acquisition = scope.acquire_waveforms(
                            waveform_count=pulses,
                            timeout_s=point_timeout,
                            abort_event=self.abort_event,
                            progress=report_progress,
                            enable_output=first_step,
                        )
                        self.emit(
                            "log",
                            text=(
                                f"{step.prefix} burst start: "
                                f"{acquisition.start_mode}"
                            ),
                        )
                        if acquisition.warning is not None:
                            quality_warnings.append(acquisition.warning)
                        time.sleep(max(0.0, transition_settle_s))

                        stats = scope.read_stats()
                        if stats.waveform_count != pulses:
                            quality_warnings.append(
                                f"{step.prefix}: expected {pulses} waveforms, "
                                f"received {stats.waveform_count}"
                            )

                        samples = scope.read_stats_values()
                        if len(samples) != pulses:
                            quality_warnings.append(
                                f"{step.prefix}: expected {pulses} delay samples, "
                                f"received {len(samples)}"
                            )

                        scope.write_stats_values(
                            step_dir / f"{step.prefix}_delay_samples.csv",
                            samples,
                        )
                        evidence_saved = True

                        plateau_change = self._find_plateau_change(samples)
                        if plateau_change is not None:
                            mixed_path = (
                                step_dir /
                                f"{step.prefix}_mixed_delay_samples.csv"
                            )
                            scope.write_stats_values(mixed_path, samples)
                            quality_warnings.append(
                                f"{step.prefix}: mixed delay plateaus at sample "
                                f"{plateau_change['split']}/{len(samples)} "
                                f"({plateau_change['left_median_ps']:.1f} ps -> "
                                f"{plateau_change['right_median_ps']:.1f} ps)"
                            )

                        try:
                            scope.save_waveforms(
                                step_dir / f"{step.prefix}_waveforms.csv",
                                point_selection,
                            )
                        except Exception as artifact_error:
                            quality_warnings.append(
                                f"{step.prefix}: waveform CSV not saved: "
                                f"{artifact_error}"
                            )
                        try:
                            scope.save_screenshot(screenshot_path)
                        except Exception as artifact_error:
                            quality_warnings.append(
                                f"{step.prefix}: screenshot not saved: "
                                f"{artifact_error}"
                            )
                    except Exception:
                        if acquisition_started and not evidence_saved:
                            try:
                                partial_path = (
                                    step_dir /
                                    f"{step.prefix}_partial_delay_samples.csv"
                                )
                                partial_count = scope.save_partial_stats_values(
                                    partial_path
                                )
                                if partial_count > 0:
                                    self.emit(
                                        "log",
                                        text=(
                                            f"Saved {partial_count} partial "
                                            f"samples: {partial_path}"
                                        ),
                                    )
                            except Exception as partial_error:
                                self.emit(
                                    "log",
                                    text=(
                                        "Could not save partial samples: "
                                        f"{partial_error}"
                                    ),
                                )
                        raise

                    quality_status = "warning" if quality_warnings else "ok"
                    quality_notes = " | ".join(quality_warnings)
                    for warning in quality_warnings:
                        self.emit("log", text=f"WARNING: {warning}")
                    summary_writer.writerow(
                        self._summary_row(
                            step,
                            pulses,
                            stats,
                            len(samples),
                            quality_status,
                            quality_notes,
                        )
                    )
                    summary_stream.flush()
                    self.emit(
                        "result",
                        index=index,
                        step=step,
                        stats=stats,
                        quality_status=quality_status,
                        output_dir=step_dir,
                    )
                    if screenshot_path.exists():
                        self.emit("screenshot", path=screenshot_path)
                    self.emit("progress", value=(index + 1) / len(steps))

                    if self.stop_after_step_event.is_set():
                        status = "stopped"
                        break

            scope.finish()
        except RunAborted as error:
            status = "aborted"
            error_text = str(error)
            if scope is not None:
                scope.emergency_stop()
        except Exception as error:  # The UI needs the complete hardware failure text.
            status = "failed"
            error_text = str(error)
            if scope is not None:
                scope.emergency_stop()
        finally:
            if scope is not None:
                scope.close()
            if output_dir is not None:
                status_payload = {
                    "status": status,
                    "error": error_text,
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                }
                (output_dir / "run_status.json").write_text(
                    json.dumps(status_payload, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            self.emit(
                "finished",
                status=status,
                error=error_text,
                output_dir=output_dir,
            )


def _console_sink(event: str, payload: dict[str, Any]) -> None:
    if event in ("log", "stage"):
        print(payload["text"])
    elif event == "progress":
        print(f"Progress: {payload['value'] * 100:.1f}%")
    elif event == "finished":
        print(f"Finished: {payload['status']}")
        if payload.get("error"):
            print(payload["error"])


def main() -> None:
    parser = argparse.ArgumentParser(description="RTM3004 delay-sweep automation")
    parser.add_argument("--host", default="192.168.50.2")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--flash", action="store_true")
    args = parser.parse_args()
    if args.build and not args.flash and not args.dry_run:
        parser.error("--build requires --flash for an autonomous measurement run")
    config = load_config(DEFAULT_CONFIG_PATH)
    output_root = PROJECT_ROOT / str(config["paths"]["output_root"])
    runner_holder: dict[str, AutomationRunner] = {}

    def console_sink(event: str, payload: dict[str, Any]) -> None:
        _console_sink(event, payload)
        if event == "reset_required":
            input(
                "RTM3004 configuration is complete. Press RESET on the board, "
                "then press Enter: "
            )
            runner_holder["runner"].confirm_board_reset()

    runner = AutomationRunner(
        RunOptions(
            scope_host=args.host,
            repeats=args.repeats,
            output_root=output_root,
            build_firmware=args.build,
            flash_firmware=args.flash,
            dry_run=args.dry_run,
        ),
        console_sink,
    )
    runner_holder["runner"] = runner
    runner.run()


if __name__ == "__main__":
    main()
