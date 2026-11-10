@echo off
setlocal
title K15 External-Input Smoke
echo.
echo [K15] External-input C1/C2/C3 smoke measurement
echo [K15] This launcher does not control the RTM generator or any input board.
echo.

where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0external_input_k15.py" --segments 3
) else (
    py -3 "%~dp0external_input_k15.py" --segments 3
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
