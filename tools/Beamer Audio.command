#!/bin/sh
# Double-click to start Beamer Audio in the menu bar (docs/audio-experiment.md).
cd "$(dirname "$0")/.." || exit 1
if [ ! -x .venv-audio/bin/python ]; then
    echo "Set up .venv-audio first: see docs/audio-experiment.md"
    exit 1
fi
nohup .venv-audio/bin/python tools/audio_menu_mac.py >/dev/null 2>&1 &
