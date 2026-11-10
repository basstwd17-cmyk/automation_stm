@echo off
setlocal
py -3 "%~dp0ui.py"
if errorlevel 1 python "%~dp0ui.py"
endlocal
