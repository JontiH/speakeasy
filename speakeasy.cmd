@echo off
rem Speakeasy for Windows. First call starts recording, second stops and types
rem the text. The hotkeys are handled by the tray listener (speakeasy --listen).
setlocal
set "HERE=%~dp0"
set "PYTHONPATH=%HERE%src;%PYTHONPATH%"
"%HERE%venv\Scripts\python.exe" -m speakeasy %*
