@echo off
setlocal
title K15 Input-Reference Main (No Flash)
echo.
echo [K15] 3-channel input-reference main run (existing firmware)
echo.

where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0runner.py" --config "%~dp0config_input_reference.json"
) else (
    py -3 "%~dp0runner.py" --config "%~dp0config_input_reference.json"
)

set "RUN_EXIT=%ERRORLEVEL%"
echo.
if not "%RUN_EXIT%"=="0" (
    echo [FAILED] Exit code: %RUN_EXIT%
    pause
)
endlocal
