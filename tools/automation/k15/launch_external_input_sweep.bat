@echo off
setlocal
title K15 External-Input Full-Waveform Measurement
echo.
echo [K15] Full-waveform input-reference run (Z00, 1000 segments).
echo [K15] The normal-statistics sweep is not used for final data.
echo.

where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0external_input_k15.py" --main
) else (
    py -3 "%~dp0external_input_k15.py" --main
)

set "RUN_EXIT=%ERRORLEVEL%"
echo.
if "%RUN_EXIT%"=="0" (
    echo [COMPLETED] Exit code: 0
) else (
    echo [FAILED] Exit code: %RUN_EXIT%
)
echo.
echo Press any key to close this window.
pause >nul
endlocal
