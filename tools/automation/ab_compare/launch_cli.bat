@echo off
setlocal
py -3 "%~dp0run.py" %*
if errorlevel 1 python "%~dp0run.py" %*
endlocal
