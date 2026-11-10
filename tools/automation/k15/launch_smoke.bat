@echo off
py -3 "%~dp0runner.py" --smoke --build --flash
pause
