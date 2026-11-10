"""Tkinter control panel for C1-C2 and A/B swap measurements."""

from __future__ import annotations

import queue
import sys
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

AUTOMATION_DIR = Path(__file__).resolve().parents[1]
if str(AUTOMATION_DIR) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_DIR))

from common.plan import SweepStep, build_steps, load_config
from common.statistics_runner import (
    PROJECT_ROOT,
    AutomationRunner,
    RunOptions,
)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

class AutomationApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("C1-C2 / A-B Swap Delay Measurement")
        self.root.geometry("1280x840")
        self.root.minsize(1040, 700)

        self.config = load_config(DEFAULT_CONFIG_PATH)
        default_output = PROJECT_ROOT / str(self.config["paths"]["output_root"])
        self.scope_host = tk.StringVar(value=str(self.config["scope"]["host"]))
        self.repeats = tk.IntVar(value=int(self.config["sweep"]["repeats"]))
        self.output_root = tk.StringVar(value=str(default_output))
        self.build_firmware = tk.BooleanVar(value=False)
        self.flash_firmware = tk.BooleanVar(value=False)
        self.dry_run = tk.BooleanVar(value=False)
        self.progress = tk.DoubleVar(value=0.0)
        self.stage_text = tk.StringVar(value="Ready")
        self.current_text = tk.StringVar(value="-")
        self.board_text = tk.StringVar(value="Autonomous array")

        self.events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self.runner: AutomationRunner | None = None
        self.worker: threading.Thread | None = None
        self.paused = False
        self.latest_image: tk.PhotoImage | None = None

        self._configure_style()
        self._build_ui()
        self._populate_preview()
        self.root.after(50, self._pump_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Treeview", rowheight=24)
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=tk.X)
        ttk.Label(
            header,
            text="C1-C2 / A-B Swap Delay Measurement",
            style="Title.TLabel",
        ).pack(side=tk.LEFT)
        ttk.Label(header, textvariable=self.stage_text, style="Status.TLabel").pack(
            side=tk.RIGHT
        )

        settings = ttk.LabelFrame(outer, text="Run configuration", padding=10)
        settings.pack(fill=tk.X, pady=(10, 8))

        ttk.Label(settings, text="Scope IP").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(settings, textvariable=self.scope_host, width=17).grid(
            row=1, column=0, sticky=tk.W, padx=(0, 12)
        )
        ttk.Label(settings, text="Repeats").grid(row=0, column=1, sticky=tk.W)
        ttk.Spinbox(settings, from_=1, to=20, textvariable=self.repeats, width=8).grid(
            row=1, column=1, sticky=tk.W, padx=(0, 12)
        )
        ttk.Label(settings, text="Output folder").grid(row=0, column=2, sticky=tk.W)
        ttk.Entry(settings, textvariable=self.output_root).grid(
            row=1, column=2, sticky=tk.EW, padx=(0, 6)
        )
        ttk.Button(settings, text="Browse", command=self._browse_output).grid(
            row=1, column=3, sticky=tk.W, padx=(0, 14)
        )

        checks = ttk.Frame(settings)
        checks.grid(row=0, column=4, rowspan=2, sticky=tk.W)
        ttk.Checkbutton(checks, text="Build once", variable=self.build_firmware).pack(
            anchor=tk.W
        )
        ttk.Checkbutton(checks, text="DFU flash once", variable=self.flash_firmware).pack(
            anchor=tk.W
        )
        ttk.Checkbutton(checks, text="Dry run", variable=self.dry_run).pack(anchor=tk.W)
        settings.columnconfigure(2, weight=1)

        toolbar = ttk.Frame(outer)
        toolbar.pack(fill=tk.X, pady=(0, 8))
        self.start_button = ttk.Button(
            toolbar,
            text="Start",
            style="Accent.TButton",
            command=self._start,
        )
        self.start_button.pack(side=tk.LEFT)
        self.pause_button = ttk.Button(
            toolbar,
            text="Pause",
            command=self._toggle_pause,
            state=tk.DISABLED,
        )
        self.pause_button.pack(side=tk.LEFT, padx=(6, 0))
        self.stop_button = ttk.Button(
            toolbar,
            text="Stop after point",
            command=self._stop_after_point,
            state=tk.DISABLED,
        )
        self.stop_button.pack(side=tk.LEFT, padx=(6, 0))
        self.abort_button = ttk.Button(
            toolbar,
            text="Abort now",
            command=self._abort,
            state=tk.DISABLED,
        )
        self.abort_button.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(toolbar, text="Current").pack(side=tk.LEFT, padx=(20, 6))
        ttk.Label(toolbar, textvariable=self.current_text, style="Status.TLabel").pack(
            side=tk.LEFT
        )
        ttk.Label(toolbar, textvariable=self.board_text).pack(
            side=tk.RIGHT
        )

        progress_row = ttk.Frame(outer)
        progress_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Progressbar(
            progress_row,
            variable=self.progress,
            maximum=1.0,
            mode="determinate",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.progress_label = ttk.Label(progress_row, text="0.0%", width=8, anchor=tk.E)
        self.progress_label.pack(side=tk.RIGHT, padx=(8, 0))

        panes = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True)

        table_frame = ttk.Frame(panes)
        image_frame = ttk.Frame(panes)
        panes.add(table_frame, weight=3)
        panes.add(image_frame, weight=2)

        columns = ("set", "request", "ticks", "actual", "mean", "stddev", "status")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = {
            "set": "Set",
            "request": "Request ps",
            "ticks": "Ticks",
            "actual": "Actual ps",
            "mean": "Mean ps",
            "stddev": "Std ps",
            "status": "Status",
        }
        widths = {
            "set": 95,
            "request": 90,
            "ticks": 60,
            "actual": 95,
            "mean": 95,
            "stddev": 85,
            "status": 85,
        }
        for column in columns:
            self.table.heading(column, text=headings[column])
            self.table.column(column, width=widths[column], anchor=tk.CENTER)
        table_scroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.table.yview)
        self.table.configure(yscrollcommand=table_scroll.set)
        self.table.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        table_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        ttk.Label(image_frame, text="Latest scope capture", style="Status.TLabel").pack(
            anchor=tk.W, pady=(0, 6)
        )
        self.image_label = ttk.Label(image_frame, anchor=tk.CENTER)
        self.image_label.pack(fill=tk.BOTH, expand=True)

        log_frame = ttk.LabelFrame(outer, text="Log", padding=4)
        log_frame.pack(fill=tk.X, pady=(8, 0))
        self.log = tk.Text(
            log_frame,
            height=8,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("Consolas", 9),
        )
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _browse_output(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_root.get())
        if selected:
            self.output_root.set(selected)

    def _steps_from_form(self) -> list[SweepStep]:
        config = load_config(DEFAULT_CONFIG_PATH)
        return build_steps(config, int(self.repeats.get()))

    def _populate_preview(self) -> None:
        for item in self.table.get_children():
            self.table.delete(item)
        try:
            steps = self._steps_from_form()
        except Exception:
            return
        for step in steps:
            self.table.insert(
                "",
                tk.END,
                iid=str(step.index),
                values=(
                    step.set_id,
                    step.delay_ps,
                    step.signed_ticks,
                    f"{step.actual_delay_ps:.3f}",
                    "",
                    "",
                    "Pending",
                ),
            )

    def _append_log(self, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, f"[{timestamp}] {text}\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _start(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        try:
            repeats = int(self.repeats.get())
            if repeats < 1:
                raise ValueError("Repeats must be at least 1")
            if self.build_firmware.get() and not self.flash_firmware.get() and not self.dry_run.get():
                raise ValueError("Build once requires DFU flash once in autonomous mode")
            output_root = Path(self.output_root.get()).expanduser().resolve()
        except Exception as error:
            messagebox.showerror("Invalid configuration", str(error))
            return

        self._populate_preview()
        self.progress.set(0.0)
        self.progress_label.configure(text="0.0%")
        self.current_text.set("-")
        self.board_text.set("Autonomous run")
        self.stage_text.set("Starting")
        self.paused = False
        self.pause_button.configure(text="Pause")
        self.start_button.configure(state=tk.DISABLED)
        self.pause_button.configure(state=tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL)
        self.abort_button.configure(state=tk.NORMAL)

        options = RunOptions(
            scope_host=self.scope_host.get().strip(),
            repeats=repeats,
            output_root=output_root,
            build_firmware=self.build_firmware.get(),
            flash_firmware=self.flash_firmware.get(),
            dry_run=self.dry_run.get(),
        )
        self.runner = AutomationRunner(options, self._queue_event)
        self.worker = threading.Thread(target=self.runner.run, daemon=True)
        self.worker.start()

    def _queue_event(self, event: str, payload: dict[str, Any]) -> None:
        self.events.put((event, payload))

    def _toggle_pause(self) -> None:
        if self.runner is None:
            return
        self.paused = not self.paused
        self.runner.request_pause(self.paused)
        self.pause_button.configure(text="Resume" if self.paused else "Pause")
        self._append_log("Pause requested" if self.paused else "Resume requested")

    def _stop_after_point(self) -> None:
        if self.runner is not None:
            self.runner.request_stop_after_step()
            self.stop_button.configure(state=tk.DISABLED)
            self._append_log("Safe stop requested after the current point")

    def _abort(self) -> None:
        if self.runner is not None:
            self.runner.abort()
            self.abort_button.configure(state=tk.DISABLED)
            self._append_log("Immediate abort requested")

    def _show_screenshot(self, path: Path) -> None:
        try:
            image = tk.PhotoImage(file=str(path))
            divisor = max(1, (image.width() + 619) // 620)
            if divisor > 1:
                image = image.subsample(divisor, divisor)
            self.latest_image = image
            self.image_label.configure(image=image)
        except tk.TclError as error:
            self._append_log(f"Could not display screenshot: {error}")

    def _handle_event(self, event: str, payload: dict[str, Any]) -> None:
        if event == "log":
            self._append_log(str(payload["text"]))
        elif event == "stage":
            self.stage_text.set(str(payload["text"]))
            self._append_log(str(payload["text"]))
        elif event == "reset_required":
            self.board_text.set("Waiting for RESET")
            ready = messagebox.askokcancel(
                "Reset board",
                "RTM3004 and its generator are configured and idle.\n\n"
                "Press RESET on the board once, then select OK.\n"
                "Do not press BOOT.",
            )
            if self.runner is not None:
                if ready:
                    self.board_text.set("RESET acknowledged")
                    self._append_log("Board RESET acknowledged by operator")
                    self.runner.confirm_board_reset()
                else:
                    self.runner.abort()
        elif event == "plan":
            self._populate_preview()
        elif event == "step":
            step: SweepStep = payload["step"]
            self.current_text.set(step.prefix)
            item = str(payload["index"])
            values = list(self.table.item(item, "values"))
            values[-1] = "Running"
            self.table.item(item, values=values)
            self.table.see(item)
        elif event == "progress":
            value = float(payload["value"])
            self.progress.set(value)
            self.progress_label.configure(text=f"{value * 100:.1f}%")
        elif event == "result":
            item = str(payload["index"])
            values = list(self.table.item(item, "values"))
            stats = payload.get("stats")
            if stats is not None:
                values[4] = f"{stats.mean_s * 1e12:.3f}"
                values[5] = f"{stats.stddev_s * 1e12:.3f}"
            values[-1] = (
                "Warning"
                if payload.get("quality_status") == "warning"
                else "Done"
            )
            self.table.item(item, values=values)
        elif event == "screenshot":
            self._show_screenshot(Path(payload["path"]))
        elif event == "finished":
            status = str(payload["status"])
            self.stage_text.set(status.capitalize())
            if payload.get("error"):
                self._append_log(str(payload["error"]))
                messagebox.showerror("Automation stopped", str(payload["error"]))
            elif payload.get("output_dir"):
                self._append_log(f"Results: {payload['output_dir']}")
            self.start_button.configure(state=tk.NORMAL)
            self.pause_button.configure(state=tk.DISABLED, text="Pause")
            self.stop_button.configure(state=tk.DISABLED)
            self.abort_button.configure(state=tk.DISABLED)
            self.paused = False
            self.board_text.set(
                "Simulation complete" if self.dry_run.get() else "RESET required before next run"
            )

    def _pump_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                self._handle_event(event, payload)
        except queue.Empty:
            pass
        self.root.after(50, self._pump_events)

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno("Exit", "Abort the active run and close?"):
                return
            if self.runner is not None:
                self.runner.abort()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    AutomationApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
