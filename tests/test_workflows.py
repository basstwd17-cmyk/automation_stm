"""Offline integration checks for the collected measurement workflows."""

from __future__ import annotations

import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tools/automation"), str(ROOT / "tools/analysis")]

from prepare_plan import prepare
from common.plan import build_steps, firmware_header_matches, load_config, validate_config
from ab_compare.compare_runs import compare
from analyze_external_input import analyze
from common.statistics_runner import RunOptions
from k15.runner import K15Runner


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_root = (ROOT / "output/test_tmp").resolve()
        assert cls.temp_root.is_relative_to(ROOT.resolve())
        cls.temp_root.mkdir(parents=True, exist_ok=True)

    def test_every_shipped_plan_is_valid_and_mode_is_explicit(self):
        configs = sorted((ROOT / "tools/automation").glob("*/config*.json"))
        self.assertGreaterEqual(len(configs), 5)
        with tempfile.TemporaryDirectory(dir=self.temp_root) as folder:
            header = Path(folder) / "plan.h"
            for path in configs:
                with self.subTest(config=path.relative_to(ROOT)):
                    config = load_config(path)
                    validate_config(config, build_steps(config))
                    result = prepare(path, header)
                    self.assertEqual(
                        "AUTONOMOUS_SWEEP_TRANSITION_GUARD_PULSES" in header.read_text(),
                        result["transition_mode"] == "rising_edge_count",
                    )
                    self.assertFalse(prepare(path, header)["changed"])

    def test_default_header_matches_continuous_input_plan(self):
        config = load_config(ROOT / "tools/automation/k15/config_input_reference.json")
        self.assertTrue(firmware_header_matches(
            ROOT / "Core/Inc/autonomous_sweep.h", build_steps(config),
            int(config["autonomous"]["pulses_per_step"]),
            int(config["autonomous"]["transition_guard_pulses"]),
        ))

    def test_burst_runner_rejects_continuous_plan_before_changing_firmware(self):
        header = ROOT / "Core/Inc/autonomous_sweep.h"
        before = header.read_bytes()
        events = []
        runner = K15Runner(
            RunOptions(scope_host="unused", repeats=1, output_root=self.temp_root, dry_run=True),
            lambda event, payload: events.append((event, payload)),
            config_path=ROOT / "tools/automation/k15/config_input_reference.json",
            smoke=True,
        )
        self.assertEqual(runner.run(), "failed")
        self.assertIn("external_input_k15.py", events[-1][1]["error"])
        self.assertEqual(header.read_bytes(), before)

    def test_common_input_variation_cancels_in_native_export(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as folder:
            source = Path(folder) / "input_output_segments.csv"
            with source.open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["t50_c1_s", "t50_c2_s", "t50_c3_s"])
                for common in (0.0, 1e-9, 2e-9, 3e-9):
                    writer.writerow([60e-9 + common, 62e-9 + common, 0.0])
            result = analyze(source)
            self.assertGreater(result["d1_c1_minus_input"]["sigma_ps_rms"], 1000)
            self.assertAlmostEqual(result["delta_c2_minus_c1"]["mean_ps"], 2000)
            self.assertAlmostEqual(result["delta_c2_minus_c1"]["sigma_ps_rms"], 0)

    def test_ab_swap_separates_path_and_dut_and_rejects_bad_counts(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as folder:
            root = Path(folder)
            for name, orientation in (("A", 1), ("B", -1)):
                run = root / name
                run.mkdir()
                rows = []
                for condition, delay in (("Z00", 0), ("P11", 2000)):
                    prefix = condition
                    step = run / prefix
                    step.mkdir()
                    with (step / f"{prefix}_delay_samples.csv").open("w", newline="") as stream:
                        writer = csv.writer(stream)
                        writer.writerow(["delay_s"])
                        for noise in (-10, 0, 10):
                            writer.writerow([(orientation * delay + 500 + noise) * 1e-12])
                    rows.append({"set_id": condition, "repeat": 1, "actual_delay_ps": delay,
                                 "prefix": prefix, "sample_count": 3, "quality_status": "ok"})
                with (run / "summary.csv").open("w", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
                (run / "run_status.json").write_text('{"status":"completed"}')
            output = compare([root / "A"], [root / "B"], root / "comparison")
            report = json.loads((output / "ab_comparison_summary.json").read_text())
            self.assertAlmostEqual(report["overall_path_skew_mean_ps"], 500)
            with (output / "ab_deembedded_conditions.csv").open(encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            positive = next(row for row in rows if row["set_id"] == "P11")
            self.assertAlmostEqual(float(positive["dut_mean_ps"]), 2000)
            samples = root / "B/P11/P11_delay_samples.csv"
            samples.write_text("delay_s\n0\n")
            with self.assertRaises(ValueError):
                compare([root / "A"], [root / "B"], root / "rejected")


if __name__ == "__main__":
    unittest.main()
