@echo off

rem @if not "%~0"=="%~dp0.\%~nx0" start /min cmd /c,"%~dp0.\%~nx0" %* & goto :eof

cd D:\sub-system\cam

cd %~dp0

python cam_tmpfile_del.py

exit
