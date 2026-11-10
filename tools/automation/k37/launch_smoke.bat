@echo off
setlocal
where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0runner.py" --smoke --build --flash
) else (
    py -3 "%~dp0runner.py" --smoke --build --flash
)
endlocal
pause
