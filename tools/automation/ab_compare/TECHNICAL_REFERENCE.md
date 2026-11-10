# C1-C2 Statistics Technical Reference

이 폴더는 일반 C1–C2 통계 측정과 A/B 단자 교환 비교만 관리한다.

## Run

Launch `launch_ui.bat` or run:

```powershell
py -3 tools\automation\ui.py
```

Connect the RTM3004 as follows:

| Instrument | PCB signal | Connector | MCU pin |
| --- | --- | --- | --- |
| Generator output | IN1 | J6 | PB9 / EEV5 |
| C1 | OUT3 | J4 | PB14 / TD1 |
| C2 | OUT4 | J5 | PB15 / TD2 |

The measured delay sign is `OUT4(C2) - OUT3(C1)`. A positive value delays
OUT4; a negative value delays OUT3.

## Firmware Plan

`config.json` is the single source for the ordered delay list, repeat count,
and `waveforms_per_step`. Before a build, the automation generates:

```text
Core/Inc/autonomous_sweep.h
```

The STM32 starts at step 0 after reset. EXTI9 records the time of every rising
edge on PB9 while the pin remains connected to HRTIM EEV5. The delay remains
fixed throughout a burst. When no PB9 edge has arrived for 300 ms, the main loop
applies the next delay. This is longer than the 100 ms interval inside a 10 Hz
burst and shorter than the enforced delay between measurement points. After the
last burst, Timer D outputs stop.

When the plan changes, select both **Build once** and **DFU flash once**. Enter
DFU mode when requested. For every hardware run, the UI first configures the
RTM3004 with generator output off, enables burst mode, reapplies `SINGle` mode,
and reads back burst state, trigger mode, cycle count, output state, and the
SCPI error queue. Press RESET once while generator output remains off. The first
scope acquisition is armed at zero before generator output is enabled, so the
first input after reset belongs to the first measured point.

Normal application mode does not start USB CDC. DFU programming through the
STM32 ROM bootloader remains available.

## Synchronization

For each point the scope statistics are reset while acquisition is stopped.
The automation sets `ACQ:NSIN:COUN` to the configured waveform count and arms
the scope with `SINGle`. Fast Segmentation is disabled so every acquired waveform
is processed and added to the delay statistics. After the generator state and zero statistics count are
verified, the first point enables generator output. If output enable starts an
automatic burst, that burst is measured without sending another trigger. If no
waveform arrives for one second, one software burst trigger is sent. Later
points send exactly one software burst trigger. The scope reports `COMPlete`
only after the configured number of real trigger acquisitions. Generator output
and burst mode remain enabled and idle in `SINGle` mode between points. They are
disabled only when the complete run ends or an error or abort occurs.

The burst phase is 180 degrees so the generator waits low between rising-edge
triggers. After acquisition, the PC waits at least 500 ms so the firmware can
detect the 300 ms input-idle boundary and apply the next delay. The PC and
firmware plans are protected by the same SHA-256 signature; a mismatch stops the
run before any measurement.

Before every burst, the automation verifies the programmed Nx Single count and
confirms that the armed statistics counter is still zero. No burst is sent if
the counter starts increasing without a generator trigger. Completion is based
on the scope returning `COMPlete`; `WFMCount` and the statistics sample count are
checked afterward only to validate the result.

In normal trigger mode, this RTM3004 can report `ACQ:STAT?` as `STOP` while an
Nx Single acquisition is armed and waiting. The automation therefore checks the
`STAT:OPER:COND?` WTRigger bit together with the acquisition state. `RUN` or
WTRigger means armed, `COMPlete` means success, and `BREak` means interruption.

The point timeout is calculated from
`waveforms_per_step / generator.frequency_hz`, with a minimum of ten seconds.
If at least one waveform was acquired before a timeout or early scope stop, only
the scope acquisition is stopped. The automation waits until the already
triggered generator burst has fully drained before starting the next point.
Available samples are saved, the point is marked `warning`, and the sweep
continues. A zero-waveform timeout remains a hard measurement failure because
the trigger path cannot be verified.

The automation searches ordered delay samples for a persistent change point. A
point containing two delay plateaus is saved both as the normal raw sample CSV
and as `*_mixed_delay_samples.csv`. It is written to `summary.csv` with
`quality_status=warning`, then the next delay is measured. Waveform-count and
sample-count mismatches use the same warning-and-continue policy.

The default generator frequency is 10 Hz. This leaves 100 ms between output
pairs so RTM3004 measurement statistics can record every result. If the
frequency is reduced below 10 Hz, `PULSE_BURST_IDLE_GAP_MS` must remain longer
than one generator period or a burst can be split into multiple steps.

Pause and safe stop take effect between points. Abort disables the generator
immediately. Press RESET before every new run, including after a stop, abort,
or completed sweep.

## Saved Results

Each run creates a timestamped directory containing:

- `manifest.json`: complete configuration and ordered compiled plan
- `summary.csv`: requested, quantized, measured, and quality-status fields
- one folder per point containing plan JSON, delay samples, waveform CSV, and PNG
- `*_partial_delay_samples.csv`: samples recovered from an interrupted point
- `*_mixed_delay_samples.csv`: a copy of raw samples flagged as mixed plateaus
- `run_status.json`: completed, stopped, aborted, or failed status

Each PNG is transferred directly from the current RTM3004 display with
`HCOP:DATA?`. The automation does not reuse an image file from the instrument's
internal storage.

The automation uses fixed channel scale, timebase, trigger level, delay
reference, record length, and generator settings for comparable measurements.
