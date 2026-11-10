@echo off
setlocal
title External Input Fast Statistics Sweep
echo.
echo [RTM3004] External-input C1/C2/C3 fast-statistics sweep
echo [RTM3004] 4 steps x 1000 samples, no WGEN and no K15 waveform export.
echo.
where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0external_input_statistics_sweep.py"
) else (
    py -3 "%~dp0external_input_statistics_sweep.py"
)
set "RUN_EXIT=%ERRORLEVEL%"
echo.
echo [Exit code: %RUN_EXIT%]
pause
endlocal
