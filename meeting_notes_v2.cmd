@echo off
rem Meeting Notes v2 launcher: runs the panel with its own Python environment (GPU Whisper).
rem No arguments = show the panel. --start / --stop "title" / --pause / --resume / --quit talk to the running copy.
rem Python: %MN_PYTHONW%, else the path in meeting_notes_python.txt next to this file, else .venv next to this file.
setlocal
set "PYW=%MN_PYTHONW%"
if not defined PYW if exist "%~dp0meeting_notes_python.txt" set /p PYW=<"%~dp0meeting_notes_python.txt"
if not defined PYW set "PYW=%~dp0.venv\Scripts\pythonw.exe"
start "" "%PYW%" -X utf8 "%~dp0meeting_notes_v2.pyw" %*
