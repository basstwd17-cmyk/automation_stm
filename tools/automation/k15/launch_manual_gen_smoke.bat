@echo off
setlocal
title K15 Manual-Gen Minimal Smoke
echo.
echo [K15] Minimal manual-Gen 3-channel smoke
echo [K15] This does not use runner.py, Burst mode, or firmware flashing.
echo.

where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0manual_gen_smoke.py"
) else (
    py -3 "%~dp0manual_gen_smoke.py"
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
