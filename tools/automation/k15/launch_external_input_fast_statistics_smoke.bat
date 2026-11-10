@echo off
setlocal
title External Input Fast Statistics Smoke
echo.
echo [RTM3004] External-input C1/C2/C3 fast-statistics smoke
echo [RTM3004] No WGEN command and no K15 waveform export.
echo.
where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0external_input_statistics_sweep.py" --smoke
) else (
    py -3 "%~dp0external_input_statistics_sweep.py" --smoke
)
set "RUN_EXIT=%ERRORLEVEL%"
echo.
echo [Exit code: %RUN_EXIT%]
pause
endlocal
