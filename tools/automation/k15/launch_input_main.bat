@echo off
setlocal
py -3 "%~dp0runner.py" --config "%~dp0config_input_reference.json"
if errorlevel 1 python "%~dp0runner.py" --config "%~dp0config_input_reference.json"
endlocal
