@echo off

@if not "%~0"=="%~dp0.\%~nx0" start /min cmd /c,"%~dp0.\%~nx0" %* & goto :eof

echo Starting camera monitoring system with elevated privileges...

powershell -Command "Start-Process cmd -ArgumentList '/c cd %~dp0 && python app.py' -Verb RunAs"

pause
