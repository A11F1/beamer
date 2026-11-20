@echo off
rem Double-click to start Beamer Audio in the tray (docs\audio-experiment.md).
cd /d "%~dp0.."
if not exist ".venv-audio\Scripts\pythonw.exe" (
    echo Set up .venv-audio first: see docs\audio-experiment.md
    pause
    exit /b 1
)
start "" ".venv-audio\Scripts\pythonw.exe" tools\audio_window_win.py
