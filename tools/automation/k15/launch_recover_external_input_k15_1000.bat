@echo off
setlocal
title Recover K15 External-Input History (1000)
echo.
echo [K15] Exporting the retained 1000-segment C1/C2/C3 History.
echo [K15] This does not arm, stop, or reconfigure the oscilloscope.
echo.
where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0recover_external_input_k15_history.py" --segments 1000
) else (
    py -3 "%~dp0recover_external_input_k15_history.py" --segments 1000
)
set "RUN_EXIT=%ERRORLEVEL%"
echo.
echo [Exit code: %RUN_EXIT%]
pause
endlocal
