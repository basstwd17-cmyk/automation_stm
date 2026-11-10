@echo off
setlocal
title K15 Input/Output Diagnostic Smoke
echo.
echo [K15] Five-pulse IN/C1/C2 diagnostic smoke run
echo [K15] All five waveforms are retained; press RESET only when prompted.
echo.

where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0runner.py" --config "%~dp0config_input_reference_diagnostic.json" --smoke --build --flash
) else (
    py -3 "%~dp0runner.py" --config "%~dp0config_input_reference_diagnostic.json" --smoke --build --flash
)

set "RUN_EXIT=%ERRORLEVEL%"
echo.
if not "%RUN_EXIT%"=="0" (
    echo [FAILED] Exit code: %RUN_EXIT%
    echo Copy the error text above if the cause is unclear.
    pause
)
endlocal
