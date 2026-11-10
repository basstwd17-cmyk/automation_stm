@echo off
setlocal
title K15 Input-Reference Smoke
echo.
echo [K15] Input-reference smoke run
echo.

where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0runner.py" --config "%~dp0config_input_reference.json" --smoke --build --flash
) else (
    py -3 "%~dp0runner.py" --config "%~dp0config_input_reference.json" --smoke --build --flash
)

set "RUN_EXIT=%ERRORLEVEL%"
echo.
if not "%RUN_EXIT%"=="0" (
    echo [FAILED] Exit code: %RUN_EXIT%
    echo Copy the error text above if the cause is unclear.
    pause
)
endlocal
