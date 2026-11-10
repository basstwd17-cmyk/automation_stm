@echo off
setlocal
title K15 Input-Reference Smoke (No Flash)
echo.
echo [K15] 3-channel input-reference smoke (existing firmware)
echo.

where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0runner.py" --config "%~dp0config_input_reference.json" --smoke
) else (
    py -3 "%~dp0runner.py" --config "%~dp0config_input_reference.json" --smoke
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
