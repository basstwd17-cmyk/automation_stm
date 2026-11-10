@echo off
setlocal
where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0runner.py" --smoke
) else (
    py -3 "%~dp0runner.py" --smoke
)
endlocal
pause
